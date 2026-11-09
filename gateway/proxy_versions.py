"""Versions on the Settings page and the in-console CLIProxyAPI update.

What it reports:
- the running proxy's version, from the X-CPA-* headers on any management reply;
- the latest stock release on GitHub (cached; checked again on request);
- per provider (Claude, Codex): the installed client's version, the fallback version built
  into the proxy binary, and whether requests carry the client's own version upstream.

What it changes, only when the owner asks:
- Claude: registers T3's client identifier (claude_client_config), no restart.
- The proxy binary: download the stock release for this machine, check its SHA-256 against
  GitHub's asset digest and checksums.txt, check the new binary reports the expected version,
  back up the current one, swap it in, restart only the proxy service and wait until it
  answers with the new version. Any failure after the swap puts the old binary back.

Source builds (proxy/build.py, version "...+console") carry the weekly-reset overlay; a stock
release would drop it, so they are never replaced from here.
"""
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import hashlib
import io
import json
import mmap
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time

from claude_client_config import CONFIG_PATH as CLAUDE_DEFAULTS_PATH
from claude_client_config import ClaudeConfigurationError, configure_t3_claude, has_t3_entrypoint, native_entrypoints
from settings import PROXY_BASE, PROXY_HOST, PROXY_PORT

RELEASES_URL = 'https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest'
BINARY_NAME = 'cli-proxy-api'
CODEX_DEFAULTS_PATH = '/v8/management/config/oauth/providers/codex/header-defaults'
LATEST_TTL = 6 * 3600
CHECK_SPACING = 60
CLIENT_TTL = 300
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_BINARY = 400 * 1024 * 1024
MAX_JSON = 1024 * 1024
STOCK_VERSION = re.compile(r'\d+\.\d+\.\d+')
BUILTIN_PATTERNS = {'claude': re.compile(rb'claude-cli/(\d+\.\d+\.\d+)'),
                    'codex': re.compile(rb'codex-tui/(\d+\.\d+\.\d+)')}
CLIENT_DRIVERS = {'claude': ('claudeAgent', 'claude'), 'codex': ('codex', 'codex')}


class VersionError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def version_tuple(text):
    found = re.search(r'(\d+)\.(\d+)\.(\d+)', text or '')
    if found is None:
        return None
    return tuple(int(part) for part in found.groups())


def is_newer(candidate, current):
    candidate_parts, current_parts = version_tuple(candidate), version_tuple(current)
    if candidate_parts is None or current_parts is None:
        return False
    return candidate_parts > current_parts


