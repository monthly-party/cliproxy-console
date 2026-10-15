import json
import unittest

from codex_fast_mode import RULE, CodexFastMode, FastModeError, is_fast_rule

# How the proxy hands rules back after saving them: every condition spelled out, empty.
SAVED = {'models': [{'name': '*', 'protocol': 'codex', 'headers': {}, 'from-protocol': '', 'match': [],
                     'not-match': [], 'exist': [], 'not-exist': []}], 'params': {'service_tier': 'priority'}}
OTHER = {'models': [{'name': 'gpt-*', 'protocol': 'codex', 'headers': {}, 'from-protocol': '', 'match': [],
                     'not-match': [], 'exist': [], 'not-exist': []}], 'params': {'reasoning.effort': 'high'}}


class Proxy:
    """The two v8 config paths the module uses, with the proxy's 404 for a missing list."""

    def __init__(self, rules=None, fail_patch=False, ignore_patch=False):
        self.rules = rules
        self.fail_patch = fail_patch
        self.ignore_patch = ignore_patch
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == 'GET' and path == '/config/requests/payload/override':
            return (404, b'{"error":"not_found"}') if self.rules is None else (200, json.dumps(self.rules).encode())
        if method == 'PATCH' and path == '/config/requests/payload':
            if self.fail_patch:
                return 400, b'{"error":"invalid_config"}'
            if not self.ignore_patch:
                self.rules = payload['override']
            return 200, b'{"status":"ok"}'
        raise AssertionError((method, path))


class FastModeTests(unittest.TestCase):
    def test_recognises_only_its_own_rule(self):
        self.assertTrue(is_fast_rule(RULE))
        self.assertTrue(is_fast_rule(SAVED))
        self.assertFalse(is_fast_rule(OTHER))
        scoped = json.loads(json.dumps(SAVED))
        scoped['models'][0]['headers'] = {'User-Agent': 'T3*'}
        self.assertFalse(is_fast_rule(scoped))
        self.assertFalse(is_fast_rule({**SAVED, 'params': {'service_tier': 'priority', 'x': 1}}))

    def test_reads_off_when_no_rule_exists(self):
        self.assertEqual(CodexFastMode('k', Proxy()).state(), {'enabled': False})
        self.assertEqual(CodexFastMode('k', Proxy([OTHER, SAVED])).state(), {'enabled': True})

    def test_switching_keeps_every_other_rule(self):
        proxy = Proxy([OTHER])
        mode = CodexFastMode('k', proxy)
        self.assertEqual(mode.change(True), {'enabled': True})
        self.assertEqual(proxy.rules, [OTHER, RULE])
        self.assertEqual(mode.change(False), {'enabled': False})
        self.assertEqual(proxy.rules, [OTHER])

    def test_no_write_when_already_in_that_state(self):
        proxy = Proxy([SAVED])
        CodexFastMode('k', proxy).change(True)
        self.assertEqual([call[0] for call in proxy.calls], ['GET'])

    def test_refused_or_unconfirmed_save_is_an_error(self):
        with self.assertRaises(FastModeError) as refused:
            CodexFastMode('k', Proxy(fail_patch=True)).change(True)
        self.assertEqual(refused.exception.code, 'fast_mode_not_saved')
        with self.assertRaises(FastModeError) as ignored:
            CodexFastMode('k', Proxy(ignore_patch=True)).change(True)
        self.assertEqual(ignored.exception.code, 'fast_mode_not_confirmed')

    def test_unexpected_shape_is_left_alone(self):
        proxy = Proxy({'not': 'a list'})
        with self.assertRaises(FastModeError) as error:
            CodexFastMode('k', proxy).change(True)
        self.assertEqual(error.exception.code, 'unmanaged_payload_rules')
        self.assertEqual([call[0] for call in proxy.calls], ['GET'])


if __name__ == '__main__':
    unittest.main()
