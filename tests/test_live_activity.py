import tempfile
import time
import unittest
from pathlib import Path

from live_activity import LiveActivity

PICK = '[{t}] [{rid}] [info ] [selector.go:1067] session-affinity: {kind} | session={session} auth={auth} provider=mixed model={model}\n'
DONE = '[{t}] [{rid}] [info ] [gin_logger.go:108] 200 |        9.899s |       127.0.0.1 | POST    "/v1/messages?beta=true"\n'
CLAUDE = 'claude-9571da0c-do@example.com.json'
CODEX = 'codex-47f3d181-x@example.com-pro.json'


def wall(seconds_ago):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - seconds_ago))


class LiveActivityTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = Path(self.dir.name) / 'proxy.log'
        self.log.write_text('')

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text):
        with self.log.open('a') as out:
            out.write(text)

    def test_start_and_finish_pairs_by_request_id(self):
        live = LiveActivity(self.log)
        live.read()
        self.write(PICK.format(t=wall(20), rid='aaaaaaaa', kind='cache hit', session='claude:9abc', auth=CLAUDE, model='claude-opus-5-5'))
        self.write(PICK.format(t=wall(5), rid='bbbbbbbb', kind='cache miss, new binding', session='codex:01x', auth=CODEX, model='gpt-6.1-sol'))
        live.read()
        view = live.snapshot()
        self.assertEqual((view[CLAUDE]['in_flight'], view[CODEX]['in_flight']), (1, 1))
        self.assertTrue(view[CODEX]['requests'][0]['new_binding'])
        self.write(DONE.format(t=wall(1), rid='aaaaaaaa'))
        live.read()
        view = live.snapshot()
        self.assertEqual(view[CLAUDE]['in_flight'], 0)
        self.assertEqual(view[CLAUDE]['sessions'], 1)
        self.assertIsNotNone(view[CLAUDE]['last_finished_at'])

    def test_retry_moves_request_to_latest_account(self):
        live = LiveActivity(self.log)
        live.read()
        self.write(PICK.format(t=wall(9), rid='cccccccc', kind='cache hit', session='s1', auth=CLAUDE, model='m'))
        self.write(PICK.format(t=wall(8), rid='cccccccc', kind='fallback cache hit', session='s1', auth=CODEX, model='m'))
        live.read()
        view = live.snapshot()
        self.assertEqual(view.get(CLAUDE, {}).get('in_flight', 0), 0)
        self.assertTrue(view[CODEX]['requests'][0]['fallback'])

    def test_partial_line_waits_for_newline_and_stale_starts_expire(self):
        live = LiveActivity(self.log)
        live.read()
        line = PICK.format(t=wall(3), rid='dddddddd', kind='cache hit', session='s2', auth=CLAUDE, model='m')
        self.write(line[:40])
        live.read()
        self.assertEqual(live.snapshot(), {})
        self.write(line[40:])
        self.write(PICK.format(t=wall(3600), rid='eeeeeeee', kind='cache hit', session='old', auth=CODEX, model='m'))
        live.read()
        view = live.snapshot()
        self.assertEqual(view[CLAUDE]['in_flight'], 1)
        self.assertEqual(view.get(CODEX, {}).get('in_flight', 0), 0)

    def test_session_ids_are_not_exposed(self):
        live = LiveActivity(self.log)
        live.read()
        self.write(PICK.format(t=wall(2), rid='ffffffff', kind='cache hit', session='claude:secret-session', auth=CLAUDE, model='m'))
        live.read()
        self.assertNotIn('secret-session', repr(live.snapshot()))

    def test_truncated_log_restarts(self):
        live = LiveActivity(self.log)
        self.write('x' * 100 + '\n')
        live.read()
        self.log.write_text('')
        self.write(PICK.format(t=wall(2), rid='abababab', kind='cache hit', session='s', auth=CLAUDE, model='m'))
        live.read()
        self.assertEqual(live.snapshot()[CLAUDE]['in_flight'], 1)


if __name__ == '__main__':
    unittest.main()
