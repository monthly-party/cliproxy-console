import json
import tempfile
import unittest
from pathlib import Path

import pricing
from pricing import RateTable, Rates, reduce_document

# Shapes copied from LiteLLM's model_prices_and_context_window.json (USD per token).
DOCUMENT = {
    'sample_spec': {'input_cost_per_token': 0, 'output_cost_per_token': 0, 'max_tokens': 'see docs'},
    'gpt-6.1-sol': {
        'input_cost_per_token': 2e-06, 'output_cost_per_token': 1e-05,
        'cache_read_input_token_cost': 1e-07, 'cache_creation_input_token_cost': 2.5e-06,
        'input_cost_per_token_above_272k_tokens': 4e-06, 'output_cost_per_token_above_272k_tokens': 1.5e-05,
        'cache_read_input_token_cost_above_272k_tokens': 2e-07, 'cache_creation_input_token_cost_above_272k_tokens': 5e-06,
        'input_cost_per_token_priority': 4e-06, 'output_cost_per_token_priority': 2e-05,
        'input_cost_per_token_batches': 1e-06, 'search_context_cost_per_query': {'search_context_size_low': 0.01},
        'litellm_provider': 'openai', 'mode': 'chat'},
    'claude-opus-5-5': {
        'input_cost_per_token': 4e-06, 'output_cost_per_token': 2e-05,
        'cache_read_input_token_cost': 2e-07, 'cache_creation_input_token_cost': 5e-06,
        'cache_creation_input_token_cost_above_1hr': 8e-06},
    'claude-haiku-5-5': {
        'input_cost_per_token': 1e-07, 'output_cost_per_token': 5e-07, 'cache_read_input_token_cost': 1e-08,
        'input_cost_per_token_above_100k_tokens': 5e-07, 'output_cost_per_token_above_100k_tokens': 2.5e-06},
    'no-cache-model': {'input_cost_per_token': 1e-06, 'output_cost_per_token': 2e-06},
    'openrouter/acme/shared': {'input_cost_per_token': 1e-06, 'output_cost_per_token': 2e-06},
    'bedrock/acme/shared': {'input_cost_per_token': 1e-06, 'output_cost_per_token': 2e-06},
    'openrouter/acme/split': {'input_cost_per_token': 1e-06, 'output_cost_per_token': 2e-06},
    'bedrock/acme/split': {'input_cost_per_token': 3e-06, 'output_cost_per_token': 2e-06},
    'image-only': {'input_cost_per_token': 5e-06, 'output_cost_per_image_token': 3e-05},
    'hostile': {'input_cost_per_token': -1, 'output_cost_per_token': float('nan')},
    'boolish': {'input_cost_per_token': True, 'output_cost_per_token': 1e-06},
}


def tokens(input=0, cache_read=0, cache_write=0, output=0):
    return {'input': input, 'cache_read': cache_read, 'cache_write': cache_write, 'output': output}


