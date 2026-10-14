"""Durable per-account token ledger fed by the proxy's usage queue.

The proxy keeps usage records in memory for at most an hour and the queue pop is
destructive, so this gateway is the only consumer. Each record is reduced to
counters and routing metadata before it touches disk: no API key, client IP,
forwarded-for chain, response headers, access-token hash or failure body.
Session ids are stored as a short one-way hash, enough to count sessions.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
from pathlib import Path
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time

from settings import PROXY_HOST, PROXY_PORT
from pricing import RateTable

QUEUE_PATH = '/v8/management/observability/usage/queue?count=500'
MAX_BYTES = 8 * 1024 * 1024
RETENTION_DAYS = 400
RANGES = {'24h': (86400, 3600), '7d': (7 * 86400, 6 * 3600),
          '30d': (30 * 86400, 86400), '90d': (90 * 86400, 86400)}
TOKEN_FIELDS = ('input', 'cache_read', 'cache_write', 'output', 'reasoning', 'total')
COST_FIELDS = ('input', 'cache_read', 'cache_write', 'output')
NO_RATES = RateTable({})

SCHEMA = """
create table if not exists requests (
  id integer primary key,
  execution_id text not null unique,
  ts real not null,
  auth_index text not null,
  provider text not null,
  model text not null,
  response_model text,
  endpoint text,
  client text not null,
  session text,
  input integer not null default 0,
  cache_read integer not null default 0,
  cache_write integer not null default 0,
  output integer not null default 0,
  reasoning integer not null default 0,
  total integer not null default 0,
  latency_ms integer,
  ttft_ms integer,
  failed integer not null default 0,
  status integer,
  stream integer not null default 0,
  reasoning_effort text,
  service_tier text
);
create index if not exists requests_ts on requests(ts);
create index if not exists requests_account_ts on requests(auth_index, ts);
create table if not exists accounts (
  auth_index text primary key,
  provider text not null,
  email text,
  first_seen real not null,
  last_seen real not null,
  removed_at real
);
create table if not exists quota_samples (
  auth_index text not null,
  bucket text not null,
  kind text not null,
  model text,
  used_percent real not null,
  reset_at text,
  observed_at real not null,
  primary key (auth_index, bucket, observed_at)
);
create index if not exists quota_samples_ts on quota_samples(observed_at);
create table if not exists meta (key text primary key, value text not null);
"""


def epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        # Go emits nanoseconds; Python accepts at most microseconds.
        trimmed = re.sub(r'(\.\d{6})\d+', r'\1', value.replace('Z', '+00:00'))
        parsed = datetime.fromisoformat(trimmed)
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


def count(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else 0


def text(value, limit=128):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def client_name(user_agent):
    """Name the calling tool from its User-Agent product token only."""
    agent = (user_agent or '').strip().lower()
    if agent.startswith('claude-cli') or 'claude-code' in agent:
        return 'Claude Code'
    if agent.startswith('codex') or 'codex_cli' in agent or 'codex_exec' in agent:
        return 'Codex'
    # Keep the product name as the client wrote it, e.g. "T3 Code/0.161.0 (...)" -> "T3 Code".
    product = re.match(r'([A-Za-z0-9][A-Za-z0-9 ._-]{0,39}?)\s*(?:/|\(|$)', (user_agent or '').strip())
    return product.group(1).strip() if product else 'unknown'


def tokens(record):
    """Prefer the proxy's non-overlapping v2 breakdown; fall back to raw counters."""
    breakdown = record.get('token_breakdown')
    if isinstance(breakdown, dict) and isinstance(breakdown.get('input'), dict) and isinstance(breakdown.get('output'), dict):
        source, sink = breakdown['input'], breakdown['output']
        result = {'input': count(source.get('uncached_tokens')), 'cache_read': count(source.get('cache_read_tokens')),
                  'cache_write': count(source.get('cache_write_tokens')), 'output': count(sink.get('total_tokens')),
                  'reasoning': count(sink.get('reasoning_tokens')), 'total': count(breakdown.get('total_tokens'))}
        if result['total'] or any(result[k] for k in ('input', 'cache_read', 'cache_write', 'output')):
            return result
    raw = record.get('tokens') if isinstance(record.get('tokens'), dict) else {}
    result = {'input': count(raw.get('input_tokens')), 'cache_read': count(raw.get('cache_read_tokens')),
              'cache_write': count(raw.get('cache_creation_tokens')), 'output': count(raw.get('output_tokens')),
              'reasoning': count(raw.get('reasoning_tokens')), 'total': count(raw.get('total_tokens'))}
    if not result['total']:
        result['total'] = result['input'] + result['cache_read'] + result['cache_write'] + result['output']
    return result


