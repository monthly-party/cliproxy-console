#!/usr/bin/env python3
"""Check a machine for cliproxy-console, before and after installing.

    scripts/doctor.py --stage pre    tools, platform, Tailscale (no config needed)
    scripts/doctor.py                everything: config, files, proxy, usage stats, console

Each line is PASS, WARN or FAIL with what to do next. Exit code 1 when anything
FAILs. Read-only: it never changes files, services or accounts, and never prints keys.
"""
from pathlib import Path
from urllib.request import Request, urlopen
import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
results = []


def report(level, check, detail=''):
    results.append(level)
    print(f'{level:<5} {check}' + (f'  {detail}' if detail else ''))


def run(*args):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return done.returncode, done.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ''


def tailscale_cli():
    for candidate in (shutil.which('tailscale'), '/Applications/Tailscale.app/Contents/MacOS/Tailscale'):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def pre():
    system = platform.system()
    if system == 'Linux':
        code, _ = run('systemctl', '--user', 'show-environment')
        report('PASS' if code == 0 else 'FAIL', 'service manager',
               'systemd --user' if code == 0 else 'systemd --user is not available (on WSL2 enable systemd in /etc/wsl.conf)')
    elif system == 'Darwin':
        report('PASS' if shutil.which('launchctl') else 'FAIL', 'service manager', 'launchd')
    else:
        report('FAIL', 'platform', f'{system} is not supported; use Linux, WSL2 with systemd, or macOS')

    version = sys.version_info
    report('PASS' if version >= (3, 9) else 'FAIL', 'python', f'{platform.python_version()} at {sys.executable}'
           + ('' if version >= (3, 9) else '; install Python 3.9 or newer'))
    for tool in ('git', 'bash', 'openssl'):
        report('PASS' if shutil.which(tool) else 'FAIL', tool, shutil.which(tool) or 'not found')
    report('PASS' if shutil.which('patch') else 'WARN', 'patch', shutil.which('patch') or 'needed only to build the proxy')
    code, out = run('go', 'version')
    found = re.search(r'go(\d+)\.(\d+)', out or '')
    if code == 0 and found and (int(found[1]), int(found[2])) >= (1, 21):
        report('PASS', 'go', out + ' (fetches the exact toolchain go.mod asks for)')
    else:
        report('WARN', 'go', 'Go 1.21+ is needed only to build the proxy from source; otherwise use a release binary')

    cli = tailscale_cli()
    if not cli:
        report('FAIL', 'tailscale', 'not installed; see https://tailscale.com/download')
        return None
    code, out = run(cli, 'status', '--json')
    try:
        status = json.loads(out)
    except ValueError:
        report('FAIL', 'tailscale', 'cannot read `tailscale status --json`; is it logged in?')
        return None
    if status.get('BackendState') != 'Running':
        report('FAIL', 'tailscale', f"state {status.get('BackendState')}; run `tailscale up`")
        return None
    self_node = status.get('Self') or {}
    host = (self_node.get('DNSName') or '').rstrip('.')
    login = ((status.get('User') or {}).get(str(self_node.get('UserID'))) or {}).get('LoginName', '')
    report('PASS', 'tailscale', f'{host} as {login}')
    if status.get('CertDomains'):
        report('PASS', 'tailscale https', 'certificates enabled')
    else:
        report('FAIL', 'tailscale https', 'enable MagicDNS and HTTPS certificates in the Tailscale admin console (DNS page)')
    return {'host': host, 'login': login, 'cli': cli}


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(('127.0.0.1', int(port))) == 0


def get(url, headers, timeout=5):
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        return response.status, response.read()


