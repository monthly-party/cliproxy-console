import json
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import usage_ledger
from usage_ledger import UsageLedger, parse_query, reduce_record

NOW = datetime(2026, 10, 11, 14, 26, 40, tzinfo=timezone.utc).timestamp()


def record(**overrides):
    base = {
        'timestamp': '2026-10-11T14:00:00.123456789Z', 'latency_ms': 4200, 'ttft_ms': 900,
        'auth_index': 'a1b2c3d4e5f60718', 'access_token_sha256': 'secret-hash', 'client_ip': '127.0.0.1',
        'x_forwarded_for': '100.1.2.3', 'user_agent': 'claude-cli/2.1.287 (external, cli)',
        'tokens': {'input_tokens': 10, 'output_tokens': 50, 'total_tokens': 1060},
        'token_breakdown': {'schema_version': 2, 'quality': 'exact', 'total_tokens': 1060,
                            'input': {'total_tokens': 1010, 'uncached_tokens': 10, 'cache_read_tokens': 900, 'cache_write_tokens': 100},
                            'output': {'total_tokens': 50, 'non_reasoning_tokens': 30, 'reasoning_tokens': 20}},
        'failed': False, 'stream': True, 'fail': {'status_code': 200, 'body': ''},
        'response_headers': {'Set-Cookie': ['x']}, 'provider': 'claude', 'model': 'claude-opus-5-5',
        'api_key': 'client-key', 'request_id': 'req-1', 'execution_id': 'exec-1', 'session_id': 'session-a',
        'reasoning_effort': 'high', 'service_tier': 'default'}
    base.update(overrides)
    return base


class Queue:
    def __init__(self, *batches):
        self.batches = list(batches)

    def __call__(self, key):
        assert key == 'k' * 32
        return self.batches.pop(0) if self.batches else []


