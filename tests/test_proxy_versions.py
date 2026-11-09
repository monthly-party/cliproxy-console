"""Proxy versions and the in-console update, with GitHub, the proxy and the service faked."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import unittest

import proxy_versions
from management_gateway_owner import validate_request
from proxy_versions import ProxyVersions, VersionError, builtin_client_versions, is_newer, release_asset_name

ARCHIVE_URL = 'https://github.com/router-for-me/CLIProxyAPI/releases/download/v8.0.70/CLIProxyAPI_8.0.70_linux_amd64.tar.gz'
CHECKSUMS_URL = 'https://github.com/router-for-me/CLIProxyAPI/releases/download/v8.0.70/checksums.txt'


def fake_binary(version):
    # Stands in for the Go binary: its -h output and the client versions compiled into it.
    return (f'#!/bin/sh\necho "CLIProxyAPI Version: {version}, Commit: abc, BuiltAt: now"\n'
            '# claude-cli/2.1.300 codex-tui/0.160.0\n').encode()


def archive_with(binary, name='cli-proxy-api'):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as bundle:
        info = tarfile.TarInfo(name)
        info.size = len(binary)
        info.mode = 0o755
        bundle.addfile(info, io.BytesIO(binary))
    return buffer.getvalue()


class FakeProxy:
    """Answers with the version of whichever binary is installed once 'restarted'."""

    def __init__(self, binary_path, version):
        self.binary_path = binary_path
        self.version = version
        self.restarts = 0
        self.healthy = True
        self.config = {}

    def request(self, method, path, payload=None):
        if self.version is None:
            raise OSError('connection refused')
        headers = {'x-cpa-version': self.version, 'x-cpa-commit': 'abc'}
        if path in self.config:
            return 200, headers, json.dumps(self.config[path]).encode()
        return 404, headers, b'{"error":"not_found"}'

    def restart(self):
        self.restarts += 1
        text = self.binary_path.read_text()
        installed = text.split('Version: ')[1].split(',')[0]
        self.version = installed if self.healthy or installed == '8.0.62' else None
        return True


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.binary_path = root / 'bin' / 'cli-proxy-api'
        self.binary_path.parent.mkdir()
        self.binary_path.write_bytes(fake_binary('8.0.62'))
        self.binary_path.chmod(0o700)
        self.backups = root / 'backups'
        self.proxy = FakeProxy(self.binary_path, '8.0.62')
        self.archive = archive_with(fake_binary('8.0.70'))
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.checksums = f'{self.digest}  CLIProxyAPI_8.0.70_linux_amd64.tar.gz\n'.encode()
        self.downloads = []

    def release(self):
        return {'tag_name': 'v8.0.70', 'published_at': '2026-11-12T00:00:00Z', 'html_url': 'https://github.com/x',
                'assets': [{'name': 'CLIProxyAPI_8.0.70_linux_amd64.tar.gz', 'browser_download_url': ARCHIVE_URL,
                            'digest': 'sha256:' + self.digest},
                           {'name': 'checksums.txt', 'browser_download_url': CHECKSUMS_URL}]}

    def fetch(self, url, timeout=20, limit=None):
        self.downloads.append(url)
        if url == proxy_versions.RELEASES_URL:
            return json.dumps(self.release()).encode()
        if url == CHECKSUMS_URL:
            return self.checksums
        if url == ARCHIVE_URL:
            return self.archive
        raise OSError('unexpected url')

    def fake_run(self, args, timeout, cwd=None):
        if args[-1] == '-h':
            return 2, Path(args[0]).read_text().split('echo "')[1].split('"')[0]
        return 0, ''

    def versions(self):
        return ProxyVersions('key', self.binary_path, 'cliproxy.service', self.backups, Path('/nonexistent'),
                             fetch=self.fetch, run=self.fake_run, request=self.proxy.request,
                             restart_proxy=self.proxy.restart, system='Linux', machine='x86_64',
                             sleep=lambda seconds: None, proxy_base='http://127.0.0.1:9')

    def update(self, versions):
        versions.latest(force=True)
        versions.start_update('8.0.70')
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(5)
        return versions.job

    def test_installs_verified_release_and_keeps_a_backup(self):
        job = self.update(self.versions())
        self.assertEqual(job['phase'], 'done', job)
        self.assertIn('8.0.70', self.binary_path.read_text())
        self.assertEqual(self.proxy.restarts, 1)
        backups = list(self.backups.glob('proxy-*-8.0.62/cli-proxy-api'))
        self.assertEqual(len(backups), 1)
        self.assertIn('8.0.62', backups[0].read_text())
        self.assertFalse(any(self.binary_path.parent.glob('.cli-proxy-api.*')))

    def test_checksum_mismatch_never_touches_the_proxy(self):
        self.archive = archive_with(fake_binary('8.0.70') + b'tampered')
        job = self.update(self.versions())
        self.assertEqual((job['phase'], job['error']), ('failed', 'checksum_mismatch'))
        self.assertIn('8.0.62', self.binary_path.read_text())
        self.assertEqual(self.proxy.restarts, 0)

    def test_sources_that_disagree_are_refused(self):
        self.checksums = ('0' * 64 + '  CLIProxyAPI_8.0.70_linux_amd64.tar.gz\n').encode()
        job = self.update(self.versions())
        self.assertEqual((job['phase'], job['error']), ('failed', 'checksum_unavailable'))
        self.assertEqual(self.proxy.restarts, 0)

    def test_binary_that_reports_another_version_is_refused(self):
        self.archive = archive_with(fake_binary('8.0.69'))
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.checksums = f'{self.digest}  CLIProxyAPI_8.0.70_linux_amd64.tar.gz\n'.encode()
        job = self.update(self.versions())
        self.assertEqual((job['phase'], job['error']), ('failed', 'binary_version_mismatch'))
        self.assertIn('8.0.62', self.binary_path.read_text())
        self.assertFalse(any(self.binary_path.parent.glob('.cli-proxy-api.*')))

    def test_unhealthy_new_version_rolls_back(self):
        self.proxy.healthy = False
        job = self.update(self.versions())
        self.assertEqual((job['phase'], job['error']), ('rolled_back', 'new_version_not_healthy'))
        self.assertIn('8.0.62', self.binary_path.read_text())
        self.assertEqual(self.proxy.version, '8.0.62')
        self.assertEqual(self.proxy.restarts, 2)

    def test_source_build_is_never_replaced(self):
        self.proxy.version = 'v8.0.10+console'
        versions = self.versions()
        versions.latest(force=True)
        with self.assertRaises(VersionError) as caught:
            versions.start_update('8.0.70')
        self.assertEqual(caught.exception.code, 'source_build')

    def test_only_the_checked_latest_release_can_be_installed(self):
        versions = self.versions()
        versions.latest(force=True)
        with self.assertRaises(VersionError) as caught:
            versions.start_update('8.0.71')
        self.assertEqual(caught.exception.code, 'not_latest')

    def test_overview_reports_versions_without_asset_details(self):
        versions = self.versions()
        view = versions.overview(force=True)
        self.assertEqual(view['proxy']['running']['version'], '8.0.62')
        self.assertEqual(view['proxy']['latest']['version'], '8.0.70')
        self.assertNotIn('assets', view['proxy']['latest'])
        self.assertTrue(view['proxy']['update_available'])
        self.assertEqual((view['claude']['builtin'], view['codex']['builtin']), ('2.1.300', '0.160.0'))
        self.assertIsNone(view['codex']['pinned_agent'])

    def test_claude_follows_client_once_t3_is_recognized(self):
        claude = '/v8/management/config/upstream/claude/header-defaults'
        self.assertFalse(self.versions().overview()['claude']['t3_recognized'])
        self.proxy.config[claude] = {'native-entrypoints': ['t3-code']}
        state = self.versions().overview()['claude']
        self.assertEqual((state['t3_recognized'], state['stabilized'], state['pinned_agent']), (True, False, None))
        self.proxy.config[claude] = {'native-entrypoints': ['t3-code'], 'stabilize-device-profile': True}
        self.assertTrue(self.versions().overview()['claude']['stabilized'])

    def test_reports_a_pinned_codex_user_agent(self):
        self.proxy.config[proxy_versions.CODEX_DEFAULTS_PATH] = {'user-agent': 'codex_cli_rs/0.150.0'}
        self.assertEqual(self.versions().overview()['codex']['pinned_agent'], 'codex_cli_rs/0.150.0')

    def test_github_is_asked_at_most_once_a_minute(self):
        versions = self.versions()
        versions.latest(force=True)
        versions.latest(force=True)
        versions.latest()
        self.assertEqual(self.downloads.count(proxy_versions.RELEASES_URL), 1)


class HelperTests(unittest.TestCase):
    def test_versions_compare_numerically(self):
        self.assertTrue(is_newer('8.0.100', '8.0.62'))
        self.assertFalse(is_newer('8.0.62', '8.0.62'))
        self.assertFalse(is_newer('8.0.62', 'unknown'))

    def test_asset_names_per_platform(self):
        self.assertEqual(release_asset_name('8.0.62', 'Linux', 'x86_64'), 'CLIProxyAPI_8.0.62_linux_amd64.tar.gz')
        self.assertEqual(release_asset_name('8.0.62', 'Darwin', 'arm64'), 'CLIProxyAPI_8.0.62_darwin_arm64.tar.gz')
        self.assertIsNone(release_asset_name('8.0.62', 'Windows', 'x86_64'))

    def test_reads_builtin_client_versions_from_binary(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(fake_binary('8.0.62'))
            handle.flush()
            self.assertEqual(builtin_client_versions(handle.name), {'claude': '2.1.300', 'codex': '0.160.0'})


class RouteTests(unittest.TestCase):
    def test_accepts_exact_routes(self):
        self.assertEqual(validate_request('GET', '/v8/management/proxy-versions', b''), '/v8/management/proxy-versions')
        for route in ('check', 'recognize-t3'):
            path = '/v8/management/proxy-versions/' + route
            self.assertEqual(validate_request('POST', path, b''), path)
        self.assertEqual(validate_request('POST', '/v8/management/proxy-update', b'{"version": "8.0.70"}'),
                         '/v8/management/proxy-update')

    def test_refuses_anything_else(self):
        for method, path, body in [
            ('POST', '/v8/management/proxy-update', b'{"version": "latest"}'),
            ('POST', '/v8/management/proxy-update', b'{"version": "8.0.70", "url": "https://x"}'),
            ('POST', '/v8/management/proxy-update', b'{}'),
            ('GET', '/v8/management/proxy-update', b''),
            ('GET', '/v8/management/proxy-versions?refresh=1', b''),
            ('POST', '/v8/management/proxy-versions/check', b'{"force": true}'),
            ('POST', '/v8/management/proxy-versions/restart', b''),
        ]:
            with self.assertRaises(ValueError, msg=(method, path, body)):
                validate_request(method, path, body)


if __name__ == '__main__':
    unittest.main()
