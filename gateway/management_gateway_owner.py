"""Owner-authenticated loopback management gateway.

Serves the reviewed single HTML build and a small account-management API surface.
Keys are read from protected files at runtime, never embedded in HTML or logged.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
import argparse
import hmac
import sqlite3
import json
import threading
import time
from owner_auth_policy import authorize_owner_request
from account_preferences import AccountPreferences, PreferenceError, preference_records, attach_preferences
import usage_ledger
from settings import PROXY_HOST, PROXY_PORT
from reset_tickets import ResetTickets, TicketError
from live_activity import LiveActivity

PREFIX = '/v8/management'
QUOTA_URLS = {
    'https://api.anthropic.com/api/oauth/usage',
    'https://chatgpt.com/backend-api/wham/usage',
}


def safe_activity(record):
    """Expose completed-request counts; never infer a live or last-used account."""
    buckets = record.get('recent_requests')
    def count(value):
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else None
    if not isinstance(buckets, list) or len(buckets) != 20 or not isinstance(buckets[-1], dict):
        return None
    success = count(record.get('success'))
    failed = count(record.get('failed'))
    recent_success = count(buckets[-1].get('success'))
    recent_failed = count(buckets[-1].get('failed'))
    if None in (success, failed, recent_success, recent_failed):
        return None
    return {'kind': 'recent', 'completed_in_current_window': recent_success + recent_failed,
            'window_minutes': 10, 'success': success, 'failed': failed}


def sanitize_accounts(content):
    document = json.loads(content)
    fields = {'id', 'name', 'provider', 'type', 'email', 'auth_index',
              'status', 'disabled', 'unavailable', 'cooldowns', 'quota_supported',
              'created_at', 'updated_at', 'last_refresh', 'next_retry_after'}
    safe_files = []
    for record in document.get('files', []):
        if not isinstance(record, dict) or record.get('provider', record.get('type')) not in {'claude', 'anthropic', 'codex'}:
            continue
        safe = {key: value for key, value in record.items() if key in fields}
        activity = safe_activity(record)
        if activity is not None:
            safe['activity'] = activity
        claims = record.get('id_token', {})
        # The pinned list endpoint returns decoded claims. Raw tokens are never passed.
        if isinstance(claims, dict):
            for key in ['chatgpt_account_id', 'plan_type', 'chatgpt_plan_type']:
                if isinstance(claims.get(key), str):
                    safe[key] = claims[key]
        for key in ['chatgpt_account_id', 'plan_type']:
            if isinstance(record.get(key), str):
                safe[key] = record[key]
        safe_files.append(safe)
    return json.dumps({'observed_at': document.get('observed_at'), 'files': safe_files}).encode()


def quota_model_context():
    """Explicit display context, not a claim of the model used by every session."""
    models = {'claude': 'claude-fable-5', 'codex': 'gpt-6.1-sol'}
    try:
        settings = json.loads((Path.home()/'.t3/userdata/settings.json').read_text())
        selection = settings.get('defaultModelSelection', {})
        provider = {'codex':'codex', 'claudeAgent':'claude'}.get(selection.get('instanceId'))
        model = selection.get('model')
        if provider and isinstance(model, str) and 0 < len(model) <= 128:
            models[provider] = model
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return models


def attach_quota_priority(safe_content, snapshot, models, now=None):
    """Project only quota-based candidates from already-sanitized metadata."""
    from quota_priority import project_quota_priority
    document = json.loads(safe_content)
    projections = project_quota_priority(snapshot, document.get('files', []), models, now)
    for record in document.get('files', []):
        projection = projections.get(str(record.get('auth_index', '')))
        if projection is not None:
            record['quota_priority'] = projection
    return json.dumps(document).encode()


def fable_window(body):
    """Fable weekly window from Claude's scoped limits, active entry first, else the first one."""
    limits = body.get('limits') if isinstance(body, dict) else None
    if not isinstance(limits, list):
        return None
    candidates = [item for item in limits if isinstance(item, dict) and item.get('kind') == 'weekly_scoped'
                  and str(((item.get('scope') or {}).get('model') or {}).get('display_name', '')).strip().lower() in ('fable', 'fable 5')
                  and isinstance(item.get('percent'), (int, float)) and not isinstance(item.get('percent'), bool)
                  and 0 <= item['percent'] <= 100]
    chosen = next((item for item in candidates if item.get('is_active') is True), candidates[0] if candidates else None)
    if chosen is None:
        return None
    window = {'kind': 'weekly', 'model_prefix': 'claude-fable', 'used_percent': float(chosen['percent']),
              'bucket': 'scoped:claude-fable', 'active': chosen.get('is_active') is True}
    if isinstance(chosen.get('resets_at'), str):
        window['reset_at'] = chosen['resets_at']
    return window