class ReduceTests(unittest.TestCase):
    def test_keeps_counters_and_drops_sensitive_fields(self):
        row = reduce_record(record())
        self.assertEqual((row['input'], row['cache_read'], row['cache_write'], row['output'], row['reasoning'], row['total']),
                         (10, 900, 100, 50, 20, 1060))
        self.assertEqual(row['client'], 'Claude Code')
        self.assertEqual(len(row['session']), 16)
        flat = json.dumps(row)
        for secret in ('client-key', 'secret-hash', '100.1.2.3', 'Set-Cookie', 'session-a'):
            self.assertNotIn(secret, flat)

    def test_failure_keeps_status_not_body(self):
        row = reduce_record(record(failed=True, fail={'status_code': 429, 'body': 'upstream detail'}))
        self.assertEqual((row['failed'], row['status']), (1, 429))
        self.assertNotIn('upstream detail', json.dumps(row))

    def test_rejects_records_without_account_or_time(self):
        self.assertIsNone(reduce_record(record(auth_index='')))
        self.assertIsNone(reduce_record(record(auth_index='../x')))
        self.assertIsNone(reduce_record(record(timestamp='yesterday')))

    def test_falls_back_to_raw_counters(self):
        row = reduce_record(record(token_breakdown=None, tokens={'input_tokens': 5, 'output_tokens': 7}))
        self.assertEqual((row['input'], row['output'], row['total']), (5, 7, 12))

    def test_codex_client(self):
        self.assertEqual(usage_ledger.client_name('codex_cli_rs/0.159.2 (Ubuntu)'), 'Codex')
        self.assertEqual(usage_ledger.client_name('Mozilla/5.0'), 'Mozilla')
        self.assertEqual(usage_ledger.client_name('T3 Code/0.161.0 (Ubuntu 26.4.0; x86_64)'), 'T3 Code')
        self.assertEqual(usage_ledger.client_name(''), 'unknown')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'usage.db'

    def tearDown(self):
        self.dir.cleanup()

    def ledger(self, *batches):
        return UsageLedger('k' * 32, self.path, fetch=Queue(*batches), clock=lambda: NOW)

    def test_collector_and_dashboard_connections_close_without_gc(self):
        # Retain real connections so garbage collection cannot mask leaked handles.
        opened = []
        original = sqlite3.connect

        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            opened.append(db)
            return db

        try:
            with patch.object(usage_ledger.sqlite3, 'connect', side_effect=connect):
                ledger = self.ledger([record()])
                ledger.tick()
                ledger.remember_accounts([])
                for _ in range(20):
                    ledger.status()
                    ledger.accounts()
                    ledger.usage('24h')
                    ledger.requests()
                    ledger.quota_history('7d')
            for db in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute('select 1')
        finally:
            for db in opened:
                db.close()

    def test_failed_transaction_rolls_back_and_closes(self):
        ledger = self.ledger()
        with self.assertRaisesRegex(RuntimeError, 'failed write'):
            with ledger.connect() as db:
                db.execute("insert into meta values ('rollback-test', 'uncommitted')")
                raise RuntimeError('failed write')
        try:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('select 1')
            with ledger.connect() as check:
                self.assertIsNone(check.execute("select value from meta where key = 'rollback-test'").fetchone())
        finally:
            db.close()

    def test_setup_error_closes_connection(self):
        ledger = self.ledger()
        class BrokenSetup(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'pragma synchronous=normal':
                    raise sqlite3.OperationalError('setup failed')
                return super().execute(sql, *args)
        db = sqlite3.connect(self.path, factory=BrokenSetup)
        try:
            with patch.object(usage_ledger.sqlite3, 'connect', return_value=db):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'setup failed'):
                    with ledger.connect():
                        self.fail('setup should fail')
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('select 1')
        finally:
            db.close()

    def test_database_is_private(self):
        self.ledger()
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_drain_stores_and_dedupes(self):
        ledger = self.ledger([record(), record(execution_id='exec-2')], [record()])
        self.assertEqual(ledger.drain(), 2)
        self.assertEqual(ledger.drain(), 0)
        self.assertEqual(ledger.status()['stored_requests'], 2)

    def test_failed_commit_keeps_popped_records(self):
        ledger = self.ledger([record()])
        original = ledger._commit
        ledger._commit = lambda: (_ for _ in ()).throw(sqlite3.OperationalError('locked'))
        ledger.tick()
        self.assertEqual(len(ledger.pending), 1)
        self.assertEqual(ledger.last_error, 'OperationalError')
        ledger._commit = original
        ledger.drain()
        self.assertEqual((len(ledger.pending), ledger.status()['stored_requests']), (0, 1))

    def test_usage_groups_by_account_and_bucket(self):
        ledger = self.ledger([
            record(),
            record(execution_id='e2', timestamp='2026-10-11T13:10:00Z', session_id='session-b'),
            record(execution_id='e3', auth_index='f0e1d2c3b4a59687', provider='codex', model='gpt-6.1-sol',
                   user_agent='codex_cli_rs/0.159.2', failed=True, fail={'status_code': 429}),
            record(execution_id='old', timestamp='2026-10-01T00:00:00Z')])
        ledger.drain()
        usage = ledger.usage('24h', tz_minutes=420)
        self.assertEqual(usage['totals']['requests'], 3)
        self.assertEqual(usage['totals']['failed'], 1)
        self.assertEqual(usage['totals']['sessions'], 2)
        claude = next(a for a in usage['accounts'] if a['auth_index'] == 'a1b2c3d4e5f60718')
        self.assertEqual((claude['requests'], claude['total']), (2, 2120))
        self.assertEqual(len(claude['series']), 24)
        self.assertEqual(sum(p.get('total', 0) for p in claude['series']), 2120)
        self.assertEqual(sum(p['requests'] for p in usage['series']), 3)
        self.assertEqual({c['client'] for c in usage['clients']}, {'Claude Code', 'Codex'})
        self.assertEqual(usage['failures'], [{'status': 429, 'requests': 1}])
        self.assertEqual(claude['latency_p50_ms'], 4200)
        # 50 output tokens over (4200 - 900) ms of generation.
        self.assertEqual(claude['tps_p50'], 15.2)
        self.assertEqual(usage['tps_samples'], 2)

    def test_speed_rules(self):
        self.assertEqual(usage_ledger.speed(1000, 11000, 1000), 100.0)
        self.assertIsNone(usage_ledger.speed(10, 11000, 1000))          # too few tokens
        self.assertIsNone(usage_ledger.speed(1000, 11000, 1000, stream=0))
        self.assertIsNone(usage_ledger.speed(1000, 11000, 0))           # no first-token time
        self.assertIsNone(usage_ledger.speed(1000, 900, 1000))          # finished before first token
        self.assertIsNone(usage_ledger.speed(1000, 11000, 1000, failed=1))

    def test_requests_page(self):
        stamps = ['2026-10-11T14:00:03Z', '2026-10-11T14:00:01Z', '2026-10-11T14:00:05Z', '2026-10-11T14:00:02Z', '2026-10-11T14:00:04Z']
        ledger = self.ledger([record(execution_id=f'e{i}', timestamp=t) for i, t in enumerate(stamps)])
        ledger.drain()
        page = ledger.requests(limit=2)
        self.assertEqual(len(page['requests']), 2)
        cursor = parse_query('/dashboard/requests', {'before': page['next_before']})['before']
        rest = ledger.requests(limit=10, before=cursor)
        times = [r['ts'] for r in page['requests'] + rest['requests']]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(rest['requests']), 3)
        self.assertNotIn('execution_id', page['requests'][0])

    def test_accounts_keep_label_after_removal(self):
        ledger = self.ledger()
        ledger.remember_accounts([{'auth_index': 'a1', 'provider': 'claude', 'email': 'a@x'}])
        ledger.remember_accounts([])
        account = ledger.accounts()['a1']
        self.assertEqual(account['email'], 'a@x')
        self.assertIsNotNone(account['removed_at'])

    def test_quota_samples_append_once(self):
        ledger = self.ledger()
        snapshot = Path(self.dir.name) / 'snap.json'
        snapshot.write_text(json.dumps({'version': 1, 'accounts': {'a1': {'provider': 'claude', 'observed_at': '2026-10-11T14:00:00Z',
            'windows': [{'kind': 'weekly', 'bucket': 'seven_day', 'used_percent': 97.0, 'observed_at': '2026-10-11T14:00:00Z'}]}}}))
        self.assertEqual(ledger.sample_quota(snapshot), 1)
        self.assertEqual(ledger.sample_quota(snapshot), 0)
        history = ledger.quota_history('7d')
        self.assertEqual(history['windows'][0]['points'][0][1], 97.0)


class QueryTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_query('/dashboard/usage', {'range': '7d', 'tz': '-420'}), {'range_key': '7d', 'tz_minutes': -420})
        self.assertEqual(parse_query('/dashboard/requests', {'failed': '1', 'limit': '20'})['failed'], True)

    def test_invalid(self):
        for route, params in [('/dashboard/usage', {'range': '1y'}), ('/dashboard/usage', {'tz': '1e3'}),
                              ('/dashboard/requests', {'limit': '0'}), ('/dashboard/requests', {'before': '12;drop'}), ('/dashboard/requests', {'account': "x' or 1"}),
                              ('/dashboard/requests', {'sql': '1'}), ('/dashboard/quota', {'x': '1'}), ('/dashboard/other', {})]:
            with self.assertRaises(ValueError, msg=(route, params)):
                parse_query(route, params)


if __name__ == '__main__':
    unittest.main()