def reduce_record(record):
    """Return the stored row for one queue record, or None when it is unusable."""
    if not isinstance(record, dict):
        return None
    stamp = epoch(record.get('timestamp'))
    auth_index = text(record.get('auth_index'), 256)
    if stamp is None or not auth_index or not re.fullmatch(r'[a-zA-Z0-9_-]{1,256}', auth_index):
        return None
    identity = text(record.get('execution_id'), 128) or text(record.get('request_id'), 128)
    if not identity:
        identity = hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()
    provider = text(record.get('provider'), 32) or 'unknown'
    provider = {'anthropic': 'claude', 'openai': 'codex'}.get(provider, provider)
    session = text(record.get('session_id'), 128)
    fail = record.get('fail') if isinstance(record.get('fail'), dict) else {}
    failed = record.get('failed') is True
    status = fail.get('status_code') if type(fail.get('status_code')) is int else None
    row = {'execution_id': identity, 'ts': stamp, 'auth_index': auth_index, 'provider': provider,
           'model': text(record.get('model')) or 'unknown', 'response_model': text(record.get('response_model')),
           'endpoint': text(record.get('endpoint'), 96), 'client': client_name(record.get('user_agent')),
           'session': hashlib.sha256(session.encode()).hexdigest()[:16] if session else None,
           'latency_ms': count(record.get('latency_ms')) or None, 'ttft_ms': count(record.get('ttft_ms')) or None,
           'failed': int(failed), 'status': status if failed else (status or 200),
           'stream': int(record.get('stream') is True),
           'reasoning_effort': text(record.get('reasoning_effort'), 32), 'service_tier': text(record.get('service_tier'), 32)}
    row.update(tokens(record))
    return row


def fetch_queue(key):
    connection = HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=15)
    try:
        connection.request('GET', QUEUE_PATH, headers={'Authorization': 'Bearer ' + key})
        response = connection.getresponse()
        body = response.read(MAX_BYTES + 1)
        if response.status != 200 or len(body) > MAX_BYTES:
            raise ValueError('Usage queue unavailable')
        items = json.loads(body)
        if not isinstance(items, list):
            raise ValueError('Unexpected usage queue shape')
        return [json.loads(item) if isinstance(item, str) else item for item in items]
    except (OSError, HTTPException):
        raise ValueError('Usage queue unavailable') from None
    finally:
        connection.close()


