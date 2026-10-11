"""Conditional NEW-session quota projection; never a claim of proxy eligibility.

Reads existing normalized quota metadata only. No network, selector invocation,
session-cache mutation, token access or scheduling changes.
"""
from datetime import datetime, timezone, timedelta
import math

FRESH_SECONDS = 900  # Matches the active weekly-reset selector's timing margin.
MAX_RECORDS = 64
MAX_WINDOWS = 64


def timestamp(value):
    if not isinstance(value,str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def canonical_provider(value):
    return {'anthropic':'claude','openai':'codex'}.get(value,value)


def applies(window, model):
    exact = window.get('model')
    prefix = window.get('model_prefix')
    return (not exact or model.lower() == str(exact).lower()) and (
        not prefix or model.lower().startswith(str(prefix).lower()))


def known_cooldown(record, model, now):
    views = record.get('cooldowns')
    if isinstance(views,list):
        for view in views[:MAX_WINDOWS]:
            if not isinstance(view,dict):
                continue
            retry = timestamp(view.get('retry_at'))
            if not retry or retry <= now:
                continue
            if view.get('scope') == 'credential':
                return True
            if view.get('scope') == 'model' and str(view.get('model_key','')).strip().lower() == model.strip().lower():
                return True
        # A complete local timer view outranks aggregate next_retry_after:
        # another model's cooldown must not become a credential-wide claim.
        return False
    retry = timestamp(record.get('next_retry_after'))
    return bool(retry and retry > now)


def project_quota_priority(snapshot, records, models, now=None):
    """Return auth-index -> safe DTO for the specified per-provider model context.

    A candidate is the earliest known fresh weekly reset in this observed pool.
    Actual proxy checks model support/auth/cooldown/configured priorities and
    existing affinity. The UI must retain that qualification in its tooltip.
    """
    now = now or datetime.now(timezone.utc)
    accounts = snapshot.get('accounts',{}) if isinstance(snapshot,dict) and snapshot.get('version') == 1 else {}
    result = {}
    candidates = {}
    for record in records[:MAX_RECORDS]:
        if not isinstance(record,dict):
            continue
        index = str(record.get('auth_index',''))
        provider = canonical_provider(record.get('provider',record.get('type')))
        model = models.get(provider)
        if not index or len(index) > 256 or provider not in ('claude','codex'):
            continue
        account = accounts.get(index,{})
        row = {'kind':'unknown','model':model,'reasonCode':'quota_missing',
               'freshness':'missing','quotasObservedAt':account.get('observed_at'),
               'weeklyResetAt':None,'scope':'new-session-only','conditional':True}
        result[index] = row
        if not isinstance(model,str) or not model or len(model) > 256:
            row['reasonCode'] = 'model_context_missing'
            continue
        if record.get('disabled') is True or record.get('status') == 'disabled':
            row.update(kind='quota-blocked',reasonCode='disabled')
            continue
        if record.get('unavailable') is True:
            row.update(kind='quota-blocked',reasonCode='known_unavailable')
            continue
        if known_cooldown(record,model,now):
            row.update(kind='quota-blocked',reasonCode='known_cooldown')
            continue
        if record.get('status') in ('error','pending','refreshing','unknown'):
            row['reasonCode'] = 'authentication_or_availability_unknown'
            continue
        if canonical_provider(account.get('provider')) != provider:
            continue
        observed = timestamp(account.get('observed_at'))
        if not observed:
            continue
        account_stale = observed > now+timedelta(seconds=60) or (now-observed).total_seconds() > FRESH_SECONDS
        row['freshness'] = 'stale' if account_stale else 'fresh'
        windows = account.get('windows')
        if not isinstance(windows,list) or len(windows) > MAX_WINDOWS:
            row['reasonCode'] = 'quota_shape_unknown'
            continue
        reset_candidates = []
        unknown = False
        blocked = None
        for window in windows:
            if not isinstance(window,dict) or not applies(window,model):
                continue
            value = window.get('used_percent')
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 100:
                unknown = True
                continue
            # The active selector remembers exhausted buckets through poll
            # failures and reset rollover until successful revalidation.
            if value >= 100:
                blocked = 'five_hour_exhausted' if window.get('kind') == 'five_hour' else 'weekly_exhausted' if window.get('kind') == 'weekly' else 'quota_exhausted'
                break
            seen = timestamp(window.get('observed_at')) or observed
            if seen > now+timedelta(seconds=60) or (now-seen).total_seconds() > FRESH_SECONDS:
                unknown = True
                continue
            if window.get('kind') == 'weekly':
                reset = timestamp(window.get('reset_at'))
                if reset and reset > now:
                    reset_candidates.append(reset)
                else:
                    unknown = True
        if blocked:
            row.update(kind='quota-blocked',reasonCode=blocked)
            continue
        if account_stale:
            row.update(reasonCode='quota_stale',freshness='stale')
            continue
        if unknown or not reset_candidates:
            row.update(reasonCode='quota_reset_unknown')
            continue
        earliest = min(reset_candidates)
        row.update(kind='quota-eligible',reasonCode='fresh_quota_remaining',
                   weeklyResetAt=earliest.isoformat().replace('+00:00','Z'))
        candidates.setdefault(provider,[]).append((earliest,str(record.get('id',index)),index))
    for provider,pool in candidates.items():
        _,_,index = min(pool)
        result[index].update(kind='quota-candidate',reasonCode='earliest_known_weekly_reset')
    return result
