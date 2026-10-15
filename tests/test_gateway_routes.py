import json
import unittest

from management_gateway_owner import validate_request


class FastModeRouteTests(unittest.TestCase):
    def test_reads_and_switches_with_exactly_one_boolean(self):
        self.assertEqual(validate_request('GET', '/v8/management/codex-fast-mode', b''), '/v8/management/codex-fast-mode')
        for enabled in (True, False):
            body = json.dumps({'enabled': enabled}).encode()
            self.assertEqual(validate_request('POST', '/v8/management/codex-fast-mode', body), '/v8/management/codex-fast-mode')

    def test_refuses_anything_else(self):
        for method, path, body in [
            ('POST', '/v8/management/codex-fast-mode', b'{"enabled": "yes"}'),
            ('POST', '/v8/management/codex-fast-mode', b'{"enabled": 1}'),
            ('POST', '/v8/management/codex-fast-mode', b'{"enabled": true, "rule": {}}'),
            ('POST', '/v8/management/codex-fast-mode?x=1', b'{"enabled": true}'),
            ('GET', '/v8/management/codex-fast-mode', b'{"enabled": true}'),
            ('PATCH', '/v8/management/config/requests/payload', b'{"override": []}'),
            ('GET', '/v8/management/config/requests/payload/override', b''),
        ]:
            with self.assertRaises(ValueError, msg=(method, path, body)):
                validate_request(method, path, body)


if __name__ == '__main__':
    unittest.main()
