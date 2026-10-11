"""Bounded local quota polling. Reads an existing key internally; never logs bodies.

The snapshot contains only stable auth indices, provider names, quota percentages,
window model prefixes, observation times, and reset instants. No OAuth is copied.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime
import json
import math
import os
import re
import time
import threading
import copy

from settings import PROXY_BASE

BASE = PROXY_BASE + '/v8/management'
MAX_BYTES = 1 << 20
MAX_ACCOUNTS = 64
INTERVAL = 600
CLAUDE_USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
CLAUDE_RESET_USAGE_URL = CLAUDE_USAGE_URL + '?cedar_ember=1&skip_spend=1'


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def reset(value, now, offset=None):
    try:
        if isinstance(value, str) and 'T' in value:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return iso(parsed) if parsed.tzinfo else None
        seconds = number(value)
        if seconds is not None and seconds > 0:
            return iso(datetime.fromtimestamp(seconds, timezone.utc))
        relative = number(offset)
        if relative is not None and relative >= 0:
            return iso(now + timedelta(seconds=relative))
    except (ValueError, OverflowError, OSError):
        pass
    return None


def window(kind, prefix, percent, reset_at):
    percent = number(percent)
    if percent is None or percent < 0 or percent > 100:
        return None
    value = {'kind':kind, 'used_percent':percent}
    if prefix:
        value['model_prefix'] = prefix
    if reset_at:
        value['reset_at'] = reset_at
    return value


def normalize(provider, body, now):
    windows = []
    def add(value, bucket='', model=''):
        if value is not None:
            value['observed_at'] = iso(now)
            if bucket:
                value['bucket'] = bucket
            if model:
                value.pop('model_prefix',None)
                value['model'] = model
            windows.append(value)
    if provider == 'claude':
        keys = [('five_hour','five_hour',''), ('seven_day','weekly',''),
                ('seven_day_oauth_apps','weekly',''),
                ('seven_day_opus','weekly','claude-opus'),
                ('seven_day_sonnet','weekly','claude-sonnet'),
                ('iguana_necktie','weekly','claude-fable')]
        for key, kind, prefix in keys:
            data = body.get(key)
            if isinstance(data, dict):
                add(window(kind, prefix, data.get('utilization'), reset(data.get('resets_at'),now)),key)
        for data in body.get('limits', []) if isinstance(body.get('limits'), list) else []:
            if not isinstance(data,dict) or data.get('kind') != 'weekly_scoped' or data.get('is_active') is False:
                continue
            scope = data.get('scope',{}).get('model',{})
            model_id = str(scope.get('id','')).lower()
            display = str(scope.get('display_name','')).lower()
            prefix = model_id if model_id.startswith('claude-') else {
                'opus':'claude-opus', 'sonnet':'claude-sonnet',
                'fable':'claude-fable', 'fable 5':'claude-fable',
            }.get(display)
            if prefix:
                add(window('weekly', prefix, data.get('percent'), reset(data.get('resets_at'),now)), 'scoped:'+prefix)
    else:
        def codex_limit(data, prefix=''):
            if not isinstance(data,dict):
                return
            denied = data.get('allowed') is False or data.get('limit_reached') is True
            found = False
            for key in ('primary_window','secondary_window'):
                value = data.get(key)
                if not isinstance(value,dict):
                    continue
                seconds = number(value.get('limit_window_seconds'))
                kind = 'weekly' if seconds == 604800 else 'five_hour' if seconds == 18000 else 'other'
                percent = value.get('used_percent')
                if number(percent) is None and denied:
                    percent = 100
                item = window(kind,prefix,percent,reset(value.get('reset_at'),now,value.get('reset_after_seconds')))
                add(item, prefix+':'+key, prefix)
                found = found or item is not None
            # Explicit denial with no populated window is still unavailable.
            if denied and (not found or not any(w['used_percent'] >= 100 and w.get('model_prefix','') == prefix for w in windows)):
                add(window('other',prefix,100,None),prefix+':denial',prefix)
            elif data.get('allowed') is True:
                add(window('other',prefix,0,None),prefix+':denial',prefix)
        codex_limit(body.get('rate_limit'))
        for data in body.get('additional_rate_limits',[]) if isinstance(body.get('additional_rate_limits'),list) else []:
            if not isinstance(data,dict):
                continue
            # Only an explicit model identifier is a safe model association.
            # Unclassified feature limits must not be applied to unrelated models.
            feature = str(data.get('metered_feature','')).strip().lower()
            if re.fullmatch(r'gpt-[a-z0-9.\-]+', feature):
                codex_limit(data.get('rate_limit'),feature)
    return windows if len(windows) <= 64 else []


def api(key, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(BASE+path, data=body, headers={
        'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
    try:
        with urlopen(request,timeout=20) as response:
            data = response.read(MAX_BYTES+1)
            if len(data) > MAX_BYTES:
                return 0, {}
            return response.status, json.loads(data)
    except HTTPError as error:
        return error.code, {}
    except Exception:
        return 0, {}


def usage_payload(record, provider):
    headers = {'Authorization':'Bearer $TOKEN$', 'Content-Type':'application/json'}
    if provider == 'claude':
        url = CLAUDE_RESET_USAGE_URL
        headers.update({'User-Agent':'claude-cli/2.1.287 (external, cli)',
                        'anthropic-beta':'oauth-2025-04-20'})
    else:
        url = 'https://chatgpt.com/backend-api/wham/usage'
        headers['User-Agent'] = 'codex-cli/0.159.2'
        account_id = record.get('chatgpt_account_id')
        if not account_id:
            # Upstream returns a parsed safe id_token object, not the raw JWT.
            parsed = record.get('id_token')
            if isinstance(parsed,dict):
                account_id = parsed.get('chatgpt_account_id')
        if isinstance(account_id,str) and account_id:
            headers['Chatgpt-Account-Id'] = account_id
    return {'authIndex':record['auth_index'],'method':'GET','url':url,'header':headers}


def retry_after(reply, now):
    headers = reply.get('headers',reply.get('header',{}))
    if not isinstance(headers,dict):
        return 0
    value = next((v for k,v in headers.items() if k.lower() == 'retry-after'),None)
    if isinstance(value,list):
        value = value[0] if value else None
    if value is None:
        return 0
    numeric = number(value)
    if numeric is not None:
        return max(0,math.ceil(numeric))
    try:
        parsed = parsedate_to_datetime(str(value))
        return max(0,math.ceil((parsed-now).total_seconds())) if parsed.tzinfo else 0
    except (ValueError,TypeError,OverflowError):
        return 0


def read_private_json(path):
    try:
        with Path(path).open('rb') as source:
            data = source.read(MAX_BYTES+1)
        if len(data) <= MAX_BYTES:
            value = json.loads(data)
            return value if isinstance(value,dict) else {}
    except Exception:
        pass
    return {}


def write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as output:
        json.dump(value,output,separators=(',',':'))
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary,0o600)
    os.replace(temporary,path)


def safe_claude_reset_allowance(body, now=None):
    """Count-only cedar_ember view; never retain grant IDs or spending metadata.

    Remaining is the sum used by the pinned upstream ClaudeResetGrants reader.
    Known usability gates mirror its selectResetGrant helper; cached status does
    not promise that a reset can be spent, and no spending action is exposed.
    """
    now = now or datetime.now(timezone.utc)
    block = body.get('cedar_ember')
    if not isinstance(block,dict) or type(block.get('eligible')) is not bool:
        return None
    grants = block.get('grants')
    if not isinstance(grants,list) or len(grants)>64:
        return None
    def count(value):
        return type(value) is int and 0<=value<=2**53-1
    def instant(value):
        if value is None: return None
        if not isinstance(value,str): raise ValueError('Invalid timestamp')
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if not parsed.tzinfo: raise ValueError('Timestamp timezone required')
        return parsed.astimezone(timezone.utc)
    def flag(record,key,fallback):
        value=record.get(key)
        if value is None: return fallback
        if type(value) is not bool: raise ValueError('Invalid status')
        return value
    try:
        at_limit=flag(block,'at_limit',None)
        cooldown=instant(block.get('cooldown_until'))
        seen=set(); remaining=0; usable=False
        for grant in grants:
            if not isinstance(grant,dict): return None
            identity=grant.get('id')
            if not isinstance(identity,str) or not re.fullmatch(r'[a-z0-9_-]{1,40}',identity) or identity in seen:
                return None
            seen.add(identity)
            total=grant.get('resets_total'); left=grant.get('resets_left')
            if not count(total) or not count(left) or left>total: return None
            remaining+=left
            if not count(remaining): return None
            starts=instant(grant.get('starts_at')); ends=instant(grant.get('ends_at'))
            paused=flag(grant,'paused',False)
            usable_now=flag(grant,'usable_now',False)
            requires_limit=flag(grant,'use_requires_limit',True)
            usable=usable or bool(block['eligible'] and left>0 and usable_now and not paused
                and (not requires_limit or at_limit is True)
                and (not starts or starts<=now) and (not ends or ends>now)
                and (not cooldown or cooldown<=now))
        return {'source':'cedar_ember','remaining_count':remaining,
                'eligible':block['eligible'],'at_limit':at_limit,'usable_now':usable}
    except (ValueError,TypeError,OverflowError):
        return None


def safe_quota_body(provider, body, now=None):
    # Keep only the provider's quota schema. Unexpected credentials, URLs,
    # request content or provider error bodies cannot enter the disk cache.
    top = ({'five_hour','seven_day','seven_day_oauth_apps','seven_day_opus',
            'seven_day_sonnet','seven_day_cowork','iguana_necktie','limits','extra_usage'}
           if provider == 'claude' else
           {'rate_limit','additional_rate_limits','code_review_rate_limit',
            'plan_type','credits','rate_limit_reset_credits'})
    nested = {'utilization','resets_at','kind','group','percent','is_active',
              'scope','model','id','display_name','is_enabled','monthly_limit',
              'used_credits','allowed','limit_reached','primary_window','secondary_window',
              'used_percent','limit_window_seconds','reset_at','reset_after_seconds',
              'limit_name','metered_feature','rate_limit','has_credits','unlimited',
              'balance','available_count','applicable_available_count'}
    def clean(value,depth=0):
        if depth > 8:
            return None
        if isinstance(value,dict):
            return {k:clean(v,depth+1) for k,v in value.items() if k in nested}
        if isinstance(value,list):
            return [clean(v,depth+1) for v in value[:64]]
        if value is None or isinstance(value,(bool,int,float)):
            return value
        return value[:512] if isinstance(value,str) else None
    result = {key:clean(value) for key,value in body.items() if key in top}
    if provider == 'claude':
        allowance=safe_claude_reset_allowance(body,now)
        if allowance is not None: result['claude_reset_allowance']=allowance
    return result


class SharedQuotaCollector:
    """One gateway-owned instance serves daemon ticks and every UI quota read.

    The caller must retain the gateway's existing payload validation and start
    at most one tick thread. No separate polling process or force-refresh exists.
    """
    def __init__(self, key, snapshot, cache_path, clock=None, request=api, initial_grace_seconds=INTERVAL):
        self.key = key
        self.snapshot_path = Path(snapshot)
        self.cache_path = Path(cache_path)
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.request = request
        self.initial_grace_seconds = initial_grace_seconds
        self.lock = threading.RLock()
        self.accounts = {}
        self.cache = {}
        self.next_list = 0
        self.records = {}
        saved = read_private_json(self.snapshot_path)
        if saved.get('version') == 1 and isinstance(saved.get('accounts'),dict) and len(saved['accounts']) <= MAX_ACCOUNTS:
            self.accounts = saved['accounts']
        saved = read_private_json(self.cache_path)
        if saved.get('version') == 1 and isinstance(saved.get('entries'),dict) and len(saved['entries']) <= MAX_ACCOUNTS:
            self.cache = saved['entries']
        changed = False
        for index,account in self.accounts.items():
            provider = account.get('provider')
            if provider in ('claude','codex') and provider+':'+index not in self.cache:
                self.cache[provider+':'+index] = self._cold_entry(provider,index,self.clock())
                changed = True
        if changed:
            self.save()

    def _cold_entry(self, provider, index, now):
        account = self.accounts.get(index,{})
        body = {}
        for window in account.get('windows',[]):
            bucket = window.get('bucket','')
            percent = window.get('used_percent')
            if number(percent) is None:
                continue
            if provider == 'claude' and bucket in {'five_hour','seven_day','seven_day_oauth_apps','seven_day_opus','seven_day_sonnet','iguana_necktie'}:
                body[bucket] = {'utilization':percent,'resets_at':window.get('reset_at')}
            elif provider == 'codex' and bucket in (':primary_window',':secondary_window'):
                item = {'used_percent':percent}
                duration = {'weekly':604800,'five_hour':18000}.get(window.get('kind'))
                if duration:
                    item['limit_window_seconds'] = duration
                if window.get('reset_at'):
                    try:
                        item['reset_at'] = datetime.fromisoformat(window['reset_at'].replace('Z','+00:00')).timestamp()
                    except (ValueError,TypeError):
                        pass
                body.setdefault('rate_limit',{})[bucket[1:]] = item
        entry = {'next_check_at':now.timestamp()+self.initial_grace_seconds,
                 'http_status':503,'error_status':503,'failures':0,
                 'error_reply':{'status_code':503,'body':{}},
                 'observed_at':account.get('observed_at')}
        if body:
            entry.update({'good_reply':{'status_code':200,'body':body},
                          'http_status':200,'error_status':None,'seeded':True})
        return entry

    def _view(self, entry, now):
        reply = copy.deepcopy(entry.get('good_reply',entry.get('error_reply',{'status_code':503,'body':{}})))
        deadline = number(entry.get('next_check_at')) or 0
        reply['quota_cache'] = {
            'observed_at':entry.get('observed_at'),
            'next_check_at':iso(datetime.fromtimestamp(deadline,timezone.utc)),
            'stale':bool(entry.get('error_status')) or bool(entry.get('seeded')),
            'error_status':entry.get('error_status'),
            'retry_after_seconds':max(0,math.ceil(deadline-now.timestamp())),
        }
        return (200 if entry.get('good_reply') else entry.get('http_status',200)),reply

    def get(self, payload):
        with self.lock:
            now = self.clock()
            url = payload.get('url')
            provider = {CLAUDE_USAGE_URL:'claude',CLAUDE_RESET_USAGE_URL:'claude',
                        'https://chatgpt.com/backend-api/wham/usage':'codex'}.get(url)
            index = str(payload.get('authIndex',''))
            if not provider or not index or payload.get('method') != 'GET':
                return 400,{'status_code':400,'body':{}}
            cache_key = provider+':'+index
            entry = self.cache.get(cache_key)
            if entry is None:
                entry = self._cold_entry(provider,index,now)
                self.cache[cache_key] = entry
                self.save()
            if now.timestamp() < (number(entry.get('next_check_at')) or 0):
                return self._view(entry,now)
            # The browser still permits only plain usage. Upgrade its one shared
            # scheduled read to the exact official no-spend query internally.
            # Both URLs share the same cache key,600s deadline and Retry-After.
            if provider == 'claude':
                payload=dict(payload,url=CLAUDE_RESET_USAGE_URL)
            status,reply = self.request(self.key,'/requests/api-call',payload)
            if not isinstance(reply,dict):
                reply = {}
            body = reply.get('body')
            upstream_status = reply.get('status_code',reply.get('statusCode'))
            try:
                if isinstance(body,str):
                    body = json.loads(body)
                windows = normalize(provider,body,now) if isinstance(body,dict) else []
            except Exception:
                windows = []
            if status == 200 and upstream_status == 200 and windows:
                prior = self.accounts.get(index,{})
                incoming = {w.get('bucket',''):w for w in windows}
                for old in prior.get('windows',[]):
                    bucket = old.get('bucket','')
                    if bucket not in incoming and bucket:
                        old = dict(old)
                        old.setdefault('observed_at',prior.get('observed_at'))
                        windows.append(old)
                if len(windows) > 64:
                    return self._fail(cache_key,entry,status,reply,now)
                self.accounts[index] = {'provider':provider,'observed_at':iso(now),'windows':windows}
                # Only quota response body is cached. Provider headers, client
                # request headers and the existing server key are never persisted.
                entry = {'good_reply':{'status_code':200,'body':safe_quota_body(provider,body,now)},'observed_at':iso(now),
                         'http_status':200,'error_status':None,'failures':0,
                         'next_check_at':now.timestamp()+INTERVAL}
                self.cache[cache_key] = entry
                self.save()
                return self._view(entry,now)
            return self._fail(cache_key,entry,status,reply,now)

    def _fail(self, cache_key, entry, status, reply, now):
        failures = min(5,int(entry.get('failures',0))+1)
        delay = max(INTERVAL,min(3600,INTERVAL*(2**failures)),retry_after(reply,now))
        error_status = reply.get('status_code',reply.get('statusCode',status))
        error_status = int(error_status) if isinstance(error_status,(int,float)) else 503
        if error_status < 100 or error_status == 200:
            error_status = 502
        entry.update({'error_status':error_status,'failures':failures,
                      'next_check_at':now.timestamp()+delay,'http_status':status or 503,
                      'error_reply':{'status_code':error_status,'body':{}}})
        self.cache[cache_key] = entry
        self.save()
        return self._view(entry,now)

    def tick(self):
        with self.lock:
            return self._tick()

    def _tick(self):
        now = self.clock()
        stamp = now.timestamp()
        if stamp >= self.next_list:
            self.next_list = stamp + INTERVAL
            status, body = self.request(self.key,'/credentials')
            if status == 200 and isinstance(body,dict) and isinstance(body.get('files'),list):
                records = {}
                for record in body['files'][:MAX_ACCOUNTS]:
                    if not isinstance(record,dict):
                        continue
                    provider = record.get('provider',record.get('type'))
                    provider = 'claude' if provider == 'anthropic' else provider
                    index = str(record.get('auth_index',''))
                    if provider in ('claude','codex') and index and not record.get('disabled'):
                        records[index] = (record,provider)
                changed_records = set(records) != set(self.records)
                self.records = records
                self.accounts = {index:account for index,account in self.accounts.items() if index in records}
                self.cache = {key:entry for key,entry in self.cache.items() if key.split(':',1)[-1] in records}
                if changed_records:
                    self.save()
        changed = False
        for index,(record,provider) in self.records.items():
            before = self.accounts.get(index,{}).get('observed_at')
            self.get(usage_payload(record,provider))
            changed = changed or before != self.accounts.get(index,{}).get('observed_at')
        return {'account_count':len(self.accounts),'refreshed':changed}

    def save(self):
        write_private_json(self.snapshot_path,{'version':1,'accounts':self.accounts})
        write_private_json(self.cache_path,{'version':1,'entries':self.cache})
