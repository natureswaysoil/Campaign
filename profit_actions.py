"""Previewable inventory, placement and financial controls plus deduplicated alerts."""
from __future__ import annotations
import time
from datetime import datetime, timedelta

from automation_store import GCSState
from profit_guard import Guard, GuardBlocked, budget
from profit_intelligence import Policy, ET, economics, inventory_factor, mature_rows, totals, pacing, learned_multiplier


def build_plan(client, data, policy, now=None):
    now = now or time.time()
    guard = Guard(client, data, policy, {}, now)
    actions, alerts, products = [], [], []
    pace = pacing(data, policy, now)

    def alert(code, subject, message, severity='warning'):
        alerts.append({'key': code + ':' + subject, 'code': code, 'subject': subject,
                       'message': message, 'severity': severity})

    if not pace['fresh']:
        alert('stale_spend', 'account', 'Current account spend is unavailable or stale; increases are blocked.')
    if pace['stop']:
        alert('account_limit', 'account', 'Spend reached the buffered daily limit; pause enabled campaigns.', 'critical')
    total_budget = sum(budget(c) for c in guard.campaigns.values() if c.get('state') == 'ENABLED')
    if total_budget > policy.daily_limit:
        alert('budget_envelope', 'account', 'Enabled campaign budgets exceed the configured account limit.', 'critical')
    for asin, p in data.get('products', {}).items():
        try:
            e = economics(p, policy, now)
            factor, cover = inventory_factor(p, policy, now)
            products.append({'asin': asin, 'sku': p['sku'], **e, 'days_cover': cover, 'inventory': p['inventory']})
            if e['target_acos'] <= 0:
                alert('no_ad_margin', asin, 'Product has no advertising allowance after costs and profit reserve.', 'critical')
            if factor < 1:
                alert('stock_risk', asin, 'Inventory cover is below lead time plus safety stock; bids are constrained.', 'critical' if factor == 0 else 'warning')
        except ValueError as exc:
            alert('product_data', asin, str(exc))
    for cid, c in guard.campaigns.items():
        if c.get('state') != 'ENABLED':
            continue
        if pace['stop']:
            actions.append({'endpoint': '/sp/campaigns', 'key': 'campaigns', 'row': {'campaignId': cid, 'state': 'PAUSED'}, 'reason': 'account_limit'})
            continue
        try:
            mapped = guard.campaign_products(cid)
            e = [economics(p, policy, now) for p in mapped]
            stock = [inventory_factor(p, policy, now)[0] for p in mapped]
            rows = mature_rows(data, policy, now, grain='campaign', campaign_id=cid)
            metric = totals(rows)
            margin = min(x['margin'] for x in e)
            if metric['spend'] > metric['sales'] * margin and metric['spend'] >= 5:
                alert('margin_breach', cid, 'Mature attributed sales contribution is below ad spend (estimated from product costs).', 'critical')
            if min(stock) == 0 or min(x['target_acos'] for x in e) <= 0:
                actions.append({'endpoint': '/sp/campaigns', 'key': 'campaigns', 'row': {'campaignId': cid, 'state': 'PAUSED'}, 'reason': 'inventory_or_margin'})
                continue
            factor = guard.effective_factor(c)
            groups = [g for g in guard.groups.values() if str(g.get('campaignId')) == cid and g.get('state') == 'ENABLED']
            entities = [('/sp/adGroups', 'adGroups', 'adGroupId', 'defaultBid', g) for g in groups]
            entities += [('/sp/keywords', 'keywords', 'keywordId', 'bid', r) for r in client.list_keywords(cid) if r.get('state') == 'ENABLED']
            entities += [('/sp/targets', 'targetingClauses', 'targetId', 'bid', r) for r in client.list_targets(cid) if r.get('state') == 'ENABLED']
            for endpoint, key, id_key, bid_key, entity in entities:
                if entity.get(bid_key) is None:
                    continue
                try:
                    cap = guard.ceiling(cid, entity) / factor
                except GuardBlocked as exc:
                    alert('bid_data', str(entity[id_key]), str(exc))
                    continue
                current = float(entity[bid_key])
                if cap < policy.min_cpc:
                    row = {id_key: str(entity[id_key]), 'state': 'PAUSED'}
                elif current > cap:
                    import math
                    row = {id_key: str(entity[id_key]), bid_key: math.floor(cap * 100 + 1e-9) / 100}
                else:
                    continue
                actions.append({'endpoint': endpoint, 'key': key, 'row': row, 'reason': 'margin_inventory_ceiling'})
            if policy.placements_enabled:
                rows = mature_rows(data, policy, now, grain='placement', campaign_id=cid)
                if not rows:
                    alert('placement_data', cid, 'No mature placement observations; placement controls held.')
                    continue
                old = c.get('dynamicBidding') or {'strategy': 'LEGACY_FOR_SALES'}
                placements = []
                for name in ('PLACEMENT_TOP', 'PLACEMENT_REST_OF_SEARCH', 'PLACEMENT_PRODUCT_PAGE'):
                    mult = learned_multiplier(rows, policy, now, placement=name)
                    placements.append({'placement': name, 'percentage': max(0, int((mult - 1) * 100))})
                desired = {**old, 'placementBidding': placements}
                if desired != old:
                    # Every placement proposal must pass the same effective-CPC bound as live writes.
                    try:
                        guard.verify_activation(cid, {**c, 'dynamicBidding': desired})
                        actions.append({'endpoint': '/sp/campaigns', 'key': 'campaigns',
                                        'row': {'campaignId': cid, 'dynamicBidding': desired}, 'reason': 'learned_placement'})
                    except GuardBlocked as exc:
                        alert('placement_capped', cid, str(exc))
        except (ValueError, GuardBlocked) as exc:
            alert('campaign_data', cid, str(exc))
    # Conversion diagnostics compare mature weeks; low-volume changes remain unclassified.
    today = datetime.fromtimestamp(now, ET).date()
    boundary = (today - timedelta(days=policy.attribution_days + 8)).isoformat()
    for cid in guard.campaigns:
        rows = mature_rows(data, policy, now, grain='campaign', campaign_id=cid)
        recent, previous = totals([r for r in rows if r['date'] > boundary]), totals([r for r in rows if boundary >= r['date'] > (today - timedelta(days=policy.attribution_days + 15)).isoformat()])
        if min(recent['clicks'], previous['clicks']) >= policy.minimum_clicks and previous['orders'] >= 3:
            if recent['orders'] / recent['clicks'] < .6 * previous['orders'] / previous['clicks']:
                alert('conversion_drop', cid, 'Mature conversion rate fell over 40%; inspect listing, price, reviews, and Featured Offer. Cause is not established.')
    return {'actions': actions, 'alerts': alerts, 'products': products, 'pacing': pace, 'enabled_budget_total': total_budget}


