"""Owner-triggered spend of one provider reset ticket for one account.

Every spend re-reads the provider first and refuses unless a ticket applies right
now, so a stale dashboard can never burn a ticket the provider would not honour.
All provider calls go through the proxy's api-call with "$TOKEN$" substitution: this
module never sees a token, and it never logs request or response bodies.

Claude: GET usage?cedar_ember=1 -> next_grant_id, GET oauth/profile -> organization
uuid, POST /api/organizations/{org}/reset_rate_limits (as Claude Code's /limit-reset).
Codex: GET wham/usage -> applicable reset credits, POST
wham/rate-limit-reset-credits/consume (as the Codex CLI does).
"""
import json
import re
import threading
import time
import uuid

from quota_poller import CLAUDE_RESET_USAGE_URL, api

CLAUDE_HEADERS = {'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
                  'User-Agent': 'claude-cli/2.1.287 (external, cli)', 'anthropic-beta': 'oauth-2025-04-20'}
CODEX_USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
CODEX_CONSUME_URL = 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume'
CLAUDE_PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
CLAUDE_RESULTS = {'reset', 'already_used', 'not_limited', 'cooldown', 'ineligible', 'unavailable'}
COOLDOWN_SECONDS = 120


class TicketError(Exception):
    def __init__(self, status, code, detail=None):
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(code)


def body_of(reply):
    body = reply.get('body') if isinstance(reply, dict) else None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    return body if isinstance(body, dict) else None


def short(value, pattern=r'[A-Za-z0-9_.:-]{1,64}'):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


class ResetTickets:
    def __init__(self, key, request=api, clock=time.time):
        self.key = key
        self.request = request
        self.clock = clock
        self.lock = threading.Lock()
        self.busy = set()
        self.last_spend = {}

    def call(self, index, method, url, headers, data=None):
        payload = {'authIndex': index, 'method': method, 'url': url, 'header': headers}
        if data is not None:
            payload['data'] = json.dumps(data)
        status, reply = self.request(self.key, '/requests/api-call', payload)
        upstream = reply.get('status_code', reply.get('statusCode')) if isinstance(reply, dict) else None
        if status != 200 or not isinstance(upstream, int):
            raise TicketError(502, 'provider_unreachable')
        return upstream, body_of(reply)

    def spend(self, provider, record):
        index = record['auth_index']
        with self.lock:
            if index in self.busy:
                raise TicketError(409, 'in_progress')
            # Guard against a double click or a retried request spending twice.
            if self.clock() - self.last_spend.get(index, 0) < COOLDOWN_SECONDS:
                raise TicketError(409, 'recently_used')
            self.busy.add(index)
        try:
            result = self._claude(index) if provider == 'claude' else self._codex(index, record)
            if result.get('spent'):
                with self.lock:
                    self.last_spend[index] = self.clock()
            return result
        finally:
            with self.lock:
                self.busy.discard(index)

    def _claude(self, index):
        status, body = self.call(index, 'GET', CLAUDE_RESET_USAGE_URL, CLAUDE_HEADERS)
        block = (body or {}).get('cedar_ember') if status == 200 else None
        if not isinstance(block, dict):
            raise TicketError(502, 'status_unavailable')
        if block.get('eligible') is not True:
            raise TicketError(409, 'ineligible', short(block.get('ineligible_reason')))
        grant_id = short(block.get('next_grant_id'), r'[a-z0-9_-]{1,40}')
        grants = [g for g in block.get('grants') or [] if isinstance(g, dict)]
        grant = next((g for g in grants if g.get('id') == grant_id), None)
        if not grant_id or grant is None or not isinstance(grant.get('resets_left'), int) or grant['resets_left'] < 1:
            raise TicketError(409, 'no_ticket')
        if grant.get('paused') is True:
            raise TicketError(409, 'paused')
        if grant.get('usable_now') is not True:
            # use_requires_limit grants only apply once the account is actually limited.
            reason = 'not_limited' if grant.get('use_requires_limit', True) and block.get('at_limit') is not True else 'not_usable_now'
            raise TicketError(409, reason, short(block.get('cooldown_until'), r'[0-9T:.+Z-]{10,40}'))
        status, profile = self.call(index, 'GET', CLAUDE_PROFILE_URL, CLAUDE_HEADERS)
        organization = (profile or {}).get('organization') if status == 200 else None
        org = short((organization or {}).get('uuid'), r'[0-9a-fA-F-]{36}')
        if not org:
            raise TicketError(502, 'organization_unavailable')
        status, reply = self.call(index, 'POST', f'https://api.anthropic.com/api/organizations/{org}/reset_rate_limits',
                                  CLAUDE_HEADERS, {'program': 'cedar_ember', 'grant_id': grant_id, 'request_id': str(uuid.uuid4())})
        if status == 429:
            raise TicketError(429, 'rate_limited')
        if status >= 400 or not isinstance(reply, dict):
            raise TicketError(502, 'provider_refused', status)
        outcome = reply.get('result') if reply.get('result') in CLAUDE_RESULTS else 'unavailable'
        left = reply.get('resets_left')
        return {'provider': 'claude', 'result': outcome, 'spent': outcome == 'reset',
                'reason': short(reply.get('reason')), 'resets_left': left if isinstance(left, int) else None,
                'cleared': [c for c in reply.get('cleared') or [] if short(c)][:16],
                'weekly_resets_at': short(reply.get('weekly_resets_at'), r'[0-9T:.+Z-]{10,40}')}

    def _codex(self, index, record):
        headers = {'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json', 'User-Agent': 'codex-cli/0.161.0'}
        account = short(record.get('chatgpt_account_id'), r'[A-Za-z0-9_-]{1,128}')
        if account:
            headers['Chatgpt-Account-Id'] = account
        status, body = self.call(index, 'GET', CODEX_USAGE_URL, headers)
        credits = (body or {}).get('rate_limit_reset_credits') if status == 200 else None
        if not isinstance(credits, dict):
            raise TicketError(502, 'status_unavailable')
        available, applicable = credits.get('available_count'), credits.get('applicable_available_count')
        if not isinstance(available, int) or available < 1:
            raise TicketError(409, 'no_ticket')
        if not isinstance(applicable, int) or applicable < 1:
            raise TicketError(409, 'not_applicable')
        status, reply = self.call(index, 'POST', CODEX_CONSUME_URL, headers, {'redeem_request_id': str(uuid.uuid4())})
        if status == 429:
            raise TicketError(429, 'rate_limited')
        if status >= 400:
            raise TicketError(502, 'provider_refused', status)
        return {'provider': 'codex', 'result': 'reset', 'spent': True, 'reason': None,
                'resets_left': available - 1, 'cleared': [], 'weekly_resets_at': None}