def release_asset_name(version, system=None, machine=None):
    """Stock archive name, e.g. CLIProxyAPI_8.0.62_linux_amd64.tar.gz."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    if system not in ('linux', 'darwin') or arch is None:
        return None
    return f'CLIProxyAPI_{version}_{system}_{arch}.tar.gz'


def builtin_client_versions(binary_path):
    """The fallback client versions compiled into the proxy, read from the binary itself."""
    found = {}
    with open(binary_path, 'rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for provider, pattern in BUILTIN_PATTERNS.items():
            match = pattern.search(data)
            found[provider] = match.group(1).decode() if match else None
    return found


def http_get(url, timeout=20, limit=MAX_JSON):
    request = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'cliproxy-console'})
    with urlopen(request, timeout=timeout) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise VersionError(502, 'download_too_large')
    return content


def run_command(args, timeout, cwd=None):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                            stdin=subprocess.DEVNULL)
    return result.returncode, result.stdout + result.stderr


class ProxyVersions:
    def __init__(self, key, binary_path, unit, backup_dir, t3_settings, fetch=None, run=None, request=None,
                 restart_proxy=None, system=None, machine=None, sleep=time.sleep, proxy_base=PROXY_BASE):
        self.key = key
        self.binary_path = Path(binary_path) if binary_path else None
        self.unit = unit
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.t3_settings = Path(t3_settings)
        self.fetch = fetch or http_get
        self.run = run or run_command
        self.request = request or self._request
        self.restart_proxy = restart_proxy or self._restart_proxy
        self.system = system
        self.machine = machine
        self.sleep = sleep
        self.proxy_base = proxy_base
        self.lock = threading.Lock()
        self.latest_cache = None
        self.latest_checked_at = 0
        self.builtin_cache = (None, None)
        self.client_cache = {}
        self.job = {'phase': 'idle'}

    # ------------------------------------------------------------ proxy reads
    def _request(self, method, path, payload=None):
        connection = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=10)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            content = response.read(MAX_JSON + 1)
            headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, headers, content
        finally:
            connection.close()

    def running(self):
        """The proxy that answers right now, or None when it does not answer."""
        try:
            _, headers, _ = self.request('GET', '/v8/management/config/routing')
        except (OSError, HTTPException):
            # A stopped or restarting proxy has no version to report.
            return None
        version = headers.get('x-cpa-version')
        if not version:
            return None
        return {'version': version, 'commit': headers.get('x-cpa-commit'), 'built_at': headers.get('x-cpa-build-date'),
                'source_build': STOCK_VERSION.fullmatch(version) is None}

    def codex_pinned_agent(self):
        try:
            status, _, content = self.request('GET', CODEX_DEFAULTS_PATH)
        except (OSError, HTTPException):
            return None
        if status != 200:
            # 404 is how the proxy reports an unset optional block: no pin.
            return None
        try:
            defaults = json.loads(content)
        except ValueError:
            return None
        agent = defaults.get('user-agent') if isinstance(defaults, dict) else None
        if not isinstance(agent, str) or not agent.strip():
            return None
        return agent.strip()

    def claude_state(self):
        unknown = {'t3_recognized': None, 'stabilized': None, 'pinned_agent': None}
        try:
            status, _, content = self.request('GET', CLAUDE_DEFAULTS_PATH)
        except (OSError, HTTPException):
            return unknown
        if status not in (200, 404):
            return unknown
        try:
            # 404: nothing set, so only the built-in native clients keep their version.
            defaults = json.loads(content) if status == 200 else {}
            if not isinstance(defaults, dict):
                return unknown
            entrypoints = native_entrypoints(defaults)
        except (ValueError, ClaudeConfigurationError):
            return unknown
        agent = defaults.get('user-agent')
        return {'t3_recognized': has_t3_entrypoint(entrypoints),
                'stabilized': defaults.get('stabilize-device-profile') is True,
                'pinned_agent': agent.strip() if isinstance(agent, str) and agent.strip() else None}

    # ------------------------------------------------------- versions on disk
    def builtin(self):
        if self.binary_path is None:
            return {}
        try:
            stat = self.binary_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if self.builtin_cache[0] != signature:
                self.builtin_cache = (signature, builtin_client_versions(self.binary_path))
        except (OSError, ValueError):
            return {}
        return self.builtin_cache[1]

    def client_binary(self, provider):
        """The executable T3 runs for this provider, else the CLI on PATH."""
        driver, fallback = CLIENT_DRIVERS[provider]
        try:
            settings = json.loads(self.t3_settings.read_text())
            instances = settings.get('providerInstances', {})
        except (OSError, ValueError, AttributeError):
            instances = {}
        for instance in instances.values() if isinstance(instances, dict) else []:
            if not isinstance(instance, dict) or instance.get('driver') != driver or instance.get('enabled', True) is not True:
                continue
            binary = (instance.get('config') or {}).get('binaryPath')
            if isinstance(binary, str) and binary.strip():
                return str(Path(binary.strip()).expanduser())
        found = shutil.which(fallback, path=os.environ.get('PATH', '') + os.pathsep + str(Path.home() / '.local/bin'))
        return found

    def installed(self, provider):
        cached = self.client_cache.get(provider)
        if cached and time.time() - cached[0] < CLIENT_TTL:
            return cached[1]
        binary = self.client_binary(provider)
        result = {'version': None, 'binary': Path(binary).name if binary else None}
        if binary:
            try:
                # The bin/ wrappers hand --version straight to the real CLI.
                _, output = self.run([binary, '--version'], 15)
                parts = version_tuple(output)
                result['version'] = '.'.join(map(str, parts)) if parts else None
            except (OSError, subprocess.SubprocessError):
                pass
        self.client_cache[provider] = (time.time(), result)
        return result

    # ------------------------------------------------------------ GitHub
    def latest(self, force=False):
        now = time.time()
        fresh = self.latest_cache is not None and now - self.latest_checked_at < LATEST_TTL
        if fresh and not force:
            return self.latest_cache
        if force and now - self.latest_checked_at < CHECK_SPACING and self.latest_cache is not None:
            # GitHub allows 60 unauthenticated calls an hour; one check a minute is plenty.
            return self.latest_cache
        self.latest_checked_at = now
        try:
            release = json.loads(self.fetch(RELEASES_URL))
            tag = str(release.get('tag_name') or '').strip()
            version = tag.removeprefix('v')
            if STOCK_VERSION.fullmatch(version) is None:
                raise ValueError('unexpected tag')
            self.latest_cache = {'version': version, 'published_at': release.get('published_at'),
                                 'url': release.get('html_url'), 'checked_at': now, 'error': None,
                                 'assets': [asset for asset in release.get('assets', []) if isinstance(asset, dict)]}
        except (OSError, URLError, HTTPError, ValueError, AttributeError, VersionError):
            previous = self.latest_cache or {}
            self.latest_cache = {**previous, 'checked_at': now, 'error': 'latest_unavailable'}
        return self.latest_cache

    # ------------------------------------------------------------ overview
    def overview(self, force=False):
        running = self.running()
        latest = self.latest(force)
        builtin = self.builtin()
        claude = self.claude_state()
        pinned_codex = self.codex_pinned_agent()
        public_latest = {key: value for key, value in latest.items() if key != 'assets'}
        update_available = bool(running and latest.get('version') and not running['source_build']
                                and is_newer(latest['version'], running['version']))
        with self.lock:
            job = dict(self.job)
        return {
            'proxy': {'running': running, 'latest': public_latest, 'update_available': update_available,
                      'can_update': self.binary_path is not None and self.backup_dir is not None and bool(self.unit)},
            'update': job,
            'claude': {'installed': self.installed('claude'), 'builtin': builtin.get('claude'), **claude},
            'codex': {'installed': self.installed('codex'), 'builtin': builtin.get('codex'),
                      'pinned_agent': pinned_codex},
        }

    def recognize_t3(self):
        try:
            configure_t3_claude(self.proxy_base, self.key)
        except ClaudeConfigurationError:
            raise VersionError(502, 't3_setting_not_saved') from None
        return self.overview()

    # ------------------------------------------------------------ update
    def start_update(self, version):
        with self.lock:
            if self.job.get('phase') in ('downloading', 'verifying', 'installing', 'restarting'):
                raise VersionError(409, 'update_in_progress')
            if self.binary_path is None or self.backup_dir is None or not self.unit:
                raise VersionError(503, 'update_unavailable')
            running = self.running()
            if running is None:
                raise VersionError(502, 'proxy_unavailable')
            if running['source_build']:
                raise VersionError(409, 'source_build')
            latest = self.latest_cache or {}
            if latest.get('version') != version or not is_newer(version, running['version']):
                raise VersionError(409, 'not_latest')
            self.job = {'phase': 'downloading', 'from': running['version'], 'to': version,
                        'started_at': time.time(), 'finished_at': None, 'error': None}
            assets = list(latest.get('assets', []))
        threading.Thread(target=self._update, args=(running['version'], version, assets), daemon=True).start()
        return self.overview()

    def _phase(self, phase, **extra):
        with self.lock:
            self.job = {**self.job, 'phase': phase, **extra}

    def _finish(self, phase, error=None, **extra):
        self._phase(phase, error=error, finished_at=time.time(), **extra)

    def _update(self, current, version, assets):
        try:
            binary = self._download(version, assets)
            self._phase('verifying')
            staged = self._stage(binary, version)
        except VersionError as error:
            self._finish('failed', error.code)
            return
        except Exception:
            # Nothing was replaced yet; the running proxy is untouched.
            self._finish('failed', 'update_failed')
            return
        self._phase('installing')
        try:
            backup = self._backup(current)
        except OSError:
            staged.unlink(missing_ok=True)
            self._finish('failed', 'backup_failed')
            return
        os.replace(staged, self.binary_path)
        self._phase('restarting', backup=str(backup))
        if self._restart_and_wait(version):
            self._finish('done')
            return
        self._roll_back(backup, current)

    def _download(self, version, assets):
        name = release_asset_name(version, self.system, self.machine)
        if name is None:
            raise VersionError(409, 'unsupported_platform')
        archive = next((asset for asset in assets if asset.get('name') == name), None)
        if archive is None or not str(archive.get('browser_download_url', '')).startswith('https://github.com/'):
            raise VersionError(502, 'asset_missing')
        expected = set()
        digest = str(archive.get('digest') or '')
        if digest.startswith('sha256:'):
            expected.add(digest.removeprefix('sha256:').lower())
        checksums = next((asset for asset in assets if asset.get('name') == 'checksums.txt'), None)
        if checksums is not None and str(checksums.get('browser_download_url', '')).startswith('https://github.com/'):
            for line in self.fetch(checksums['browser_download_url']).decode().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip('*') == name:
                    expected.add(parts[0].lower())
        if len(expected) != 1:
            # No published checksum, or two sources that disagree: never install.
            raise VersionError(502, 'checksum_unavailable')
        content = self.fetch(archive['browser_download_url'], 120, MAX_ARCHIVE)
        if hashlib.sha256(content).hexdigest() != expected.pop():
            raise VersionError(502, 'checksum_mismatch')
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as bundle:
            member = next((item for item in bundle.getmembers() if item.name.lstrip('./') == BINARY_NAME), None)
            if member is None or not member.isfile() or member.size > MAX_BINARY:
                raise VersionError(502, 'binary_missing')
            return bundle.extractfile(member).read()

    def _stage(self, binary, version):
        staged = self.binary_path.with_name('.' + BINARY_NAME + '.update')
        with open(staged, 'wb') as handle:
            handle.write(binary)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, 0o700)
        try:
            _, output = self.run([str(staged), '-h'], 15, str(staged.parent))
        except (OSError, subprocess.SubprocessError):
            output = ''
        if f'CLIProxyAPI Version: {version},' not in output:
            staged.unlink(missing_ok=True)
            raise VersionError(502, 'binary_version_mismatch')
        return staged

    def _backup(self, current):
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        folder = self.backup_dir / f'proxy-{stamp}-{current}'
        folder.mkdir(parents=True, mode=0o700)
        target = folder / BINARY_NAME
        shutil.copy2(self.binary_path, target)
        return target

    def _restart_proxy(self):
        if platform.system() == 'Darwin':
            args = ['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{self.unit}']
        else:
            args = ['systemctl', '--user', 'restart', self.unit]
        code, _ = self.run(args, 120)
        return code == 0

    def _restart_and_wait(self, version):
        try:
            if not self.restart_proxy():
                return False
        except (OSError, subprocess.SubprocessError):
            return False
        for _ in range(60):
            running = self.running()
            if running is not None and running['version'] == version:
                return True
            self.sleep(1)
        return False

    def _roll_back(self, backup, current):
        restore = self.binary_path.with_name('.' + BINARY_NAME + '.restore')
        try:
            shutil.copy2(backup, restore)
            os.replace(restore, self.binary_path)
        except OSError:
            self._finish('failed', 'rollback_failed')
            return
        if self._restart_and_wait(current):
            self._finish('rolled_back', 'new_version_not_healthy')
            return
        self._finish('failed', 'rollback_failed')
