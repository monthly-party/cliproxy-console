"""bin/ wrappers find the real CLI, including when both sit in the same PATH directory."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / 'bin'


class RealCliLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {'HOME': str(self.tmp), 'CLIPROXY_CONSOLE_CONFIG': str(self.tmp / 'none.env')}

    def fake_cli(self, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(f'#!/bin/sh\necho "real {name} $*"\n')
        path.chmod(0o755)
        return path

    def run_wrapper(self, wrapper, path_dirs):
        env = {**self.env, 'PATH': ':'.join(str(d) for d in path_dirs) + ':/usr/bin:/bin'}
        return subprocess.run([str(wrapper), '--version'], capture_output=True, text=True, env=env, timeout=10)

    def test_finds_cli_next_to_the_linked_wrapper(self):
        # The usual install: claude and the claude-proxy link both live in ~/.local/bin.
        local_bin = self.tmp / '.local/bin'
        self.fake_cli(local_bin, 'claude')
        os.symlink(BIN / 'claude-proxy', local_bin / 'claude-proxy')
        done = self.run_wrapper(local_bin / 'claude-proxy', [local_bin])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), 'real claude --version')

    def test_skips_a_wrapper_linked_under_the_cli_name(self):
        shadow, real = self.tmp / 'shadow', self.tmp / 'real'
        shadow.mkdir()
        os.symlink(BIN / 'codex-proxy', shadow / 'codex')
        self.fake_cli(real, 'codex')
        done = self.run_wrapper(shadow / 'codex', [shadow, real])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), 'real codex --version')

    def test_reports_a_missing_cli(self):
        empty = self.tmp / 'empty'
        empty.mkdir()
        os.symlink(BIN / 'codex-proxy', empty / 'codex-proxy')
        done = self.run_wrapper(empty / 'codex-proxy', [empty])
        self.assertEqual(done.returncode, 127)
        self.assertIn('Cannot find the codex CLI', done.stderr)


if __name__ == '__main__':
    unittest.main()
