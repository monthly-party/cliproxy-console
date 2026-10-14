"""API-equivalent cost for ledger tokens, at public list prices.

The accounts here are subscriptions, so nothing is billed per token. The estimate answers
"what would this traffic cost on the pay-as-you-go API". Rates come from LiteLLM's public
model price table, the source T3 Code's usage page uses: fetched at most once a day, kept on
disk in a reduced form, and replaced only by a table that parses. Unknown models stay
unpriced rather than priced at zero, so the page can say how much it could not price.

Unlike T3 Code, which reads transcripts without per-request sizes, the ledger knows each
request's prompt size, so long-context tiers ("above 272k tokens") are applied.
"""
from pathlib import Path
from urllib.request import Request, urlopen
import json
import math
import os
import re
import threading
import time

RATES_URL = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'
REFRESH_SECONDS = 86400
RETRY_SECONDS = 1800
TIMEOUT_SECONDS = 10
MAX_BYTES = 32 * 1024 * 1024
BUCKETS = {'input': 'input_cost_per_token', 'cache_read': 'cache_read_input_token_cost',
           'cache_write': 'cache_creation_input_token_cost', 'output': 'output_cost_per_token'}
# Only the four token prices, optionally per long-context tier and per speed tier.
FIELD = re.compile(r'(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost|cache_creation_input_token_cost)'
                   r'(?:_above_(\d{2,4})k_tokens)?(_priority|_flex)?')
SPEEDS = {'priority': '_priority', 'fast': '_priority', 'flex': '_flex'}
# Family names, not models; pricing them would guess.
NEVER_PRICE = {'synthetic', '<synthetic>', 'opus', 'sonnet', 'haiku', 'fable', 'unknown'}


def reduce_document(document):
    """Keep the token prices of entries that have both an input and an output price."""
    rates = {}
    if not isinstance(document, dict):
        return rates
    for key, entry in document.items():
        if not isinstance(key, str) or not isinstance(entry, dict) or len(key) > 200:
            continue
        kept = {}
        for field, value in entry.items():
            if not isinstance(field, str) or not FIELD.fullmatch(field):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                continue
            kept[field] = float(value)
        if 'input_cost_per_token' in kept and 'output_cost_per_token' in kept:
            rates[key.strip().lower()] = kept
    return rates


def normalize(model):
    """'Claude-Opus-5-5[1m]' -> 'claude-opus-5-5'."""
    if not isinstance(model, str):
        return ''
    return model.strip().lower().split('[', 1)[0].strip()


def thresholds(entry):
    found = set()
    for field in entry:
        match = FIELD.fullmatch(field)
        if match and match.group(2):
            found.add(int(match.group(2)) * 1000)
    return sorted(found)


class RateTable:
    """An immutable view of one rate document: exact keys plus unambiguous short-name aliases."""

    def __init__(self, rates, fetched_at=None, status='unavailable'):
        self.rates = rates
        self.fetched_at = fetched_at
        self.status = status
        short = {}
        for key, entry in rates.items():
            if '/' in key:
                short.setdefault(key.rsplit('/', 1)[1], []).append(entry)
        # A short name is an alias only when no exact entry has it and every prefixed entry agrees.
        self.aliases = {name: entries[0] for name, entries in short.items()
                        if name not in rates and all(entry == entries[0] for entry in entries)}
        edges = set()
        for entry in rates.values():
            edges.update(thresholds(entry))
        self.edges = sorted(edges)
        self.memo = {}

    def entry(self, model):
        name = normalize(model)
        if not name or name in NEVER_PRICE:
            return None
        return self.rates.get(name) or self.aliases.get(name)

    def tiers(self, model):
        name = normalize(model)
        if name not in self.memo:
            entry = self.entry(model)
            self.memo[name] = thresholds(entry) if entry else []
        return self.memo[name]

    def prompt_bucket_sql(self, column):
        """SQL CASE that maps a prompt size to the largest tier edge it exceeds (0 for none).

        Edges are integers parsed from field names, so they are safe to inline.
        """
        if not self.edges:
            return '0'
        cases = ' '.join(f'when {column} > {edge} then {edge}' for edge in reversed(self.edges))
        return f'(case {cases} else 0 end)'

    def unit_prices(self, model, service_tier=None, prompt=0):
        """USD per token for each bucket, or None when the model has no price."""
        entry = self.entry(model)
        if entry is None:
            return None
        tier = max((edge for edge in self.tiers(model) if prompt > edge), default=None)
        speed = SPEEDS.get((service_tier or '').strip().lower(), '')
        above = f'_above_{tier // 1000}k_tokens' if tier else ''

        def pick(base):
            names = [base + above + speed, base + speed, base + above, base] if speed else [base + above, base]
            return next((entry[name] for name in names if name in entry), None)

        prices = {bucket: pick(field) for bucket, field in BUCKETS.items()}
        # A missing cache price falls back to the input price: cache traffic is never free.
        for bucket in ('cache_read', 'cache_write'):
            if prices[bucket] is None:
                prices[bucket] = prices['input']
        return prices

    def price(self, model, service_tier, prompt, tokens):
        """Cost of a token group: {input, cache_read, cache_write, output, total, savings} or None.

        `tokens` holds non-overlapping counts; reasoning is already inside output.
        """
        prices = self.unit_prices(model, service_tier, prompt)
        if prices is None:
            return None
        cost = {bucket: (tokens.get(bucket) or 0) * prices[bucket] for bucket in BUCKETS}
        cost['total'] = sum(cost[bucket] for bucket in BUCKETS)
        cost['savings'] = (tokens.get('cache_read') or 0) * max(0.0, prices['input'] - prices['cache_read'])
        return cost

    def describe(self):
        return {'source': 'LiteLLM', 'url': RATES_URL, 'fetched_at': self.fetched_at, 'status': self.status,
                'models': len(self.rates)}


def fetch_document():
    request = Request(RATES_URL, headers={'User-Agent': 'cliproxy-console', 'Accept': 'application/json'})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError('Rate table unavailable')
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('Rate table too large')
    return json.loads(body)


class Rates:
    """Owns the on-disk copy. One background thread refreshes; request threads read `table`."""

    def __init__(self, path, fetch=None, clock=None):
        self.path = Path(path)
        self.fetch = fetch or fetch_document
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.last_attempt = 0
        self.table = self._load()

    def _load(self):
        try:
            saved = json.loads(self.path.read_text())
            rates = reduce_document(saved.get('rates'))
            fetched_at = saved.get('fetched_at')
            if rates and isinstance(fetched_at, (int, float)):
                return RateTable(rates, float(fetched_at), 'cached')
        except (OSError, ValueError, AttributeError):
            pass
        return RateTable({}, None, 'unavailable')

    def stale(self):
        fetched = self.table.fetched_at
        return fetched is None or self.clock() - fetched >= REFRESH_SECONDS

    def refresh(self, force=False):
        """Fetch when stale; keep the current table on any failure. Returns True on a new table."""
        now = self.clock()
        if not force and (not self.stale() or now - self.last_attempt < RETRY_SECONDS):
            return False
        self.last_attempt = now
        try:
            rates = reduce_document(self.fetch())
        except Exception:
            # Network or parse failure: the last good table stays in service.
            return False
        if not rates:
            return False
        table = RateTable(rates, now, 'fresh')
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump({'fetched_at': now, 'source': RATES_URL, 'rates': rates}, handle, separators=(',', ':'))
            os.replace(tmp, self.path)
        except OSError:
            pass
        with self.lock:
            self.table = table
        return True
