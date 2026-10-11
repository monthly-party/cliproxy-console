import json
import unittest

from management_gateway_owner import fable_window, validate_request
from reset_tickets import CODEX_CONSUME_URL, ResetTickets, TicketError

ORG = '11111111-2222-3333-4444-555555555555'


class Provider:
    """Fake proxy api-call: records every call, answers by method + URL."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, key, path, payload):
        assert path == '/requests/api-call' and payload['header']['Authorization'] == 'Bearer $TOKEN$'
        self.calls.append(payload)
        for (method, fragment), reply in self.answers.items():
            if payload['method'] == method and fragment in payload['url']:
                status, body = reply
                return 200, {'status_code': status, 'body': json.dumps(body)}
        return 200, {'status_code': 404, 'body': '{}'}

    def posts(self):
        return [c for c in self.calls if c['method'] == 'POST']


def claude_status(usable=True, at_limit=True, left=1):
    return {'cedar_ember': {'eligible': True, 'at_limit': at_limit, 'next_grant_id': 'grant_1',
                            'grants': [{'id': 'grant_1', 'resets_left': left, 'usable_now': usable, 'use_requires_limit': True}]}}


def claude(status, reset=(200, {'result': 'reset', 'resets_left': 0, 'cleared': ['seven_day']})):
    return Provider({('GET', 'oauth/usage'): (200, status), ('GET', 'oauth/profile'): (200, {'organization': {'uuid': ORG}}),
                     ('POST', 'reset_rate_limits'): reset})


class ClaudeTests(unittest.TestCase):
    def test_spends_with_fresh_grant(self):
        provider = claude(claude_status())
        result = ResetTickets('k', request=provider).spend('claude', {'auth_index': 'a1'})
        self.assertEqual((result['result'], result['spent'], result['resets_left']), ('reset', True, 0))
        post = provider.posts()[0]
        self.assertIn(f'/api/organizations/{ORG}/reset_rate_limits', post['url'])
        data = json.loads(post['data'])
        self.assertEqual((data['program'], data['grant_id']), ('cedar_ember', 'grant_1'))

    def test_refuses_without_posting_when_not_limited(self):
        provider = claude(claude_status(usable=False, at_limit=False))
        with self.assertRaises(TicketError) as caught:
            ResetTickets('k', request=provider).spend('claude', {'auth_index': 'a1'})
        self.assertEqual(caught.exception.code, 'not_limited')
        self.assertEqual(provider.posts(), [])

    def test_refuses_without_ticket(self):
        provider = claude(claude_status(left=0))
        with self.assertRaises(TicketError) as caught:
            ResetTickets('k', request=provider).spend('claude', {'auth_index': 'a1'})
        self.assertEqual(caught.exception.code, 'no_ticket')
        self.assertEqual(provider.posts(), [])

    def test_second_spend_is_blocked(self):
        provider = claude(claude_status())
        tickets = ResetTickets('k', request=provider, clock=lambda: 1000.0)
        tickets.spend('claude', {'auth_index': 'a1'})
        with self.assertRaises(TicketError) as caught:
            tickets.spend('claude', {'auth_index': 'a1'})
        self.assertEqual(caught.exception.code, 'recently_used')
        self.assertEqual(len(provider.posts()), 1)

    def test_provider_answer_not_reset_is_reported(self):
        provider = claude(claude_status(), reset=(200, {'result': 'not_limited', 'reason': 'not_limited'}))
        result = ResetTickets('k', request=provider).spend('claude', {'auth_index': 'a1'})
        self.assertEqual((result['result'], result['spent']), ('not_limited', False))


class CodexTests(unittest.TestCase):
    def provider(self, applicable, consume=(200, {})):
        return Provider({('GET', 'wham/usage'): (200, {'rate_limit_reset_credits': {'available_count': 3, 'applicable_available_count': applicable}}),
                         ('POST', 'rate-limit-reset-credits/consume'): consume})

    def test_consumes_when_applicable(self):
        provider = self.provider(1)
        result = ResetTickets('k', request=provider).spend('codex', {'auth_index': 'c1', 'chatgpt_account_id': 'acct-1'})
        post = provider.posts()[0]
        self.assertEqual(post['url'], CODEX_CONSUME_URL)
        self.assertEqual(post['header']['Chatgpt-Account-Id'], 'acct-1')
        self.assertIn('redeem_request_id', json.loads(post['data']))
        self.assertEqual((result['spent'], result['resets_left']), (True, 2))

    def test_refuses_when_not_applicable(self):
        provider = self.provider(0)
        with self.assertRaises(TicketError) as caught:
            ResetTickets('k', request=provider).spend('codex', {'auth_index': 'c1'})
        self.assertEqual(caught.exception.code, 'not_applicable')
        self.assertEqual(provider.posts(), [])

    def test_provider_refusal(self):
        provider = self.provider(1, consume=(400, {'detail': 'nope'}))
        with self.assertRaises(TicketError) as caught:
            ResetTickets('k', request=provider).spend('codex', {'auth_index': 'c1'})
        self.assertEqual((caught.exception.code, caught.exception.detail), ('provider_refused', 400))


class GatewayTests(unittest.TestCase):
    def test_route_validation(self):
        self.assertEqual(validate_request('POST', '/v8/management/reset-ticket', b'{"provider":"codex","authIndex":"b247"}'),
                         '/v8/management/reset-ticket')
        for body in (b'{"provider":"codex"}', b'{"provider":"gemini","authIndex":"x"}',
                     b'{"provider":"codex","authIndex":"x","grant_id":"g"}', b'{"provider":"codex","authIndex":"../x"}'):
            with self.assertRaises(ValueError):
                validate_request('POST', '/v8/management/reset-ticket', body)
        with self.assertRaises(ValueError):
            validate_request('GET', '/v8/management/reset-ticket', b'')

    def test_fable_window_prefers_active_and_falls_back(self):
        limits = [{'kind': 'weekly_all', 'percent': 97},
                  {'kind': 'weekly_scoped', 'percent': 0, 'resets_at': '2026-10-12T03:00:00+00:00', 'is_active': False,
                   'scope': {'model': {'display_name': 'Fable'}}}]
        window = fable_window({'limits': limits})
        self.assertEqual((window['used_percent'], window['active'], window['model_prefix']), (0.0, False, 'claude-fable'))
        self.assertIsNone(fable_window({'limits': [{'kind': 'weekly_all', 'percent': 3}]}))


if __name__ == '__main__':
    unittest.main()
