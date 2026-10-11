#!/usr/bin/env python3
"""Write $PROXY_CONFIG from deploy/config.example.yaml, config.env and the key files.

    scripts/make-proxy-config.py            weekly-reset routing (proxy built with proxy/build.py)
    scripts/make-proxy-config.py --stock    round-robin routing (stock CLIProxyAPI release)
    scripts/make-proxy-config.py --force    overwrite an existing config

Keys are read from MANAGEMENT_KEY_FILE and CLIENT_KEY_FILE and written straight into
the config file (mode 600). Nothing is printed except the path written. The proxy
replaces the plaintext management key with a bcrypt hash on its first start.
"""
from pathlib import Path
import argparse
import os
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import CONFIG, REPO  # noqa: E402  (loads and validates config.env)


def key(path):
    value = Path(path).read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value):
        raise SystemExit(f'{path} should hold one key of 32+ letters, digits, _ or -; generate one with `openssl rand -hex 32`.')
    if Path(path).stat().st_mode & 0o077:
        raise SystemExit(f'{path} must be private: chmod 600 {path}')
    return value


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise SystemExit(f'deploy/config.example.yaml changed shape; expected one "{old.strip()}".')
    return text.replace(old, new)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--stock', action='store_true', help='stock CLIProxyAPI without the weekly-reset overlay')
    parser.add_argument('--force', action='store_true', help='overwrite an existing config file')
    options = parser.parse_args()
    target = Path(CONFIG['PROXY_CONFIG'])
    if target.exists() and not options.force:
        raise SystemExit(f'{target} already exists; pass --force to overwrite it (the old one is not backed up).')

    text = (REPO / 'deploy/config.example.yaml').read_text()
    text = text.replace('/home/you/.local/share/cliproxy/auth', str(Path(CONFIG['CLIPROXY_HOME']) / 'auth'))
    text = replace_once(text, '/home/you/.local/share/cliproxy/console/quota-snapshot.json', CONFIG['QUOTA_SNAPSHOT'])
    text = replace_once(text, '    port: 8317\n', f"    port: {int(CONFIG['PROXY_PORT'])}\n")
    text = replace_once(text, '"<client key>"', '"' + key(CONFIG['CLIENT_KEY_FILE']) + '"')
    text = re.sub(r'secret-key: "<management key[^"]*>"', 'secret-key: "' + key(CONFIG['MANAGEMENT_KEY_FILE']) + '"', text, count=1)
    if options.stock:
        text = replace_once(text, '    strategy: "weekly-reset"\n', '    strategy: "round-robin"\n')
        text = re.sub(r'    weekly-reset-snapshot: "[^"]*"\n', '', text, count=1)
    text = re.sub(r'\A(#[^\n]*\n)+', '', text)
    if '<' in text and re.search(r'"<[^>]+>"', text):
        raise SystemExit('A placeholder was left unfilled; not writing the config.')

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(target.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as out:
        out.write(text)
    os.replace(temporary, target)
    print(f'Wrote {target} ({"round-robin" if options.stock else "weekly-reset"} routing).')


if __name__ == '__main__':
    main()
