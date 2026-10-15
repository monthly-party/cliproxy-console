"""Codex fast mode for every client of the proxy, as one payload override rule.

Fast mode is priority processing: the proxy writes service_tier=priority into every request
its Codex executor sends upstream (`requests.payload.override` in the v8 config). The gateway
reads and replaces only that list through the proxy's v8 config API, which saves and reloads
it without a restart, and leaves every other rule as it found it. No other part of the proxy
config is read or returned.
"""
from http.client import HTTPConnection, HTTPException
import json
import threading

from settings import PROXY_HOST, PROXY_PORT

MAX_BYTES = 1024 * 1024
OVERRIDE_PATH = '/config/requests/payload/override'
PAYLOAD_PATH = '/config/requests/payload'
RULE = {'models': [{'name': '*', 'protocol': 'codex'}], 'params': {'service_tier': 'priority'}}


class FastModeError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def is_fast_rule(rule):
    """The rule this module writes. The proxy saves rules with empty conditions spelled out."""
    if not isinstance(rule, dict) or set(rule) != {'models', 'params'} or rule['params'] != RULE['params']:
        return False
    models = rule['models']
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        return False
    entry = models[0]
    if entry.get('name') != '*' or entry.get('protocol') != 'codex':
        return False
    # Any condition (headers, from-protocol, match...) makes it someone else's rule.
    return not any(value for key, value in entry.items() if key not in ('name', 'protocol'))


class CodexFastMode:
    def __init__(self, key, request=None):
        self.key = key
        self.request = request or self._request
        self.lock = threading.Lock()

    def _request(self, method, path, payload=None):
        connection = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=10)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, '/v8/management' + path, body=body, headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            content = response.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise FastModeError(502, 'fast_mode_backend_unavailable')
            return response.status, content
        except (OSError, ValueError, HTTPException):
            raise FastModeError(502, 'fast_mode_backend_unavailable') from None
        finally:
            connection.close()

    def overrides(self):
        status, content = self.request('GET', OVERRIDE_PATH)
        if status == 404:
            return []
        if status != 200:
            raise FastModeError(502, 'fast_mode_backend_unavailable')
        rules = json.loads(content)
        if rules is None:
            return []
        if not isinstance(rules, list):
            raise FastModeError(409, 'unmanaged_payload_rules')
        return rules

    def state(self):
        return {'enabled': any(is_fast_rule(rule) for rule in self.overrides())}

    def change(self, enabled):
        with self.lock:
            rules = self.overrides()
            if any(is_fast_rule(rule) for rule in rules) == enabled:
                return {'enabled': enabled}
            kept = [rule for rule in rules if not is_fast_rule(rule)]
            # Lists are replaced whole; the other rules go back exactly as they were read.
            status, _ = self.request('PATCH', PAYLOAD_PATH, {'override': kept + [RULE] if enabled else kept})
            if status != 200:
                raise FastModeError(502, 'fast_mode_not_saved')
            current = self.state()
            if current['enabled'] != enabled:
                raise FastModeError(502, 'fast_mode_not_confirmed')
            return current
