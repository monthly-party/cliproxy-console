import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor

from quota_poller import SharedQuotaCollector, normalize, iso, reset, usage_payload
Poller = SharedQuotaCollector


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,10,2,12,tzinfo=timezone.utc)

    def test_claude_scopes_and_unknown_reset(self):
        data = {'five_hour':{'utilization':100,'resets_at':None},
                'seven_day':{'utilization':34,'resets_at':'2026-10-03T12:00:00Z'},
                'seven_day_opus':{'utilization':100,'resets_at':'2026-10-04T12:00:00Z'},
                'limits':[{'kind':'weekly_scoped','percent':20,'is_active':True,
                           'resets_at':'2026-10-05T12:00:00Z',
                           'scope':{'model':{'display_name':'Fable 5'}}}]}
        windows = normalize('claude',data,self.now)
        self.assertEqual(len(windows),4)
        self.assertNotIn('reset_at',windows[0])
        self.assertEqual(windows[2]['model_prefix'],'claude-opus')
        self.assertEqual(windows[3]['model_prefix'],'claude-fable')

    def test_codex_duration_not_primary_slot(self):
        data = {'rate_limit':{'allowed':True,'primary_window':{
            'used_percent':74,'limit_window_seconds':604800,'reset_at':1791028800}}}
        windows = normalize('codex',data,self.now)
        self.assertEqual(windows[0]['kind'],'weekly')
        self.assertEqual(windows[0]['used_percent'],74)

    def test_codex_model_exact_and_denial(self):
        data = {'rate_limit':{'allowed':False},'additional_rate_limits':[
            {'metered_feature':'gpt-6.1-sol','rate_limit':{'allowed':True,
                'secondary_window':{'used_percent':80,'limit_window_seconds':604800,
                                     'reset_after_seconds':600}}},
            {'metered_feature':'code_review','rate_limit':{'allowed':False}}]}
        windows = normalize('codex',data,self.now)
        self.assertEqual(windows[0]['used_percent'],100)
        self.assertEqual(windows[1]['model'],'gpt-6.1-sol')
        self.assertNotIn('model_prefix',windows[1])
        self.assertEqual(windows[1]['reset_at'],'2026-10-02T12:10:00Z')
        self.assertEqual(len(windows),3)

    def test_unknown_period_never_fabricates_weekly_priority(self):
        data = {'rate_limit':{'secondary_window':{'used_percent':12,'reset_after_seconds':600}}}
        self.assertEqual(normalize('codex',data,self.now)[0]['kind'],'other')

    def test_invalid_numbers_and_timestamps(self):
        for percent in (-1,101,float('nan'),float('inf'),'bad',True):
            self.assertEqual(normalize('claude',{'seven_day':{'utilization':percent}},self.now),[])
        self.assertIsNone(reset('bad',self.now))
        self.assertIsNone(reset('2026-10-02T12:00:00',self.now))


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot = Path(self.temporary.name)/'snapshot.json'
        self.now = datetime(2026,10,2,12,tzinfo=timezone.utc)
        self.calls = []
        self.reply = {'status_code':200,'body':json.dumps({
            'seven_day':{'utilization':100,'resets_at':'2026-10-02T13:00:00Z'}})}
        self.http_status = 200
        self.records = [{'auth_index':'safe-index','provider':'claude','email':'private@example.invalid',
                         'access_token':'MUST-NOT-PERSIST'}]
        def request(key,path,payload=None):
            self.assertEqual(key,'INTERNAL-ONLY-KEY')
            self.calls.append((path,payload))
            if path == '/credentials':
                return 200,{'files':self.records}
            self.assertEqual(payload['method'],'GET')
            self.assertEqual(payload['header']['Authorization'],'Bearer $TOKEN$')
            return self.http_status,self.reply
        self.poller = Poller('INTERNAL-ONLY-KEY',self.snapshot,Path(self.temporary.name)/'cache.json',
                             clock=lambda:self.now,request=request,initial_grace_seconds=0)

    def advance(self,seconds):
        self.now += timedelta(seconds=seconds)

    def test_refresh_dedup_and_atomic_private_snapshot(self):
        self.poller.tick()
        self.assertEqual(len(self.calls),2)
        for _ in range(4):
            self.poller.tick()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(os.stat(self.snapshot).st_mode & 0o777,0o600)
        content = self.snapshot.read_text()
        self.assertNotIn('private@example.invalid',content)
        self.assertNotIn('MUST-NOT-PERSIST',content)
        self.assertNotIn('INTERNAL-ONLY-KEY',content)
        self.assertFalse(self.snapshot.with_suffix('.tmp').exists())
        self.advance(600)
        self.poller.tick()
        self.assertEqual(len(self.calls),4)

    def test_rate_limit_backoff_retains_good_data(self):
        self.poller.tick()
        previous = self.snapshot.read_text()
        self.http_status = 429
        self.advance(600)
        self.poller.tick()
        count = len([p for p,_ in self.calls if p == '/requests/api-call'])
        self.advance(600)
        self.poller.tick()
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),count)
        self.assertEqual(self.snapshot.read_text(),previous)
        self.advance(600)
        self.poller.tick()
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),count+1)

    def test_reset_requires_revalidation_then_recovery(self):
        self.poller.tick()
        self.advance(3700)
        self.http_status = 429
        self.poller.tick()
        self.assertEqual(self.poller.accounts['safe-index']['windows'][0]['used_percent'],100)
        self.advance(1200)
        self.http_status = 200
        self.reply = {'status_code':200,'body':{'seven_day':{
            'utilization':0,'resets_at':'2026-10-09T13:00:00Z'}}}
        self.poller.tick()
        self.assertEqual(self.poller.accounts['safe-index']['windows'][0]['used_percent'],0)

    def test_partial_response_retains_exhausted_bucket_original_time(self):
        self.poller.tick()
        observed = iso(self.now)
        self.advance(600)
        self.reply = {'status_code':200,'body':{'five_hour':{'utilization':0}}}
        self.poller.tick()
        retained = next(w for w in self.poller.accounts['safe-index']['windows'] if w['kind']=='weekly')
        self.assertEqual(retained['used_percent'],100)
        self.assertEqual(retained['observed_at'],observed)

    def test_disabled_or_removed_accounts_pruned(self):
        self.poller.tick()
        self.records[0]['disabled'] = True
        self.advance(600)
        self.poller.tick()
        self.assertEqual(json.loads(self.snapshot.read_text())['accounts'],{})

    def test_restart_retains_exhausted_snapshot(self):
        self.poller.tick()
        restarted = Poller('INTERNAL-ONLY-KEY',self.snapshot,Path(self.temporary.name)/'cache.json',clock=lambda:self.now)
        self.assertEqual(restarted.accounts['safe-index']['windows'][0]['used_percent'],100)

    def test_ui_manual_and_timer_share_ten_minute_deadline(self):
        payload = usage_payload(self.records[0],'claude')
        status,reply = self.poller.get(payload)
        self.assertEqual(status,200)
        self.assertEqual(reply['quota_cache']['retry_after_seconds'],600)
        self.poller.tick()
        for _ in range(5):
            self.poller.get(payload)
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),1)
        self.advance(599)
        self.poller.get(payload)
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),1)
        self.advance(1)
        self.poller.get(payload)
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),2)

    def test_retry_after_longer_than_ten_minutes(self):
        payload = usage_payload(self.records[0],'claude')
        self.poller.get(payload)
        self.advance(600)
        self.reply = {'status_code':429,'body':{'access_token':'MUST-NOT-PERSIST'},
                      'headers':{'Retry-After':['2400'],'Set-Cookie':['MUST-NOT-PERSIST']}}
        status,reply = self.poller.get(payload)
        self.assertEqual(status,200)
        self.assertTrue(reply['quota_cache']['stale'])
        self.assertEqual(reply['quota_cache']['error_status'],429)
        self.assertEqual(reply['quota_cache']['retry_after_seconds'],2400)
        self.assertEqual(reply['body']['seven_day']['utilization'],100)
        self.advance(2399)
        self.poller.get(payload)
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),2)
        self.advance(1)
        self.poller.get(payload)
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),3)
        self.assertNotIn('MUST-NOT-PERSIST',(Path(self.temporary.name)/'cache.json').read_text())

    def test_retry_after_http_date(self):
        from email.utils import format_datetime
        payload = usage_payload(self.records[0],'claude')
        self.reply = {'status_code':429,'body':{},'header':{
            'retry-after':format_datetime(self.now+timedelta(seconds=3000),usegmt=True)}}
        _,reply = self.poller.get(payload)
        self.assertEqual(reply['quota_cache']['retry_after_seconds'],3000)

    def test_restart_preserves_deadline_without_network(self):
        payload = usage_payload(self.records[0],'claude')
        self.poller.get(payload)
        def blocked(*args):
            self.fail('Restart bypassed persisted 600-second deadline')
        restarted = Poller('INTERNAL-ONLY-KEY',self.snapshot,
                           Path(self.temporary.name)/'cache.json',clock=lambda:self.now,request=blocked)
        _,reply = restarted.get(payload)
        self.assertEqual(reply['quota_cache']['retry_after_seconds'],600)

    def test_concurrent_reads_are_singleflight(self):
        payload = usage_payload(self.records[0],'claude')
        barrier = threading.Barrier(8)
        def get():
            barrier.wait(timeout=5)
            return self.poller.get(payload)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _:get(),range(8)))
        self.assertTrue(all(status == 200 for status,_ in results))
        self.assertEqual(len([p for p,_ in self.calls if p == '/requests/api-call']),1)

    def test_cache_body_rejects_unexpected_secrets_and_headers(self):
        self.reply = {'status_code':200,'body':{
            'access_token':'MUST-NOT-PERSIST','raw_request':'MUST-NOT-PERSIST',
            'seven_day':{'utilization':50,'resets_at':'2026-10-03T12:00:00Z',
                         'refresh_token':'MUST-NOT-PERSIST'}},
            'headers':{'Set-Cookie':['MUST-NOT-PERSIST']}}
        self.poller.get(usage_payload(self.records[0],'claude'))
        content = (Path(self.temporary.name)/'cache.json').read_text()
        self.assertNotIn('MUST-NOT-PERSIST',content)
        self.assertNotIn('INTERNAL-ONLY-KEY',content)
        self.assertEqual(os.stat(Path(self.temporary.name)/'cache.json').st_mode & 0o777,0o600)

    def test_cold_start_grace_prevents_upstream_requests_and_persists(self):
        calls = []
        def request(key,path,payload=None):
            calls.append(path)
            if path == '/credentials':
                return 200,{'files':self.records}
            return 200,self.reply
        collector = Poller('INTERNAL-ONLY-KEY',self.snapshot,
                           Path(self.temporary.name)/'cache.json',clock=lambda:self.now,request=request)
        collector.tick()
        payload = usage_payload(self.records[0],'claude')
        for _ in range(3):
            _,reply = collector.get(payload)
            self.assertEqual(reply['quota_cache']['retry_after_seconds'],600)
        self.assertNotIn('/requests/api-call',calls)
        self.advance(599)
        collector.get(payload)
        self.assertNotIn('/requests/api-call',calls)
        self.advance(1)
        collector.get(payload)
        self.assertEqual(calls.count('/requests/api-call'),1)

    def test_snapshot_seed_uses_original_quota_and_observation(self):
        self.poller.tick()
        observed = self.poller.accounts['safe-index']['observed_at']
        (Path(self.temporary.name)/'cache.json').unlink()
        self.advance(1200)
        def blocked(*args):
            self.fail('Seeding made an upstream request')
        seeded = Poller('INTERNAL-ONLY-KEY',self.snapshot,
                        Path(self.temporary.name)/'cache.json',clock=lambda:self.now,request=blocked)
        status,reply = seeded.get(usage_payload(self.records[0],'claude'))
        self.assertEqual(status,200)
        self.assertEqual(reply['body']['seven_day']['utilization'],100)
        self.assertEqual(reply['quota_cache']['observed_at'],observed)
        self.assertTrue(reply['quota_cache']['stale'])
        self.assertEqual(reply['quota_cache']['retry_after_seconds'],600)


if __name__ == '__main__':
    unittest.main()