def validate_request(method, raw_path, body):
    """Fail closed before forwarding. No config, export, import or inference."""
    url = urlsplit(raw_path)
    if url.scheme or url.netloc or url.fragment or '%' in url.path:
        raise ValueError('Invalid path')
    query = parse_qs(url.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError('Duplicate parameters')
    params = {key: values[0] for key, values in query.items()}
    route = url.path.removeprefix(PREFIX)
    if not url.path.startswith(PREFIX + '/'):
        raise ValueError('Route unavailable')
    if route.startswith('/dashboard/'):
        if method != 'GET' or body:
            raise ValueError('Dashboard reads only')
        usage_ledger.parse_query(route, params)
        return url.path + ('?' + urlencode(params) if params else '')
    payload = json.loads(body) if body else {}
    if not isinstance(payload, dict):
        raise ValueError('Invalid body')
    if method == 'GET' and route == '/credentials' and not params and not body:
        pass
    elif method == 'DELETE' and route == '/credentials' and not body:
        if set(params) != {'name'} or not params['name'] or '/' in params['name'] or '\\' in params['name']:
            raise ValueError('Single account name required')
    elif method == 'GET' and route == '/oauth/auth-url' and not body:
        if set(params) != {'provider'} or params['provider'] not in {'claude', 'codex'}:
            raise ValueError('Manual Claude or Codex flow required')
    elif method in {'GET', 'DELETE'} and route in {'/oauth/status', '/oauth/session'} and not body:
        if (method, route) not in {('GET', '/oauth/status'), ('DELETE', '/oauth/session')}:
            raise ValueError('Invalid method')
        if set(params) != {'state'} or not params['state'] or len(params['state']) > 512:
            raise ValueError('State required')
    elif method == 'POST' and route == '/oauth/callback' and not params:
        if set(payload) != {'provider', 'redirect_url'} or payload['provider'] not in {'claude', 'codex'}:
            raise ValueError('Invalid callback')
        callback = urlsplit(payload['redirect_url'])
        if callback.scheme != 'http' or callback.hostname not in {'localhost', '127.0.0.1'}:
            raise ValueError('Local callback required')
        if callback.username or callback.password or callback.fragment:
            raise ValueError('Invalid callback URL')
        allowed_port = 54545 if payload['provider'] == 'claude' else 1455
        if callback.port != allowed_port:
            raise ValueError('Unexpected callback port')
    elif method == 'POST' and route == '/requests/api-call' and not params:
        if set(payload) - {'authIndex', 'method', 'url', 'header'}:
            raise ValueError('Quota request contains unsupported fields')
        if payload.get('method') != 'GET' or payload.get('url') not in QUOTA_URLS:
            raise ValueError('Read-only quota endpoint required')
        if not isinstance(payload.get('authIndex'), str) or not payload['authIndex']:
            raise ValueError('Account selector required')
        headers = payload.get('header', {})
        if not isinstance(headers, dict):
            raise ValueError('Invalid quota headers')
        allowed = {'Authorization', 'Content-Type', 'User-Agent', 'anthropic-beta', 'Chatgpt-Account-Id'}
        if set(headers) - allowed or headers.get('Authorization') != 'Bearer $TOKEN$':
            raise ValueError('Invalid quota authorization')
        if any(not isinstance(value, str) or '\r' in value or '\n' in value for value in headers.values()):
            raise ValueError('Invalid quota header values')
    elif method == 'POST' and route == '/reset-ticket' and not params:
        import re
        if set(payload) != {'provider', 'authIndex'} or payload['provider'] not in {'claude', 'codex'}:
            raise ValueError('Exact reset-ticket fields required')
        if not isinstance(payload['authIndex'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,256}', payload['authIndex']):
            raise ValueError('Invalid reset-ticket account')
    elif method == 'POST' and route == '/account-preference' and not params:
        if set(payload) != {'provider', 'authIndex', 'expectedRevision'}:
            raise ValueError('Exact preference fields required')
        if payload['provider'] not in {'claude', 'codex'}:
            raise ValueError('Unsupported preference provider')
        import re
        if payload['authIndex'] is not None and (not isinstance(payload['authIndex'], str)
                or not re.fullmatch(r'[a-zA-Z0-9_-]{1,256}', payload['authIndex'])):
            raise ValueError('Invalid preference account')
        if not isinstance(payload['expectedRevision'], str) or not re.fullmatch(r'[a-f0-9]{64}', payload['expectedRevision']):
            raise ValueError('Preference revision required')
    else:
        raise ValueError('Route unavailable')
    return url.path + ('?' + urlencode(params) if params else '')


class Gateway(BaseHTTPRequestHandler):
    key = b''
    owner_login = ''
    expected_origin = ''
    html_path = None
    quota_collector = None
    account_preferences = None
    ledger = None
    tickets = None
    live = None
    names = {}
    names_at = 0

    def account_reply(self, content):
        records = None
        if self.account_preferences is not None:
            try:
                records = preference_records(content)
            except PreferenceError:
                # Preserve ordinary account display if unmanaged metadata
                # cannot safely be controlled by the narrow preference API.
                pass
        content = sanitize_accounts(content)
        if self.ledger is not None:
            try:
                self.ledger.remember_accounts(json.loads(content).get('files', []))
            except (OSError, ValueError, TypeError, AttributeError):
                # Labels are a convenience for history; never fail the account list.
                pass
        if records is not None:
            content = attach_preferences(content, records)
        if self.quota_collector is not None:
            from quota_poller import read_private_json
            content = attach_quota_priority(content, read_private_json(self.quota_collector.snapshot_path), quota_model_context())
        return content

    def quota_overview(self):
        """All cached quota views in one read. Never triggers a provider request."""
        from datetime import datetime, timezone
        collector = self.quota_collector
        if collector is None:
            return {'accounts': []}
        with collector.lock:
            now = datetime.now(timezone.utc)
            accounts = []
            for key, entry in collector.cache.items():
                provider, index = key.split(':', 1)
                status, view = collector._view(entry, now)
                body = (view.get('body') if isinstance(view.get('body'), dict) else {}) if status == 200 else {}
                extras = {'plan_type': body.get('plan_type') if isinstance(body.get('plan_type'), str) else None}
                if isinstance(body.get('claude_reset_allowance'), dict):
                    extras['claude_reset_allowance'] = body['claude_reset_allowance']
                credits = body.get('rate_limit_reset_credits')
                if isinstance(credits, dict):
                    extras['reset_credits'] = {k: credits.get(k) for k in ('available_count', 'applicable_available_count')}
                balance = body.get('credits')
                if isinstance(balance, dict):
                    extras['credits'] = {k: balance.get(k) for k in ('has_credits', 'unlimited', 'balance')}
                windows = collector.accounts.get(index, {}).get('windows', [])
                fable = fable_window(body) if provider == 'claude' else None
                if fable is not None:
                    # Same rule as the previous console: the scoped Fable limit replaces iguana_necktie.
                    windows = [w for w in windows if w.get('bucket') != 'iguana_necktie'] + [fable]
                accounts.append({'auth_index': index, 'provider': provider, 'status': status,
                                 'cache': view.get('quota_cache'), 'extras': extras, 'windows': windows})
        return {'accounts': accounts, 'observed_at': now.isoformat()}

    def reset_ticket(self, payload):
        provider, index = payload['provider'], payload['authIndex']
        if self.tickets is None:
            self.reply(503, b'{"error":"reset_unavailable"}')
            return
        try:
            with self.account_preferences.lock:
                files = json.loads(sanitize_accounts(self.account_preferences.read())).get('files', [])
            record = next((f for f in files if f.get('auth_index') == index
                           and {'anthropic': 'claude'}.get(f.get('provider'), f.get('provider')) == provider), None)
            if record is None:
                self.reply(404, b'{"error":"account_unavailable"}')
                return
            result = self.tickets.spend(provider, record)
        except TicketError as error:
            self.reply(error.status, json.dumps({'error': error.code, 'detail': error.detail}).encode())
            return
        except (PreferenceError, OSError, ValueError, TypeError, KeyError):
            self.reply(502, b'{"error":"reset_backend_unavailable"}')
            return
        if self.quota_collector is not None:
            # Make the shared collector re-read this account on its next tick (within 5 s).
            with self.quota_collector.lock:
                entry = self.quota_collector.cache.get(provider + ':' + index)
                if entry is not None:
                    entry['next_check_at'] = 0
        self.reply(200, json.dumps(result).encode())

    def live_view(self):
        """In-flight requests and bound sessions per account, keyed by auth index."""
        if self.live is None:
            return {'available': False, 'accounts': {}}
        view = self.live.snapshot()
        if set(view) - set(Gateway.names) and time.time() - Gateway.names_at > 30 or time.time() - Gateway.names_at > 600:
            with self.account_preferences.lock:
                files = json.loads(sanitize_accounts(self.account_preferences.read())).get('files', [])
            Gateway.names = {f['name']: f['auth_index'] for f in files if isinstance(f.get('name'), str) and isinstance(f.get('auth_index'), str)}
            Gateway.names_at = time.time()
        return {'available': True, 'observed_at': time.time(),
                'accounts': {Gateway.names[name]: entry for name, entry in view.items() if name in Gateway.names}}

    def dashboard(self, url):
        route = url.path.removeprefix(PREFIX)
        params = {key: values[0] for key, values in parse_qs(url.query, keep_blank_values=True).items()}
        try:
            query = usage_ledger.parse_query(route, params)
            if route == '/dashboard/quota':
                result = self.quota_overview()
            elif self.ledger is None:
                self.reply(503, b'{"error":"usage_ledger_unavailable"}')
                return
            elif route == '/dashboard/live':
                result = self.live_view()
            elif route == '/dashboard/status':
                result = self.ledger.status()
            elif route == '/dashboard/usage':
                result = self.ledger.usage(**query)
                result['labels'] = self.ledger.accounts()
            elif route == '/dashboard/requests':
                result = self.ledger.requests(**query)
                result['labels'] = self.ledger.accounts()
            else:
                result = self.ledger.quota_history(**query)
                result['labels'] = self.ledger.accounts()
            self.reply(200, json.dumps(result).encode())
        except ValueError:
            self.reply(400, b'{"error":"invalid_query"}')
        except (OSError, sqlite3.Error):
            self.reply(503, b'{"error":"usage_ledger_unavailable"}')

    def log_message(self, *_):
        # Requests contain OAuth state/code; never log URLs, headers or bodies.
        pass

    def reply(self, status, body, content_type='application/json', page=False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if page:
            # Single self-contained file: inline style/script, data: icons, same-origin API.
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
                             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        if self.owner_login:
            if not authorize_owner_request(self.command, self.path, self.client_address[0],
                                           self.headers, self.owner_login, self.expected_origin):
                self.reply(403, b'{"error":"Owner authentication required"}')
                return
            if self.command == 'GET' and self.path == PREFIX + '/session':
                self.reply(200, b'{"authenticated":true,"authMode":"tailscale-owner"}')
                return
        if self.command == 'GET' and self.path in {'/', '/management.html'}:
            self.reply(200, self.html_path.read_bytes(), 'text/html; charset=utf-8', page=True)
            return
        supplied = self.headers.get('Authorization', '').encode()
        if not self.owner_login and not hmac.compare_digest(supplied, b'Bearer ' + self.key):
            self.reply(401, b'{"error":"Authentication required"}')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or length < 0 or length > 16384:
                raise ValueError('Unsupported body')
            body = self.rfile.read(length)
            path = validate_request(self.command, self.path, body)
        except (ValueError, TypeError, KeyError):
            self.reply(403, b'{"error":"Operation unavailable"}')
            return
        if path.startswith(PREFIX + '/dashboard/'):
            self.dashboard(urlsplit(path))
            return
        if self.command == 'POST' and path == PREFIX + '/reset-ticket':
            self.reset_ticket(json.loads(body))
            return
        if self.command == 'POST' and path == PREFIX + '/requests/api-call':
            if self.quota_collector is None:
                self.reply(503, b'{"error":"Quota collector unavailable"}')
                return
            try:
                status, reply = self.quota_collector.get(json.loads(body))
                self.reply(status, json.dumps(reply).encode())
            except (OSError, ValueError, TypeError, KeyError):
                self.reply(502, b'{"error":"Quota collector unavailable"}')
            return
        if self.account_preferences is not None and ((self.command == 'GET' and path == PREFIX + '/credentials')
                or (self.command == 'POST' and path == PREFIX + '/account-preference')):
            try:
                with self.account_preferences.lock:
                    if self.command == 'GET':
                        content = self.account_preferences.read()
                    else:
                        payload = json.loads(body)
                        content = self.account_preferences.change(payload['provider'], payload['authIndex'], payload['expectedRevision'])
                    self.reply(200, self.account_reply(content))
            except PreferenceError as error:
                self.reply(error.status, json.dumps({'error': error.code}).encode())
            except (OSError, ValueError, TypeError, KeyError):
                self.reply(502, b'{"error":"preference_backend_unavailable"}')
            return
        if path == PREFIX + '/account-preference':
            self.reply(503, b'{"error":"preference_unavailable"}')
            return
        connection = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=60)
        try:
            connection.request(self.command, path, body=body, headers={
                'Authorization': 'Bearer ' + self.key.decode(),
                'Content-Type': 'application/json',
            })
            response = connection.getresponse()
            content = response.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise ValueError('Response too large')
            # Error bodies can contain upstream credential diagnostics.
            if response.status >= 400:
                content = b'{"error":"Account operation failed"}'
            elif self.command == 'GET' and path == PREFIX + '/credentials':
                content = self.account_reply(content)
            self.reply(response.status, content)
        except (OSError, ValueError, TypeError, AttributeError, HTTPException):
            self.reply(502, b'{"error":"Management service unavailable"}')
        finally:
            connection.close()

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch
    do_PUT = dispatch
    do_PATCH = dispatch


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--key-file', required=True, type=Path)
    parser.add_argument('--owner-login', default='')
    parser.add_argument('--origin', default='')
    parser.add_argument('--quota-snapshot', type=Path)
    parser.add_argument('--quota-cache', type=Path)
    parser.add_argument('--usage-db', type=Path)
    parser.add_argument('--port', type=int, default=8318)
    parser.add_argument('--proxy-log', type=Path)
    options = parser.parse_args()
    if bool(options.owner_login) != bool(options.origin):
        raise SystemExit('Owner login and HTTPS origin must be configured together')
    Gateway.owner_login = options.owner_login
    Gateway.expected_origin = options.origin
    if options.key_file.stat().st_mode & 0o077:
        raise SystemExit('Management key file must be private')
    Gateway.key = options.key_file.read_bytes().strip()
    Gateway.account_preferences = AccountPreferences(Gateway.key)
    if len(Gateway.key) < 32:
        raise SystemExit('Management key is missing or too short')
    Gateway.html_path = options.html
    if bool(options.quota_snapshot) != bool(options.quota_cache):
        raise SystemExit('Quota snapshot and cache must be configured together')
    if options.quota_snapshot:
        from quota_poller import SharedQuotaCollector
        Gateway.quota_collector = SharedQuotaCollector(
            Gateway.key.decode(), options.quota_snapshot, options.quota_cache)
        def collect_quota():
            while True:
                try:
                    Gateway.quota_collector.tick()
                except Exception:
                    # Never log upstream response bodies or credential diagnostics.
                    pass
                time.sleep(5)
        threading.Thread(target=collect_quota, daemon=True).start()
    Gateway.tickets = ResetTickets(Gateway.key.decode())
    if options.proxy_log:
        Gateway.live = LiveActivity(options.proxy_log)
        def follow_log():
            while True:
                try:
                    Gateway.live.read()
                except Exception:
                    # The log is the proxy's; never echo its lines anywhere.
                    pass
                time.sleep(1)
        threading.Thread(target=follow_log, daemon=True).start()
    if options.usage_db:
        Gateway.ledger = usage_ledger.UsageLedger(Gateway.key.decode(), options.usage_db)
        def collect_usage():
            labels_at = 0
            while True:
                try:
                    Gateway.ledger.tick(options.quota_snapshot)
                    if time.time() - labels_at > 600:
                        labels_at = time.time()
                        with Gateway.account_preferences.lock:
                            safe = sanitize_accounts(Gateway.account_preferences.read())
                        Gateway.ledger.remember_accounts(json.loads(safe).get('files', []))
                except Exception:
                    # Same rule as the quota thread: no bodies or diagnostics in logs.
                    pass
                time.sleep(5)
        threading.Thread(target=collect_usage, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', options.port), Gateway).serve_forever()
