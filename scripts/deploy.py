#!/usr/bin/env python3
"""Install gateway/ into the live owner console and prove nothing else moved.

    scripts/deploy.py              deploy the committed tree
    scripts/deploy.py --dry-run    show what would change
    scripts/deploy.py --units      also render and install the console's systemd unit
    scripts/deploy.py --adopt      first deploy over live files that differ from the repo
    scripts/deploy.py --print-proxy-unit   print an example proxy unit for this config

Settings come from ~/.config/cliproxy-console/config.env (see config.env.example), or
the file named by $CLIPROXY_CONSOLE_CONFIG.

CONSOLE_DIR is a deploy target, not a checkout: it sits next to OAuth credentials,
the proxy config, logs and the usage database, none of which belong in git. A
manifest of what was installed (.deployed.json) lets the next deploy refuse to
overwrite edits made directly on the machine.
"""
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
# Linux (and WSL2) run the services under systemd --user; macOS under launchd user agents.
MAC = platform.system() == 'Darwin'
UNIT_DIR = Path.home() / ('Library/LaunchAgents' if MAC else '.config/systemd/user')
TEMPLATES = ({'console': 'launchd/console.plist.in', 'proxy': 'launchd/proxy.plist.in'} if MAC else
             {'console': 'systemd/console.service.in', 'proxy': 'systemd/proxy.service.in'})
DOMAIN = f'gui/{os.getuid()}'
REQUIRED = ['CLIPROXY_HOME', 'PROXY_PORT', 'PROXY_BINARY', 'PROXY_CONFIG', 'PROXY_LOG', 'MANAGEMENT_KEY_FILE',
            'CLIENT_KEY_FILE', 'CONSOLE_DIR', 'CONSOLE_PORT', 'QUOTA_SNAPSHOT', 'BACKUP_DIR', 'CONSOLE_ORIGIN',
            'CONSOLE_OWNER', 'PROXY_UNIT', 'CONSOLE_UNIT', 'PROTECTED_UNITS']


def load_config():
    """Source config.env with bash so $HOME and ${VAR} work as they do for the bin/ helpers."""
    path = Path(os.environ.get('CLIPROXY_CONSOLE_CONFIG',
                               Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'cliproxy-console/config.env'))
    if not path.is_file():
        raise SystemExit(f'No config at {path}. Copy config.env.example there and edit it.')
    names = ' '.join(REQUIRED)
    script = f'set -a; source "$1"; for n in {names}; do printf "%s=%s\\0" "$n" "${{!n}}"; done'
    output = subprocess.run(['bash', '-c', script, 'config', str(path)], check=True, capture_output=True, text=True).stdout
    config = dict(item.split('=', 1) for item in output.split('\0') if item)
    missing = [n for n in REQUIRED if not config.get(n)]
    if missing:
        raise SystemExit(f'{path} is missing: ' + ', '.join(missing))
    return config


CONFIG = load_config()
LIVE = Path(CONFIG['CONSOLE_DIR'])
MANIFEST = LIVE / '.deployed.json'
BACKUPS = Path(CONFIG['BACKUP_DIR'])
GATEWAY_UNIT = CONFIG['CONSOLE_UNIT']
# The proxy and anything that depends on it must never restart as a side effect of a console deploy.
UNTOUCHED = CONFIG['PROTECTED_UNITS'].split()
GATEWAY = f"http://127.0.0.1:{CONFIG['CONSOLE_PORT']}"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def unit_path(unit):
    return UNIT_DIR / (unit + '.plist' if MAC else unit)


def pid(unit):
    """Main PID of a user service, or '0' when it isn't running."""
    if MAC:
        printed = subprocess.run(['launchctl', 'print', f'{DOMAIN}/{unit}'], capture_output=True, text=True).stdout
        found = re.search(r'^\s*pid = (\d+)', printed, re.M)
        return found.group(1) if found else '0'
    return run('systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value')


def reload_unit(unit):
    """Make the service manager pick up a changed unit file."""
    if MAC:
        subprocess.run(['launchctl', 'bootout', f'{DOMAIN}/{unit}'], capture_output=True)  # not loaded yet is fine
        run('launchctl', 'bootstrap', DOMAIN, str(unit_path(unit)))
    else:
        run('systemctl', '--user', 'daemon-reload')


def restart(unit):
    if MAC:
        run('launchctl', 'kickstart', '-k', f'{DOMAIN}/{unit}')
    else:
        run('systemctl', '--user', 'restart', unit)


def sources():
    return {p.name: p for p in sorted((REPO / 'gateway').iterdir()) if p.suffix in ('.py', '.html')}


def render(kind):
    template = TEMPLATES[kind]
    text = (REPO / 'deploy' / template).read_text()
    text = text.replace('@REPO@', str(REPO)).replace('@PYTHON@', sys.executable)
    for name, value in CONFIG.items():
        text = text.replace(f'@{name}@', value)
    left = re.findall(r'@[A-Z_]+@', text)
    if left:
        raise SystemExit(f'{template}: unknown placeholders {left}')
    return text


def owner_headers():
    origin = re.fullmatch(r'https://([^/]+)', CONFIG['CONSOLE_ORIGIN'])
    if not origin:
        raise SystemExit('CONSOLE_ORIGIN must look like https://host[:port]')
    return {'Host': origin.group(1), 'Tailscale-User-Login': CONFIG['CONSOLE_OWNER'], 'Sec-Fetch-Site': 'same-origin'}


def get(path, headers):
    with urlopen(Request(GATEWAY + path, headers=headers), timeout=5) as response:
        return response.status, response.read()


