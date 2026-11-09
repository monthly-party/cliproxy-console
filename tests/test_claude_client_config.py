"""Exercise the manager's Claude compatibility setup through its HTTP boundary."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from claude_client_config import configure_t3_claude, uses_t3_proxy, ClaudeConfigurationError


class T3ProxyDetectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings_path = self.root / 'settings.json'
        self.wrapper_path = self.root / 'claude-proxy'
        self.wrapper_path.touch()

    def save_provider(self, provider):
        self.settings_path.write_text(json.dumps({'providerInstances': {'custom-pool-name': provider}}))

    def test_no_t3_install_does_not_change_console_deployment(self):
        self.assertFalse(uses_t3_proxy(self.settings_path, self.wrapper_path))

    def test_finds_enabled_claude_proxy_through_a_symlink(self):
        link = self.root / 'linked-wrapper'
        link.symlink_to(self.wrapper_path)
        self.save_provider({'driver': 'claudeAgent', 'enabled': True, 'config': {'binaryPath': str(link)}})
        self.assertTrue(uses_t3_proxy(self.settings_path, self.wrapper_path))

    def test_direct_claude_and_codex_do_not_opt_into_t3_claude_setup(self):
        for provider in [
                {'driver': 'claudeAgent', 'enabled': False, 'config': {'binaryPath': str(self.wrapper_path)}},
                {'driver': 'claudeAgent', 'enabled': True, 'config': {'binaryPath': str(self.root / 'claude')}},
                {'driver': 'codex', 'enabled': True, 'config': {'binaryPath': str(self.wrapper_path)}},
                {'driver': 'claudeAgent', 'enabled': True, 'config': {}}]:
            with self.subTest(provider=provider):
                self.save_provider(provider)
                self.assertFalse(uses_t3_proxy(self.settings_path, self.wrapper_path))

    def test_provider_without_explicit_enabled_setting_uses_t3_default(self):
        self.save_provider({'driver': 'claudeAgent', 'config': {'binaryPath': str(self.wrapper_path)}})
        self.assertTrue(uses_t3_proxy(self.settings_path, self.wrapper_path))

    def test_invalid_settings_are_reported(self):
        self.settings_path.write_text('{invalid-json')
        with self.assertRaises(ClaudeConfigurationError):
            uses_t3_proxy(self.settings_path, self.wrapper_path)


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(('GET', self.path))
        if self.headers.get('Authorization') != 'Bearer test-management-key':
            self.reply(401, {'error': 'unauthorized'})
            return
        if self.path != '/v8/management/config/upstream/claude/header-defaults':
            self.reply(404, {'error': 'not_found'})
            return
        if self.server.read_status != 200:
            self.reply(self.server.read_status, {'error': 'backend unavailable'})
            return
        if self.server.defaults is None:
            self.reply(404, {'error': 'not_found'})
            return
        self.reply(200, self.server.defaults)

    def do_PATCH(self):
        self.server.requests.append(('PATCH', self.path))
        if self.headers.get('Authorization') != 'Bearer test-management-key':
            self.reply(401, {'error': 'unauthorized'})
            return
        if self.path != '/v8/management/config/upstream/claude/header-defaults':
            self.reply(400, {'error': 'unsupported configuration path'})
            return
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.patches.append(payload)
        if self.server.write_status != 200:
            self.reply(self.server.write_status, {'error': 'test-management-key must never appear'})
            return
        if self.server.defaults is None:
            self.server.defaults = {}
        self.server.defaults.update(payload)
        self.reply(200, {'status': 'ok'})


class ClaudeClientConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ProxyHandler)
        self.server.defaults = None
        self.server.requests = []
        self.server.patches = []
        self.server.read_status = 200
        self.server.write_status = 200
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def configure(self):
        configure_t3_claude(self.base, 'test-management-key')

    def test_missing_settings_enable_t3_without_pinning_a_software_version(self):
        self.configure()
        self.assertEqual(self.server.defaults, {
            'native-entrypoints': ['t3-code'], 'stabilize-device-profile': False})
        self.assertEqual(len(self.server.patches), 1)

    def test_preserves_other_clients_and_existing_header_settings(self):
        self.server.defaults = {
            'native-entrypoints': ['custom-client'], 'timezone': 'Asia/Singapore',
            'anthropic-beta-append': ['test-feature'], 'user-agent': 'configured-baseline'}
        expected = deepcopy(self.server.defaults)
        expected['native-entrypoints'].append('t3-code')
        expected['stabilize-device-profile'] = False
        self.configure()
        self.assertEqual(self.server.defaults, expected)

    def test_repeated_deploy_does_not_rewrite_correct_configuration(self):
        self.configure()
        self.server.patches.clear()
        self.configure()
        self.assertEqual(self.server.patches, [])

    def test_existing_t3_entrypoint_needs_no_explicit_default_or_duplicate(self):
        self.server.defaults = {'native-entrypoints': ['T3-Code']}
        self.configure()
        self.assertEqual(self.server.patches, [])

    def test_disables_profile_pinning_for_following_the_installed_client(self):
        self.server.defaults = {
            'native-entrypoints': ['t3-code'], 'stabilize-device-profile': True}
        self.configure()
        self.assertIs(self.server.defaults['stabilize-device-profile'], False)

    def test_invalid_existing_settings_are_not_overwritten(self):
        for defaults in [[], {'native-entrypoints': 'cli'}, {'native-entrypoints': [1]},
                         {'stabilize-device-profile': 'false'}]:
            with self.subTest(defaults=defaults):
                self.server.defaults = defaults
                with self.assertRaises(ClaudeConfigurationError):
                    self.configure()
                self.assertEqual(self.server.defaults, defaults)
                self.assertEqual(self.server.patches, [])

    def test_failed_read_does_not_write_configuration(self):
        self.server.read_status = 503
        with self.assertRaises(ClaudeConfigurationError):
            self.configure()
        self.assertEqual(self.server.patches, [])

    def test_failed_write_does_not_leak_upstream_diagnostics(self):
        self.server.write_status = 400
        with self.assertRaises(ClaudeConfigurationError) as error:
            self.configure()
        self.assertNotIn('test-management-key', str(error.exception))
        self.assertIsNone(self.server.defaults)


if __name__ == '__main__':
    unittest.main()