def update_alerts(alerts, now=None):
    now = now or time.time()
    store = GCSState('profit-alerts')
    with store.locked() as state:
        saved = state.setdefault('alerts', {})
        active = {a['key'] for a in alerts}
        for key, item in saved.items():
            if key not in active and not item.get('resolved_at'):
                item['resolved_at'] = now
        for item in alerts:
            old = saved.get(item['key'], {})
            saved[item['key']] = {**item, 'first_seen': old.get('first_seen', now), 'last_seen': now,
                                  'resolved_at': None, 'acknowledged_at': old.get('acknowledged_at')}
        state['checked_at'] = now
        store.save(state)
        return state


def control_tick(client, live=False):
    policy = Policy.model_validate(GCSState('profit-policy').read()['policy'])
    data = GCSState('profit-data').read()
    plan = build_plan(client, data, policy)
    if not live:
        return {'success': True, 'dry_run': True, **plan}
    from amazon_results import batch_outcome
    outcomes = []
    for action in plan['actions']:
        try:
            response = client.put(action['endpoint'], {action['key']: [action['row']]})
            outcome = batch_outcome(response, action['key'], 1)
            outcomes.append({'action': action, **outcome})
        except Exception as exc:
            outcomes.append({'action': action, 'success': False, 'error': str(exc)})
    update_alerts(plan['alerts'])
    result = {'success': all(r['success'] for r in outcomes), 'outcomes': outcomes, **plan, 'at': time.time()}
    GCSState('profit-control-result').save(result)
    return result