class ReduceTests(unittest.TestCase):
    def test_keeps_only_token_prices_of_complete_entries(self):
        rates = reduce_document(DOCUMENT)
        self.assertEqual(set(rates), {'sample_spec', 'gpt-6.1-sol', 'claude-opus-5-5', 'claude-haiku-5-5', 'no-cache-model',
                                      'openrouter/acme/shared', 'bedrock/acme/shared', 'openrouter/acme/split', 'bedrock/acme/split'})
        sol = rates['gpt-6.1-sol']
        self.assertNotIn('input_cost_per_token_batches', sol)
        self.assertNotIn('litellm_provider', sol)
        self.assertIn('input_cost_per_token_above_272k_tokens', sol)
        self.assertNotIn('cache_creation_input_token_cost_above_1hr', rates['claude-opus-5-5'])

    def test_rejects_non_documents(self):
        self.assertEqual(reduce_document(['x']), {})
        self.assertEqual(reduce_document({'m': 'x'}), {})


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.table = RateTable(reduce_document(DOCUMENT), 1.0, 'fresh')

    def test_formula_prices_each_bucket_once(self):
        cost = self.table.price('gpt-6.1-sol', 'auto', 1_000, tokens(input=100, cache_read=900, output=50))
        self.assertAlmostEqual(cost['input'], 100 * 2e-06)
        self.assertAlmostEqual(cost['cache_read'], 900 * 1e-07)
        self.assertAlmostEqual(cost['output'], 50 * 1e-05)
        self.assertAlmostEqual(cost['total'], 100 * 2e-06 + 900 * 1e-07 + 50 * 1e-05)
        self.assertAlmostEqual(cost['savings'], 900 * (2e-06 - 1e-07))

    def test_long_context_tier_applies_to_every_bucket(self):
        cost = self.table.price('gpt-6.1-sol', None, 272_001, tokens(input=1, cache_read=272_000, output=10))
        self.assertAlmostEqual(cost['input'], 1 * 4e-06)
        self.assertAlmostEqual(cost['cache_read'], 272_000 * 2e-07)
        self.assertAlmostEqual(cost['output'], 10 * 1.5e-05)
        exactly = self.table.price('gpt-6.1-sol', None, 272_000, tokens(output=10))
        self.assertAlmostEqual(exactly['output'], 10 * 1e-05)

    def test_missing_tier_sibling_uses_base_rate(self):
        prices = self.table.unit_prices('claude-haiku-5-5', None, 150_000)
        self.assertEqual((prices['input'], prices['output']), (5e-07, 2.5e-06))
        # No cache price above 100k: the base cache price, not the long-context input price.
        self.assertEqual(prices['cache_read'], 1e-08)

    def test_speed_tier(self):
        prices = self.table.unit_prices('gpt-6.1-sol', 'priority', 10)
        self.assertEqual((prices['input'], prices['output']), (4e-06, 2e-05))
        self.assertEqual(prices['cache_read'], 1e-07)   # no priority cache price listed
        self.assertEqual(self.table.unit_prices('gpt-6.1-sol', 'auto', 10)['input'], 2e-06)

    def test_missing_cache_prices_fall_back_to_input(self):
        prices = self.table.unit_prices('no-cache-model')
        self.assertEqual((prices['cache_read'], prices['cache_write']), (1e-06, 1e-06))

    def test_name_normalization(self):
        self.assertIsNotNone(self.table.entry('Claude-Opus-5-5[1m]'))
        self.assertIsNotNone(self.table.entry('  gpt-6.1-sol '))
        for name in ('opus', 'sonnet', '<synthetic>', '', None, 'gpt-image-2', 'unknown'):
            self.assertIsNone(self.table.entry(name))

    def test_short_name_alias_only_when_unambiguous(self):
        self.assertIsNotNone(self.table.entry('shared'))
        self.assertIsNone(self.table.entry('split'))
        self.assertIsNone(self.table.price('split', None, 0, tokens(input=5)))

    def test_prompt_bucket_sql(self):
        self.assertEqual(self.table.edges, [100_000, 272_000])
        self.assertEqual(self.table.prompt_bucket_sql('p'), '(case when p > 272000 then 272000 when p > 100000 then 100000 else 0 end)')
        self.assertEqual(RateTable({}).prompt_bucket_sql('p'), '0')


class RatesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'model-rates.json'
        self.now = 1_000_000.0

    def tearDown(self):
        self.dir.cleanup()

    def rates(self, fetch):
        return Rates(self.path, fetch=fetch, clock=lambda: self.now)

    def test_fetch_persists_reduced_private_copy(self):
        rates = self.rates(lambda: DOCUMENT)
        self.assertEqual(rates.table.status, 'unavailable')
        self.assertTrue(rates.refresh())
        self.assertEqual(rates.table.status, 'fresh')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['fetched_at'], self.now)
        self.assertNotIn('litellm_provider', json.dumps(saved))
        reloaded = self.rates(lambda: self.fail('should not fetch'))
        self.assertEqual((reloaded.table.status, reloaded.table.fetched_at), ('cached', self.now))
        self.assertFalse(reloaded.stale())

    def test_failure_keeps_last_table_and_waits_before_retrying(self):
        calls = []
        def broken():
            calls.append(1)
            raise OSError('offline')
        self.rates(lambda: DOCUMENT).refresh()
        self.now += pricing.REFRESH_SECONDS + 1
        rates = self.rates(broken)
        self.assertTrue(rates.stale())
        self.assertFalse(rates.refresh())
        self.assertFalse(rates.refresh())
        self.assertEqual(len(calls), 1)
        self.assertEqual(rates.table.status, 'cached')
        self.assertIsNotNone(rates.table.entry('gpt-6.1-sol'))

    def test_empty_or_hostile_document_never_replaces_the_table(self):
        self.rates(lambda: DOCUMENT).refresh()
        rates = self.rates(lambda: {'hostile': DOCUMENT['hostile']})
        self.assertFalse(rates.refresh(force=True))
        self.assertIsNotNone(rates.table.entry('gpt-6.1-sol'))

    def test_corrupt_disk_copy_starts_unpriced(self):
        self.path.write_text('{not json')
        rates = self.rates(lambda: DOCUMENT)
        self.assertEqual(rates.table.status, 'unavailable')
        self.assertIsNone(rates.table.price('gpt-6.1-sol', None, 0, tokens(input=1)))


if __name__ == '__main__':
    unittest.main()