class UsageLedger:
    """Owns the SQLite file. One collector thread writes; request threads read."""

    def __init__(self, key, path, fetch=None, clock=None, rates=None):
        self.key = key
        self.path = Path(path)
        self.fetch = fetch or fetch_queue
        self.clock = clock or time.time
        # Prices are applied when read, so a newer rate table re-prices all of history.
        self.rates = rates
        self.lock = threading.Lock()
        self.pending = []
        self.last_poll_at = None
        self.last_error = None
        self.last_prune = 0
        self.last_quota_mtime = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("insert or ignore into meta(key, value) values ('started_at', ?)", (str(self.clock()),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute('pragma journal_mode=wal')
            db.execute('pragma synchronous=normal')
            with db:
                yield db
        finally:
            # SQLite's transaction context commits/rolls back but does not close.
            # Close explicitly so collectors and dashboard reads cannot exhaust FDs.
            db.close()

    # ------------------------------------------------------------- collection

    def drain(self):
        """Pop until the queue is empty. Popped records stay in memory until committed.

        Only the collector thread calls this, so the network read happens outside the
        lock; request threads saving account labels never wait on the proxy.
        """
        stored = 0
        for _ in range(40):
            batch = self.fetch(self.key)
            with self.lock:
                self.pending.extend(batch)
                if self.pending:
                    stored += self._commit()
            if len(batch) < 500:
                break
        self.last_poll_at = self.clock()
        self.last_error = None
        return stored

    def _commit(self):
        rows = [row for row in map(reduce_record, self.pending) if row is not None]
        columns = ('execution_id', 'ts', 'auth_index', 'provider', 'model', 'response_model', 'endpoint', 'client',
                   'session', *TOKEN_FIELDS, 'latency_ms', 'ttft_ms', 'failed', 'status', 'stream',
                   'reasoning_effort', 'service_tier')
        with self.connect() as db:
            inserted = db.executemany(f"insert or ignore into requests({', '.join(columns)}) values ({', '.join('?' * len(columns))})",
                                      [tuple(row[c] for c in columns) for row in rows]).rowcount
            for row in rows:
                db.execute('insert into accounts(auth_index, provider, first_seen, last_seen) values (?, ?, ?, ?) '
                           'on conflict(auth_index) do update set last_seen = max(last_seen, excluded.last_seen)',
                           (row['auth_index'], row['provider'], row['ts'], row['ts']))
        self.pending = []
        return max(0, inserted)

    def remember_accounts(self, files):
        """Keep labels for accounts so history stays readable after removal."""
        now = self.clock()
        present = set()
        with self.lock, self.connect() as db:
            for record in files:
                index = record.get('auth_index')
                provider = {'anthropic': 'claude'}.get(record.get('provider'), record.get('provider'))
                if not isinstance(index, str) or provider not in ('claude', 'codex'):
                    continue
                present.add(index)
                db.execute('insert into accounts(auth_index, provider, email, first_seen, last_seen) values (?, ?, ?, ?, ?) '
                           'on conflict(auth_index) do update set email = coalesce(excluded.email, accounts.email), '
                           'provider = excluded.provider, removed_at = null',
                           (index, provider, text(record.get('email'), 320), now, now))
            known = [row['auth_index'] for row in db.execute('select auth_index from accounts where removed_at is null')]
            for index in known:
                if index not in present:
                    db.execute('update accounts set removed_at = ? where auth_index = ?', (now, index))

    def sample_quota(self, snapshot_path):
        """Append each new quota observation from the shared quota snapshot."""
        try:
            mtime = Path(snapshot_path).stat().st_mtime
        except OSError:
            return 0
        if mtime == self.last_quota_mtime:
            return 0
        from quota_poller import read_private_json
        snapshot = read_private_json(snapshot_path)
        rows = []
        for index, account in (snapshot.get('accounts') or {}).items():
            if not isinstance(account, dict):
                continue
            for window in account.get('windows') or []:
                observed = epoch(window.get('observed_at')) or epoch(account.get('observed_at'))
                percent = window.get('used_percent')
                if observed is None or isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
                    continue
                rows.append((index, str(window.get('bucket') or window.get('kind'))[:64], str(window.get('kind'))[:32],
                             text(window.get('model') or window.get('model_prefix'), 64), float(percent),
                             text(window.get('reset_at'), 40), observed))
        with self.lock, self.connect() as db:
            before = db.total_changes
            db.executemany('insert or ignore into quota_samples values (?, ?, ?, ?, ?, ?, ?)', rows)
            added = db.total_changes - before
        self.last_quota_mtime = mtime
        return added

    def prune(self):
        now = self.clock()
        if now - self.last_prune < 86400:
            return
        cutoff = now - RETENTION_DAYS * 86400
        with self.lock, self.connect() as db:
            db.execute('delete from requests where ts < ?', (cutoff,))
            db.execute('delete from quota_samples where observed_at < ?', (cutoff,))
        self.last_prune = now

    def tick(self, snapshot_path=None):
        try:
            self.drain()
        except Exception as error:
            # Never record bodies; the class name is enough to show the collector is stuck.
            self.last_error = type(error).__name__
        if snapshot_path:
            self.sample_quota(snapshot_path)
        self.prune()

    # ------------------------------------------------------------------ reads

    def rate_table(self):
        return self.rates.table if self.rates is not None else NO_RATES

    def costs(self, db, table, lower, upper, offset=0, size=None):
        """API-list-price cost for one period, folded into the keys the usage view reports.

        Rows are grouped by served model, speed tier and prompt-size tier first, so each group
        has exactly one price; long-context requests land in their own group.
        """
        prompt = table.prompt_bucket_sql('(input + cache_read + cache_write)')
        slot, slot_values = ('cast((ts + ? - ?) / ? as integer)', (offset, lower + offset, size)) if size else ('0', ())
        sums = ', '.join(f'sum({field}) as {field}' for field in (*COST_FIELDS, 'total'))
        rows = db.execute(
            f'select auth_index, model, client, coalesce(response_model, model) as priced, service_tier, {prompt} as prompt, '
            f'{slot} as slot, count(*) as requests, {sums} from requests where ts >= ? and ts < ? '
            'group by auth_index, model, client, priced, service_tier, prompt, slot', (*slot_values, lower, upper))

        def blank():
            return {'cost': 0.0, **{f'{field}_cost': 0.0 for field in COST_FIELDS}, 'savings': 0.0,
                    'unpriced_tokens': 0, 'unpriced_requests': 0}
        result = {'totals': blank(), 'accounts': {}, 'series': {}, 'models': {}, 'clients': {}, 'unpriced_models': set()}
        for row in map(dict, rows):
            # The bucket is the largest tier edge the prompt exceeds; one token past it selects that tier.
            cost = table.price(row['priced'], row['service_tier'], row['prompt'] + 1 if row['prompt'] else 0, row)
            if cost is None:
                result['unpriced_models'].add(row['priced'])
            targets = (result['totals'], result['accounts'].setdefault(row['auth_index'], blank()),
                       result['series'].setdefault((row['auth_index'], row['slot']), blank()),
                       result['models'].setdefault(row['model'], blank()), result['clients'].setdefault(row['client'], blank()))
            for target in targets:
                if cost is None:
                    target['unpriced_tokens'] += row['total'] or 0
                    target['unpriced_requests'] += row['requests']
                    continue
                target['cost'] += cost['total']
                target['savings'] += cost['savings']
                for field in COST_FIELDS:
                    target[f'{field}_cost'] += cost[field]
        return result

    def status(self):
        with self.connect() as db:
            started = db.execute("select value from meta where key = 'started_at'").fetchone()
            first = db.execute('select min(ts) as ts, count(*) as n from requests').fetchone()
        return {'tracking_since': float(started['value']) if started else None, 'first_request_at': first['ts'],
                'stored_requests': first['n'], 'last_poll_at': self.last_poll_at, 'last_error': self.last_error,
                'pending': len(self.pending)}

    def accounts(self):
        with self.connect() as db:
            return {row['auth_index']: dict(row) for row in db.execute('select * from accounts')}

    def usage(self, range_key, tz_minutes=0):
        """Totals, per-account series and breakdowns for one range, plus the prior period."""
        span, size = RANGES[range_key]
        now = self.clock()
        offset = tz_minutes * 60
        # Buckets are aligned to the viewer's local clock so daily bars break at midnight.
        end = (math.floor((now + offset) / size) + 1) * size - offset
        start = end - span
        sums = ', '.join(f'sum({field}) as {field}' for field in TOKEN_FIELDS)
        with self.connect() as db:
            def totals(lower, upper):
                row = db.execute(f'select count(*) as requests, sum(failed) as failed, count(distinct session) as sessions, {sums} '
                                 'from requests where ts >= ? and ts < ?', (lower, upper)).fetchone()
                return {key: row[key] or 0 for key in row.keys()}
            accounts = [dict(row) for row in db.execute(
                f'select auth_index, provider, count(*) as requests, sum(failed) as failed, max(ts) as last_used, '
                f'count(distinct session) as sessions, {sums} from requests where ts >= ? and ts < ? '
                'group by auth_index order by total desc', (start, end))]
            series = {}
            for row in db.execute(f'select auth_index, cast((ts + ? - ?) / ? as integer) as slot, count(*) as requests, '
                                  f'sum(failed) as failed, {sums} from requests where ts >= ? and ts < ? group by auth_index, slot',
                                  (offset, start + offset, size, start, end)):
                points = series.setdefault(row['auth_index'], [None] * (span // size))
                if 0 <= row['slot'] < len(points):
                    points[row['slot']] = {key: row[key] or 0 for key in ('requests', 'failed', *TOKEN_FIELDS)}
            models = [dict(row) for row in db.execute(
                'select model, provider, count(*) as requests, sum(total) as total, sum(output) as output '
                'from requests where ts >= ? and ts < ? group by model, provider order by total desc limit 20', (start, end))]
            clients = [dict(row) for row in db.execute(
                'select client, count(*) as requests, sum(total) as total, count(distinct session) as sessions '
                'from requests where ts >= ? and ts < ? group by client order by total desc', (start, end))]
            latencies, speeds, model_speeds, all_speeds = {}, {}, {}, []
            for row in db.execute('select auth_index, model, output, stream, latency_ms, ttft_ms from requests where ts >= ? and ts < ? '
                                  'and failed = 0 and latency_ms is not null', (start, end)):
                latencies.setdefault(row['auth_index'], ([], []))
                latencies[row['auth_index']][0].append(row['latency_ms'])
                if row['ttft_ms']:
                    latencies[row['auth_index']][1].append(row['ttft_ms'])
                rate = speed(row['output'], row['latency_ms'], row['ttft_ms'], row['stream'])
                if rate is not None:
                    speeds.setdefault(row['auth_index'], []).append(rate)
                    model_speeds.setdefault(row['model'], []).append(rate)
                    all_speeds.append(rate)
            statuses = [dict(row) for row in db.execute(
                'select status, count(*) as requests from requests where ts >= ? and ts < ? and failed = 1 '
                'group by status order by requests desc', (start, end))]
            table = self.rate_table()
            cost = self.costs(db, table, start, end, offset, size)
            previous_cost = self.costs(db, table, start - span, start)['totals']
            result = {'range': range_key, 'start': start, 'end': end, 'bucket_seconds': size,
                      'totals': totals(start, end), 'previous': totals(start - span, start),
                      'models': models, 'clients': clients, 'failures': statuses,
                      'tps_p50': percentile(all_speeds, 50), 'tps_samples': len(all_speeds),
                      'pricing': table.describe(), 'unpriced_models': sorted(cost['unpriced_models'])[:20]}
            result['totals'].update(money(cost['totals']))
            result['previous'].update(money(previous_cost))
            for model in models:
                model['tps_p50'] = percentile(model_speeds.get(model['model'], []), 50)
                model.update(money(cost['models'].get(model['model'])))
            for client in clients:
                client.update(money(cost['clients'].get(client['client'])))
        for (index, slot), priced in cost['series'].items():
            points = series.setdefault(index, [None] * (span // size))
            if 0 <= slot < len(points):
                points[slot] = {**(points[slot] or {}), 'cost': round(priced['cost'], 6)}
        for account in accounts:
            account.update(money(cost['accounts'].get(account['auth_index'])))
            points = series.get(account['auth_index'], [])
            account['series'] = [point or {} for point in points] or [{}] * (span // size)
            done, first = latencies.get(account['auth_index'], ([], []))
            account['latency_p50_ms'] = percentile(done, 50)
            account['latency_p95_ms'] = percentile(done, 95)
            account['ttft_p50_ms'] = percentile(first, 50)
            account['tps_p50'] = percentile(speeds.get(account['auth_index'], []), 50)
            account['tps_p90'] = percentile(speeds.get(account['auth_index'], []), 90)
        result['accounts'] = accounts
        result['series'] = [
            {key: sum((series[index][slot] or {}).get(key, 0) for index in series) for key in ('requests', 'failed', *TOKEN_FIELDS, 'cost')}
            for slot in range(span // size)]
        return result

    def requests(self, limit=50, before=None, auth_index=None, failed=None, model=None):
        clauses, values = [], []
        if before is not None:
            # Keyset cursor on (ts, id): newest first even when the queue delivered out of order.
            clauses.append('(ts < ? or (ts = ? and id < ?))'); values.extend([before[0], before[0], before[1]])
        if auth_index:
            clauses.append('auth_index = ?'); values.append(auth_index)
        if failed is not None:
            clauses.append('failed = ?'); values.append(int(failed))
        if model:
            clauses.append('model = ?'); values.append(model)
        where = ('where ' + ' and '.join(clauses)) if clauses else ''
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                f'select id, ts, auth_index, provider, model, response_model, endpoint, client, session, input, cache_read, '
                f'cache_write, output, reasoning, total, latency_ms, ttft_ms, failed, status, stream, reasoning_effort, '
                f'service_tier from requests {where} order by ts desc, id desc limit ?', (*values, limit + 1))]
        table = self.rate_table()
        for row in rows:
            row['tps'] = speed(row['output'], row['latency_ms'], row['ttft_ms'], row['stream'], row['failed'])
            priced = table.price(row['response_model'] or row['model'], row['service_tier'],
                                 row['input'] + row['cache_read'] + row['cache_write'], row)
            row['cost'] = round(priced['total'], 6) if priced else None
        last = rows[limit - 1] if len(rows) > limit else None
        return {'requests': rows[:limit], 'next_before': f"{last['ts']!r}_{last['id']}" if last else None}

    def quota_history(self, range_key):
        span, _ = RANGES[range_key]
        start = self.clock() - span
        history = {}
        with self.connect() as db:
            for row in db.execute('select auth_index, bucket, kind, model, used_percent, reset_at, observed_at from quota_samples '
                                  'where observed_at >= ? order by observed_at', (start,)):
                key = (row['auth_index'], row['bucket'])
                entry = history.setdefault(key, {'auth_index': row['auth_index'], 'bucket': row['bucket'],
                                                 'kind': row['kind'], 'model': row['model'], 'points': []})
                entry['points'].append([row['observed_at'], row['used_percent'], row['reset_at']])
        return {'range': range_key, 'start': start, 'windows': list(history.values())}


def money(priced):
    """Rounded cost fields for one aggregate. Tokens no rate covers are counted, not priced at zero."""
    priced = priced or {}
    result = {key: round(priced.get(key, 0.0), 6) for key in ('cost', 'savings', *(f'{field}_cost' for field in COST_FIELDS))}
    result['unpriced_tokens'] = priced.get('unpriced_tokens', 0)
    result['unpriced_requests'] = priced.get('unpriced_requests', 0)
    return result


def speed(output, latency_ms, ttft_ms, stream=1, failed=0):
    """Output tokens per second after the first token arrives, or None when it isn't measurable.

    Only streamed, successful responses with at least 16 output tokens count: short or
    buffered responses make the generation window too small to mean anything.
    """
    if not stream or failed or not output or output < 16 or not latency_ms or not ttft_ms or latency_ms <= ttft_ms:
        return None
    return round(output / ((latency_ms - ttft_ms) / 1000), 1)


def percentile(values, rank):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(rank / 100 * len(ordered)) - 1))]


def parse_query(route, params):
    """Validate dashboard query parameters. Raises ValueError for anything unexpected."""
    def integer(name, low, high, default=None):
        if name not in params:
            return default
        value = params[name]
        if not re.fullmatch(r'-?\d{1,12}', value) or not low <= int(value) <= high:
            raise ValueError('Invalid ' + name)
        return int(value)
    if route == '/dashboard/usage':
        if set(params) - {'range', 'tz'} or params.get('range', '24h') not in RANGES:
            raise ValueError('Invalid usage query')
        return {'range_key': params.get('range', '24h'), 'tz_minutes': integer('tz', -900, 900, 0)}
    if route == '/dashboard/requests':
        if set(params) - {'limit', 'before', 'account', 'failed', 'model'}:
            raise ValueError('Invalid requests query')
        account = params.get('account')
        if account is not None and not re.fullmatch(r'[a-zA-Z0-9_-]{1,256}', account):
            raise ValueError('Invalid account')
        model = params.get('model')
        if model is not None and not re.fullmatch(r'[a-zA-Z0-9._:/-]{1,128}', model):
            raise ValueError('Invalid model')
        if params.get('failed') not in (None, '0', '1'):
            raise ValueError('Invalid failed filter')
        before = params.get('before')
        if before is not None:
            match = re.fullmatch(r'(\d{1,12}(?:\.\d{1,9})?)_(\d{1,15})', before)
            if not match:
                raise ValueError('Invalid cursor')
            before = (float(match.group(1)), int(match.group(2)))
        return {'limit': integer('limit', 1, 200, 50), 'before': before,
                'auth_index': account, 'failed': None if 'failed' not in params else params['failed'] == '1', 'model': model}
    if route == '/dashboard/quota-history':
        if set(params) - {'range'} or params.get('range', '7d') not in RANGES:
            raise ValueError('Invalid quota history query')
        return {'range_key': params.get('range', '7d')}
    if route in ('/dashboard/quota', '/dashboard/status', '/dashboard/live'):
        if params:
            raise ValueError('No parameters accepted')
        return {}
    raise ValueError('Route unavailable')
