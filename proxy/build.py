#!/usr/bin/env python3
"""Build CLIProxyAPI with this project's patches and weekly-reset overlay.

    proxy/build.py                       download, verify, patch, test, build
    proxy/build.py --archive FILE        use an already downloaded source tarball
    proxy/build.py --out PATH --go GO    choose the output binary and Go toolchain

Steps: fetch the pinned upstream commit (proxy/upstream.json) and check its sha256;
check that every file the overlay replaces still matches the upstream hash it was
written against (proxy/overlay-manifest.json); apply proxy/patches/*.patch; copy
proxy/overlay/ over the tree; run the weekly-reset and routing tests; build a static
binary. Nothing outside --work and --out is touched, and nothing is installed.
"""
from pathlib import Path
from urllib.request import urlopen
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def step(label, args, cwd, env):
    print(f'== {label}', flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, default=HERE / '.build')
    parser.add_argument('--archive', type=Path, help='source tarball already on disk')
    parser.add_argument('--out', type=Path, default=HERE / '.build/cli-proxy-api')
    parser.add_argument('--go', default='go')
    parser.add_argument('--skip-tests', action='store_true')
    options = parser.parse_args()
    upstream = json.loads((HERE / 'upstream.json').read_text())
    options.work.mkdir(parents=True, exist_ok=True)

    archive = options.archive or options.work / f"{upstream['commit']}.tar.gz"
    if not archive.exists():
        print(f"== download {upstream['source_url']}", flush=True)
        with urlopen(upstream['source_url'], timeout=120) as response, open(archive, 'wb') as out:
            shutil.copyfileobj(response, out)
    if sha256(archive) != upstream['source_sha256']:
        raise SystemExit(f'{archive} does not match the pinned sha256; refusing to build.')

    source = options.work / 'source'
    if source.exists():
        shutil.rmtree(source)
    with tarfile.open(archive) as tar:
        top = tar.getmembers()[0].name.split('/')[0]
        tar.extractall(options.work, filter='data')
    (options.work / top).rename(source)

    manifest = json.loads((HERE / 'overlay-manifest.json').read_text())
    for relative, item in manifest.items():
        if sha256(source / relative) != item['upstream_sha256']:
            raise SystemExit(f'Upstream {relative} changed since the overlay was written; port the overlay first.')

    env = {**os.environ, 'CGO_ENABLED': '0', 'GOFLAGS': '-mod=readonly'}
    for patch in sorted((HERE / 'patches').glob('*.patch')):
        step(f'patch {patch.name}', ['patch', '-p1', '--forward', '--input', str(patch)], source, env)
    for file in (HERE / 'overlay').rglob('*'):
        if file.is_file():
            target = source / file.relative_to(HERE / 'overlay')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)

    if not options.skip_tests:
        step('weekly-reset tests', [options.go, 'test', '-count=1', './sdk/cliproxy/auth', '-run', '^TestWeeklyReset'], source, env)
        step('routing regression tests', [options.go, 'test', '-count=1', './sdk/cliproxy/auth', '-run',
                                          'TestSessionAffinity|TestRoundRobin|TestFillFirst|TestBuiltInSelector'], source, env)
    version = f"{upstream['tag']}+console"
    step('build', [options.go, 'build', '-trimpath', '-buildvcs=false',
                   '-ldflags', f"-X main.Version={version} -X main.Commit={upstream['commit']}",
                   '-o', str(options.out.resolve()), './cmd/server'], source, env)
    print(f'Built {options.out} ({version}), sha256 {sha256(options.out)}')


if __name__ == '__main__':
    main()
