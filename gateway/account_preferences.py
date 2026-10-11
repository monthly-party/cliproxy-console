"""Narrow owner preference operations using native persistent priority metadata.

No credential files, config or inference endpoints are read or written here.
Native priority changes preserve the running selector and established affinity.
"""
from http.client import HTTPConnection, HTTPException
import hashlib
import json
import re
import threading

from settings import PROXY_HOST, PROXY_PORT

MAX_BYTES = 4 * 1024 * 1024


class PreferenceError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def preference_records(content):
    """Copy only account selectors and integer priorities from the native DTO."""
    document = json.loads(content)
    records = []
    for item in document.get('files', []):
        provider = item.get('provider', item.get('type'))
        if provider == 'anthropic':
            provider = 'claude'
        if provider not in {'claude', 'codex'} or item.get('runtime_only'):
            continue
        index, name = item.get('auth_index'), item.get('name')
        if not isinstance(index, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,256}', index):
            raise PreferenceError(503, 'preference_metadata_unavailable')
        if not isinstance(name, str) or not name or len(name) > 512 or '/' in name or '\\' in name:
            raise PreferenceError(503, 'preference_metadata_unavailable')
        priority = item.get('priority', 0)
        if type(priority) is not int or priority not in (0, 1, 2):
            raise PreferenceError(409, 'unmanaged_priority')
        records.append({'provider': provider, 'auth_index': index, 'name': name, 'priority': priority})
    if len(records) > 64 or len({r['auth_index'] for r in records}) != len(records):
        raise PreferenceError(503, 'preference_metadata_unavailable')
    return records


def provider_preference(records, provider):
    rows = sorted((r for r in records if r['provider'] == provider), key=lambda r: r['auth_index'])
    revision = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    highest = max((r['priority'] for r in rows), default=0)
    preferred = [r['auth_index'] for r in rows if r['priority'] == highest] if highest else []
    return {'mode': 'preferred' if preferred else 'auto',
            'preferred_auth_index': preferred[0] if len(preferred) == 1 else None,
            'revision': revision, 'status': 'conflict' if len(preferred) > 1 else 'ready',
            'scope': 'new-bindings-and-failover', 'fallback': 'weekly-reset',
            'sticky_sessions': 'preserved'}


def attach_preferences(safe_content, records):
    document = json.loads(safe_content)
    states = {p: provider_preference(records, p) for p in ('claude', 'codex')}
    for item in document.get('files', []):
        provider = item.get('provider', item.get('type'))
        provider = 'claude' if provider == 'anthropic' else provider
        if provider in states:
            item['routing_preference'] = states[provider]
    return json.dumps(document).encode()


class AccountPreferences:
    def __init__(self, key, request=None):
        self.key = key
        self.request = request or self._request
        # Serialize native metadata transfers and owner list observations. The
        # inference selector remains unlocked and keeps all eligibility checks.
        self.lock = threading.RLock()

    def _request(self, method, path, payload=None):
        connection = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=10)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, '/v8/management' + path, body=body, headers={
                'Authorization': 'Bearer ' + self.key.decode(), 'Content-Type': 'application/json'})
            response = connection.getresponse()
            content = response.read(MAX_BYTES + 1)
            if response.status != 200 or len(content) > MAX_BYTES:
                raise PreferenceError(502, 'preference_backend_unavailable')
            return content
        except (OSError, ValueError, HTTPException):
            raise PreferenceError(502, 'preference_backend_unavailable') from None
        finally:
            connection.close()

    def read(self):
        return self.request('GET', '/credentials')

    def _patch(self, row, priority):
        self.request('PATCH', '/credentials/fields', {'name': row['name'], 'priority': priority})

    def change(self, provider, auth_index, expected_revision):
        with self.lock:
            original_content = self.read()
            records = preference_records(original_content)
            state = provider_preference(records, provider)
            if state['revision'] != expected_revision:
                raise PreferenceError(409, 'preference_changed')
            rows = [r for r in records if r['provider'] == provider]
            target = next((r for r in rows if r['auth_index'] == auth_index), None)
            if auth_index is not None and target is None:
                raise PreferenceError(404, 'preference_account_unavailable')
            desired = {r['auth_index']: int(r is target) for r in rows}
            if all(r['priority'] == desired[r['auth_index']] for r in rows):
                return original_content
            try:
                # Put the new account above the old preference before lowering
                # it. No transfer deliberately creates two equal top priorities.
                # The value 2 is temporary native metadata, not another UI mode.
                if target is not None:
                    self._patch(target, 2)
                for row in rows:
                    if row is not target and row['priority'] != 0:
                        self._patch(row, 0)
                if target is not None:
                    self._patch(target, 1)
                current_content = self.read()
                current = preference_records(current_content)
                actual = {r['auth_index']: r['priority'] for r in current if r['provider'] == provider}
                if actual != desired:
                    raise PreferenceError(502, 'preference_not_confirmed')
                return current_content
            except Exception:
                # A timed-out PATCH may have committed. Re-read, then restore
                # only these priority integers; never restore whole auth records.
                try:
                    current = preference_records(self.read())
                    actual = {r['auth_index']: r for r in current if r['provider'] == provider}
                    if set(actual) != set(desired):
                        raise PreferenceError(503, 'preference_rollback_unconfirmed')
                    # Raise the previous winner first so rollback is also a
                    # preference transfer, with normal eligibility/fallback.
                    winners = [r for r in rows if r['priority'] > 0]
                    for row in winners:
                        self._patch(row, 2)
                    for row in rows:
                        if row not in winners and actual[row['auth_index']]['priority'] != row['priority']:
                            self._patch(row, row['priority'])
                    for row in winners:
                        self._patch(row, row['priority'])
                    restored = preference_records(self.read())
                    actual = {r['auth_index']: r['priority'] for r in restored if r['provider'] == provider}
                    if actual != {r['auth_index']: r['priority'] for r in rows}:
                        raise PreferenceError(503, 'preference_rollback_unconfirmed')
                except Exception:
                    raise PreferenceError(503, 'preference_rollback_unconfirmed') from None
                raise PreferenceError(502, 'preference_failed_restored') from None