def healthy(page_sha):
    headers = owner_headers()
    for _ in range(40):
        try:
            status, body = get('/v8/management/session', headers)
            if status == 200 and json.loads(body).get('authenticated') is True:
                break
        except Exception:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError('gateway did not come back')
    status, body = get('/management.html', headers)
    if status != 200 or hashlib.sha256(body).hexdigest() != page_sha:
        raise RuntimeError('gateway serves a different dashboard than the one installed')
    for route in ('/v8/management/dashboard/status', '/v8/management/dashboard/quota', '/v8/management/dashboard/live'):
        status, _ = get(route, headers)
        if status != 200:
            raise RuntimeError(f'{route} answered {status}')


def install(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.deploy-tmp')
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--units', action='store_true', help="install the console's service unit too (needed on first deploy)")
    parser.add_argument('--adopt', action='store_true', help='accept live files that differ from the repo on first deploy')
    parser.add_argument('--allow-dirty', action='store_true', help='deploy uncommitted changes (records -dirty)')
    parser.add_argument('--skip-tests', action='store_true')
    parser.add_argument('--print-proxy-unit', action='store_true', help='print an example proxy unit and exit')
    options = parser.parse_args()
    if options.print_proxy_unit:
        print(render('proxy'), end='')
        return

    dirty = run('git', '-C', str(REPO), 'status', '--porcelain')
    if dirty and not options.allow_dirty:
        raise SystemExit('Uncommitted changes; commit first or pass --allow-dirty.\n' + dirty)
    commit = run('git', '-C', str(REPO), 'rev-parse', 'HEAD') + ('-dirty' if dirty else '')

    if not options.skip_tests:
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=REPO,
                                env={**os.environ, 'PYTHONPATH': str(REPO / 'gateway')}, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit('Tests failed; nothing deployed.\n' + result.stderr[-4000:])

    files = sources()
    # Drift: the live copy must still be what the last deploy installed.
    if MANIFEST.exists():
        recorded = json.loads(MANIFEST.read_text())['files']
        drifted = [name for name, digest in recorded.items() if (LIVE / name).exists() and sha(LIVE / name) != digest]
    else:
        recorded = {}
        drifted = [] if options.adopt else [n for n, p in files.items() if (LIVE / n).exists() and sha(LIVE / n) != sha(p)]
    if drifted:
        raise SystemExit('Live files were edited outside git: ' + ', '.join(drifted) +
                         '\nCopy those edits into the repo (or rerun with --adopt on a first deploy) before deploying.')

    changed = [n for n, p in files.items() if not (LIVE / n).exists() or sha(LIVE / n) != sha(p)]
    unit_text = render('console')
    console_unit = unit_path(GATEWAY_UNIT)
    unit_changes = [] if console_unit.exists() and console_unit.read_text() == unit_text else [GATEWAY_UNIT]
    if unit_changes and not options.units:
        if not console_unit.exists():
            raise SystemExit(f'{console_unit} is not installed yet; run the first deploy with --units.')
        print(f'Note: {console_unit} differs from deploy/{TEMPLATES["console"]} (pass --units to install it).')
        unit_changes = []
    needs_restart = any(n.endswith('.py') for n in changed) or bool(unit_changes)

    print(f'commit {commit[:12]}  files: {", ".join(changed) or "none"}  unit: {", ".join(unit_changes) or "unchanged"}'
          f'  console restart: {"yes" if needs_restart else "no"}')
    if options.dry_run:
        return
    manifest = {'commit': commit, 'deployed_at': datetime.now(timezone.utc).isoformat(),
                'files': {n: sha(p) for n, p in files.items()}}
    if not changed and not unit_changes:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + '\n')
        os.chmod(MANIFEST, 0o600)
        print('Live console already matches; manifest updated.')
        return

    LIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = BACKUPS / f'deploy-{datetime.now():%Y%m%d-%H%M%S}-{commit[:8]}'
    backup.mkdir(parents=True, mode=0o700)
    for name in changed:
        if (LIVE / name).exists():
            shutil.copy2(LIVE / name, backup / name)
    if unit_changes and console_unit.exists():
        (backup / 'unit').mkdir()
        shutil.copy2(console_unit, backup / 'unit' / console_unit.name)

    before = {unit: pid(unit) for unit in UNTOUCHED}
    try:
        for name in changed:
            install(files[name], LIVE / name)
        if unit_changes:
            rendered = backup / 'rendered-unit'
            rendered.write_text(unit_text)
            install(rendered, console_unit)
            reload_unit(GATEWAY_UNIT)
        if needs_restart:
            restart(GATEWAY_UNIT)
        healthy(sha(files['dashboard.html']))
        after = {unit: pid(unit) for unit in UNTOUCHED}
        if after != before:
            raise RuntimeError(f'protected services changed PID: {before} -> {after}')
    except Exception as error:
        print(f'Deploy failed ({error}); restoring {backup}', file=sys.stderr)
        for name in changed:
            if (backup / name).exists():
                install(backup / name, LIVE / name)
            else:
                (LIVE / name).unlink(missing_ok=True)
        if unit_changes and (backup / 'unit' / console_unit.name).exists():
            install(backup / 'unit' / console_unit.name, console_unit)
            reload_unit(GATEWAY_UNIT)
        if needs_restart:
            restart(GATEWAY_UNIT)
        raise SystemExit(1)

    MANIFEST.write_text(json.dumps(manifest, indent=2) + '\n')
    os.chmod(MANIFEST, 0o600)
    print(f'Deployed {commit[:12]}. Backup: {backup}. Protected services kept their PIDs: {before}')


if __name__ == '__main__':
    main()
