"""Daypart explicit keyword bids using durable, non-compounding baselines."""
import math
import os
from contextlib import nullcontext
from automation_store import GCSState
from amazon_results import batch_outcome


def retune_keywords(client, ad_groups, metrics, mode, apply_live, target_bid, protect_bid, campaign_multipliers=None):
    all_keywords = {cid: client.list_keywords(cid) for cid in
                    sorted({str(g['campaignId']) for g in ad_groups})}
    if not any(kw.get('bid') is not None and str(kw.get('state', '')).upper() == 'ENABLED'
               for keywords in all_keywords.values() for kw in keywords):
        return {'keywords_seen': 0, 'updates_needed': 0, 'updates_applied': 0,
                'update_errors': 0, 'preview': [], 'outcomes': []}
    store = GCSState('keyword-baselines') if apply_live or os.getenv('AUTOMATION_STATE_BUCKET') else None
    context = store.locked() if apply_live else nullcontext(store.read() if store else {})
    with context as state:
        baselines = state.setdefault('baselines', {})
        groups = {str(g['adGroupId']): g for g in ad_groups}
        updates, preview = [], []
        campaign_multipliers = campaign_multipliers or {}
        for campaign_id in sorted({str(g['campaignId']) for g in ad_groups}):
            for kw in all_keywords[campaign_id]:
                kid, gid = str(kw.get('keywordId') or ''), str(kw.get('adGroupId') or '')
                if not kid or gid not in groups or str(kw.get('state', '')).upper() != 'ENABLED':
                    continue
                # Keywords inheriting the default already follow ad-group dayparting.
                if kw.get('bid') is None:
                    continue
                current = float(kw['bid'])
                if not math.isfinite(current) or current <= 0:
                    raise ValueError('Invalid keyword bid')
                baseline = float(baselines.setdefault(kid, current))
                if not math.isfinite(baseline) or baseline <= 0:
                    raise ValueError('Invalid persisted keyword baseline')
                target = target_bid(baseline, mode)
                target = max(0.10, min(2.50, target * float(campaign_multipliers.get(campaign_id, 1.0))))
                campaign_metrics = metrics.get(campaign_id)
                if campaign_metrics:
                    target, _, _ = protect_bid(target, baseline, campaign_metrics, current_bid=current)
                else:
                    target = min(target, current)
                preview.append({'keywordId': kid, 'campaignId': campaign_id, 'currentBid': current, 'baselineBid': baseline, 'salesMultiplier': float(campaign_multipliers.get(campaign_id, 1.0)), 'newBid': target})
                if abs(target - current) >= .01:
                    updates.append({'keywordId': kid, 'bid': target})
        if store and apply_live:
            # Persist before writing Amazon so retries/restarts retain the original baseline.
            store.save(state)
        outcomes = []
        if apply_live:
            for offset in range(0, len(updates), 100):
                chunk = updates[offset:offset + 100]
                outcomes.append(batch_outcome(client.put('/sp/keywords', {'keywords': chunk}), 'keywords', len(chunk)))
        return {'keywords_seen': len(preview), 'updates_needed': len(updates),
                'updates_applied': sum(o['accepted'] for o in outcomes),
                'update_errors': sum(o['failed'] for o in outcomes),
                'preview': preview[:25], 'outcomes': outcomes}