def post(tail):
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        import deploy  # loads and validates config.env
    except SystemExit as error:
        report('FAIL', 'config.env', str(error))
        return
    config = deploy.CONFIG
    report('PASS', 'config.env', 'all settings present')
    if tail and tail['host'] and tail['host'] not in config['CONSOLE_ORIGIN']:
        report('WARN', 'CONSOLE_ORIGIN', f"{config['CONSOLE_ORIGIN']} does not name this machine ({tail['host']})")
    if tail and tail['login'] and tail['login'] != config['CONSOLE_OWNER']:
        report('WARN', 'CONSOLE_OWNER', f"{config['CONSOLE_OWNER']} is not the Tailscale login on this machine ({tail['login']})")

    for name in ('MANAGEMENT_KEY_FILE', 'CLIENT_KEY_FILE', 'PROXY_CONFIG'):
        path = Path(config[name])
        if not path.exists():
            report('FAIL', name, f'{path} missing')
        elif path.stat().st_mode & 0o077:
            report('FAIL', name, f'{path} is readable by others; chmod 600 it')
        else:
            report('PASS', name, str(path))
    binary = Path(config['PROXY_BINARY'])
    report('PASS' if binary.exists() and os.access(binary, os.X_OK) else 'FAIL', 'PROXY_BINARY', str(binary))

    if not listening(config['PROXY_PORT']):
        report('FAIL', 'proxy', f"nothing listens on 127.0.0.1:{config['PROXY_PORT']}; check `{config['PROXY_UNIT']}`")
    else:
        try:
            client = Path(config['CLIENT_KEY_FILE']).read_text().strip()
            status, _ = get(f"http://127.0.0.1:{config['PROXY_PORT']}/v1/models", {'Authorization': 'Bearer ' + client})
            report('PASS', 'proxy', f"answers /v1/models on port {config['PROXY_PORT']}")
        except Exception as error:
            report('FAIL', 'proxy', f'/v1/models with the client key failed ({type(error).__name__}); keys or config mismatch')
        try:
            management = {'Authorization': 'Bearer ' + Path(config['MANAGEMENT_KEY_FILE']).read_text().strip()}
            base = f"http://127.0.0.1:{config['PROXY_PORT']}/v8/management"
            _, body = get(base + '/config/observability/usage/usage-statistics-enabled', management)
            enabled = json.loads(body) is True
            report('PASS' if enabled else 'FAIL', 'usage statistics',
                   'enabled' if enabled else 'set observability.usage.usage-statistics-enabled: true')
            _, body = get(base + '/credentials', management)
            files = [f for f in json.loads(body).get('files', []) if f.get('provider', f.get('type')) in ('claude', 'anthropic', 'codex')]
            report('PASS' if files else 'WARN', 'accounts', f'{len(files)} Claude/Codex account(s)' if files else 'none yet; add them from the console')
        except Exception as error:
            report('FAIL', 'management API', f'failed with the management key ({type(error).__name__})')

    log = Path(config['PROXY_LOG'])
    report('PASS' if log.exists() else 'WARN', 'PROXY_LOG', str(log) if log.exists()
           else f'{log} missing; the proxy service must write stdout/stderr there for the live view')
    if not listening(config['CONSOLE_PORT']):
        report('FAIL', 'console', f"nothing listens on 127.0.0.1:{config['CONSOLE_PORT']}; run `scripts/deploy.py --units`")
    else:
        try:
            deploy.healthy(deploy.sha(REPO / 'gateway/dashboard.html'))
            report('PASS', 'console', 'page and API healthy for the configured owner')
        except Exception as error:
            report('FAIL', 'console', str(error))
    if tail:
        code, out = run(tail['cli'], 'serve', 'status')
        port = re.search(r':(\d+)$', config['CONSOLE_ORIGIN'])
        served = f"127.0.0.1:{config['CONSOLE_PORT']}" in out and (not port or f":{port[1]}" in out)
        report('PASS' if served else 'FAIL', 'tailscale serve', config['CONSOLE_ORIGIN'] if served else
               f"not serving the console; run `tailscale serve --bg --https={port[1] if port else 443} http://127.0.0.1:{config['CONSOLE_PORT']}`")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--stage', choices=('pre', 'all'), default='all')
    options = parser.parse_args()
    tail = pre()
    if options.stage == 'all':
        post(tail)
    failed = results.count('FAIL')
    print(f'\n{failed} failed, {results.count("WARN")} warnings, {results.count("PASS")} passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
