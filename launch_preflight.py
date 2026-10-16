"""Shared, read-only launch authorization; writes still revalidate in Guard."""
import math
import time
from automation_store import GCSState
from profit_guard import Guard, GuardBlocked, resolve_launch_product
from profit_intelligence import Policy, cpc_ceiling, mature_rows, conversion_evidence


def launch_preflight(client, *, asin, sku, plans, resume=None):
    now = time.time()
    data = GCSState('profit-data').read()
    product = resolve_launch_product(data.get('products', {}), asin, sku)
    raw = GCSState('profit-policy').read().get('policy')
    if not raw:
        raise GuardBlocked('Profit policy required')
    policy = Policy.model_validate(raw)
    ledger = GCSState('profit-write-guard').read()
    guard = Guard(client, data, policy, ledger, now)
    resume = resume or {}
    prepared = []
    for plan in plans:
        cid = str(resume.get(plan['type']) or '')
        rows = mature_rows(data, policy, now, grain='campaign', campaign_id=cid) if cid else []
        cap = cpc_ceiling(product, rows, policy, now)
        allowance = policy.product_exploration.get(product['asin'])
        daily = min(plan['budget'], allowance.daily_budget) if allowance else plan['budget']
        if not math.isfinite(daily) or daily < 1:
            raise GuardBlocked('Daily campaign budget must be at least $1')
        if cid:
            campaign = guard.campaigns.get(cid)
            if not campaign or campaign.get('state') != 'PAUSED':
                raise GuardBlocked('Resume requires a uniquely paused campaign')
            existing = guard.campaign_products(cid)
            if any(p['asin'] != product['asin'] or p['sku'] != product['sku'] for p in existing):
                raise GuardBlocked('Resume product identity mismatch')
            factor = guard.effective_factor(campaign)
        else:
            factor = 1.
        bid = math.floor(min(plan['bid'], cap['max_cpc'] / factor) * 100 + 1e-9) / 100
        if not math.isfinite(bid) or bid < policy.min_cpc:
            raise GuardBlocked('Campaign cannot run within its margin/inventory ceiling')
        # A new campaign has no mature campaign demand; an explicit allowance is mandatory.
        if not cid and not allowance and not (policy.exploration_cvr > 0 and daily <= policy.exploration_daily_budget):
            raise GuardBlocked('New campaign requires an explicitly bounded exploration budget')
        if cid:
            guard.verify_budget_growth(cid, daily)
        guard.account_room(cid or 'new:' + plan['type'], daily)
        prepared.append({**plan, 'budget': daily, 'bid': bid, 'economic_cpc_cap': cap['max_cpc'],
                         'source': cap['source'], 'evidence': conversion_evidence(
                             data, policy, now, grain='campaign', campaign_id=cid)})
    return {'product': product, 'plans': prepared, 'total_daily_budget': round(sum(p['budget'] for p in prepared), 2),
            'exploration_budget_scope': 'per_campaign', 'report_timezone': policy.timezone}
