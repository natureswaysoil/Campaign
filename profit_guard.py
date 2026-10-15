"""One mandatory guard for production Sponsored Products writes.

Serializes account mutations and reserves budget before requests. A failed or
ambiguous write retains its reservation; it never silently frees spending room.
The budget envelope is not an Amazon billing cap: delayed reporting/overdelivery
can exceed it. Spend surveillance pauses campaigns with a configurable buffer.
"""
from __future__ import annotations
import copy
import math
import time
from datetime import datetime
from uuid import uuid4

from automation_store import GCSState
from profit_intelligence import (ET, Policy, cpc_ceiling, mature_rows, learned_multiplier, pacing, totals, economics)

ENDPOINTS = {'/sp/campaigns': ('campaigns', 'campaignId'),
             '/sp/adGroups': ('adGroups', 'adGroupId'),
             '/sp/keywords': ('keywords', 'keywordId'),
             '/sp/targets': ('targetingClauses', 'targetId'),
             '/sp/productAds': ('productAds', 'adId')}


class GuardBlocked(RuntimeError):
    pass


def resolve_launch_product(products, asin, sku):
    """Resolve exact identity before creating any Amazon campaign objects."""
    asin, sku = str(asin or '').strip(), str(sku or '').strip()
    product = products.get(asin) if asin else None
    if not asin and sku:
        matches = [p for p in products.values() if p.get('sku') == sku]
        product = matches[0] if len(matches) == 1 else None
    if not product:
        raise ValueError('Launch blocked: no verified product costs mapped for ASIN '
                         + (asin or '(missing)') + '. Verify this product in the Profit dashboard before launching.')
    if not product.get('sku') or not product.get('asin'):
        raise ValueError('Launch blocked: verified product is missing its Amazon SKU or ASIN')
    return product


def amount(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise GuardBlocked('Invalid money amount')
    return number


def budget(row):
    value = row.get('budget', {})
    if isinstance(value, dict):
        if value.get('budgetType', 'DAILY') != 'DAILY':
            raise GuardBlocked('Only daily campaign budgets are supported')
        return amount(value.get('budget', 0))
    return amount(row.get('dailyBudget', value or 0))


def restrictive_only(method, rows):
    return method == 'PUT' and all(
        set(r) <= {'campaignId', 'adGroupId', 'keywordId', 'targetId', 'adId', 'state'}
        and r.get('state') in ('PAUSED', 'ARCHIVED') for r in rows)


class Guard:
    def __init__(self, client, data, policy, ledger, now):
        self.client, self.data, self.policy, self.ledger, self.now = client, data, policy, ledger, now
        self.campaigns = {str(r['campaignId']): r for r in client.list_campaigns()}
        self.groups = {str(r['adGroupId']): r for r in client.list_all('/sp/adGroups/list', 'adGroups')}
        self.ads, self.entities = {}, {}
        self.reservations = {}
        self.adjustments = []

    def campaign_products(self, cid):
        if cid not in self.ads:
            self.ads[cid] = self.client.list_product_ads(cid)
        products = self.data.get('products', {})
        result = []
        for ad in self.ads[cid]:
            if ad.get('state') == 'ARCHIVED':
                continue
            p = products.get(str(ad.get('asin') or ''))
            if not p and ad.get('sku'):
                matches = [v for v in products.values() if v['sku'] == ad['sku']]
                p = matches[0] if len(matches) == 1 else None
            if not p:
                raise GuardBlocked('Product ad lacks verified ASIN economics: ' + cid)
            if p not in result:
                result.append(p)
        # Explicit launch context, never a fuzzy campaign-name match.
        launch = getattr(self.client, 'profit_product_asin', None)
        if not result and launch and self.campaigns.get(cid, {}).get('state') == 'PAUSED':
            p = products.get(launch)
            if p:
                result.append(p)
        if not result:
            raise GuardBlocked('No verified products mapped to campaign: ' + cid)
        return result

    def old_entity(self, endpoint, row, method):
        key, id_key = ENDPOINTS[endpoint]
        if method == 'POST':
            return {}
        rid = str(row.get(id_key) or '')
        if endpoint == '/sp/campaigns':
            old = self.campaigns.get(rid)
        elif endpoint == '/sp/adGroups':
            old = self.groups.get(rid)
        else:
            if endpoint not in self.entities:
                # Global listing prevents callers from spoofing campaignId on updates.
                self.entities[endpoint] = {str(r[id_key]): r for r in self.client.list_all(endpoint + '/list', key)}
            old = self.entities[endpoint].get(rid)
        if old is None:
            raise GuardBlocked('Cannot verify existing entity: ' + rid)
        if row.get('campaignId') and old.get('campaignId') and str(row['campaignId']) != str(old['campaignId']):
            raise GuardBlocked('Campaign/entity mismatch')
        if row.get('adGroupId') and old.get('adGroupId') and str(row['adGroupId']) != str(old['adGroupId']):
            raise GuardBlocked('Ad-group/entity mismatch')
        return old

    def ceiling(self, cid, row=None):
        if not self.policy:
            raise GuardBlocked('Configure the profit policy before increasing spend')
        row = row or {}
        filters = {'campaign_id': cid, 'grain': 'campaign'}
        if row.get('keywordId'):
            filters.update(grain='keyword', keyword_id=str(row['keywordId']))
        elif row.get('targetId'):
            filters.update(grain='keyword', target_id=str(row['targetId']))
        rows = mature_rows(self.data, self.policy, self.now, **filters)
        # All advertised ASINs must be viable. Use the most conservative ceiling.
        try:
            caps = [cpc_ceiling(p, rows, self.policy, self.now)['max_cpc'] for p in self.campaign_products(cid)]
        except ValueError as exc:
            raise GuardBlocked(str(exc)) from exc
        return min(caps)

    def effective_factor(self, campaign):
        bidding = campaign.get('dynamicBidding') or {}
        strategy = bidding.get('strategy', 'LEGACY_FOR_SALES')
        # Conservatively allow 2x Amazon dynamic uplift; unknown strategies fail closed.
        if strategy not in ('LEGACY_FOR_SALES', 'AUTO_FOR_SALES', 'MANUAL'):
            raise GuardBlocked('Unsupported Amazon bidding strategy')
        dynamic = 2. if strategy == 'AUTO_FOR_SALES' else 1.
        adjustments = bidding.get('placementBidding') or []
        allowed = {'PLACEMENT_TOP', 'PLACEMENT_REST_OF_SEARCH', 'PLACEMENT_PRODUCT_PAGE'}
        for a in adjustments:
            if a.get('placement') not in allowed or amount(a.get('percentage', 0)) > 900:
                raise GuardBlocked('Unsupported placement adjustment')
        # Audience/rule bidding requires a separate verified bound.
        if set(bidding) - {'strategy', 'placementBidding'} or campaign.get('budgetRules'):
            raise GuardBlocked('Unbounded Amazon bidding/budget rule')
        return dynamic * (1 + max([amount(a.get('percentage', 0)) for a in adjustments] or [0]) / 100)

    def account_room(self, cid, proposed):
        if not self.policy:
            raise GuardBlocked('Account daily limit is not configured')
        pace = pacing(self.data, self.policy, self.now)
        if not pace['fresh']:
            raise GuardBlocked('Fresh complete account spend report required for increases')
        if pace['stop']:
            raise GuardBlocked('Account spend stop threshold reached')
        today = datetime.fromtimestamp(self.now, ET).date().isoformat()
        saved = self.ledger.get('budget_days', {}).get(today, {})
        envelope = {k: budget(c) for k, c in self.campaigns.items() if c.get('state') == 'ENABLED'}
        for k, value in {**saved, **self.reservations}.items():
            envelope[k] = max(envelope.get(k, 0), value)
        envelope[cid] = max(envelope.get(cid, 0), proposed)
        # Include observed spend from campaigns outside the enabled/reserved envelope.
        outside_spend = sum(r['spend'] for r in self.data.get('metrics', [])
                            if r['grain'] == 'campaign' and r['date'] == today and r['campaign_id'] not in envelope)
        if sum(envelope.values()) + outside_spend > self.policy.daily_limit + 1e-8:
            raise GuardBlocked('Aggregate enabled/reserved budgets exceed the account daily limit')
        self.reservations[cid] = envelope[cid]

    def verify_budget_growth(self, cid, proposed):
        if not self.policy:
            raise GuardBlocked('Profit policy required')
        rows = mature_rows(self.data, self.policy, self.now, grain='campaign', campaign_id=cid)
        t = totals(rows)
        target = min(economics(p, self.policy, self.now)['target_acos'] for p in self.campaign_products(cid))
        proven = t['clicks'] >= self.policy.minimum_clicks and t['orders'] >= 3 and t['sales'] > 0 and t['spend'] <= t['sales'] * target
        permitted = all(
            proposed <= self.policy.product_exploration[p['asin']].daily_budget
            if p['asin'] in self.policy.product_exploration else
            self.policy.exploration_cvr > 0 and proposed <= self.policy.exploration_daily_budget
            for p in self.campaign_products(cid))
        if not proven and not permitted:
            raise GuardBlocked('Budget growth needs mature profitable demand or an explicitly bounded exploration budget')

    def verify_activation(self, cid, campaign):
        cap = self.ceiling(cid)
        factor = self.effective_factor(campaign)
        if cap < self.policy.min_cpc:
            raise GuardBlocked('Campaign cannot run within its margin/inventory ceiling')
        for group in self.groups.values():
            if str(group.get('campaignId')) == cid and group.get('state') == 'ENABLED':
                if amount(group.get('defaultBid', 0)) * factor > cap + 1e-8:
                    raise GuardBlocked('Reduce ad-group bids before activating/increasing placement')
        for endpoint, key in (('/sp/keywords', 'keywords'), ('/sp/targets', 'targetingClauses')):
            for entity in self.client.list_all(endpoint + '/list', key, {'campaignIdFilter': {'include': [cid]}}):
                if entity.get('state') == 'ENABLED' and entity.get('bid') is not None:
                    if amount(entity['bid']) * factor > self.ceiling(cid, entity) + 1e-8:
                        raise GuardBlocked('Reduce explicit bids before activating/increasing placement')

    def prepare(self, method, endpoint, body):
        key, id_key = ENDPOINTS[endpoint]
        result = copy.deepcopy(body)
        for row in result[key]:
            old = self.old_entity(endpoint, row, method)
            merged = {**old, **row}
            cid = str(merged.get('campaignId') or '')
            if not cid and merged.get('adGroupId'):
                cid = str(self.groups.get(str(merged['adGroupId']), {}).get('campaignId') or '')
            if endpoint == '/sp/campaigns':
                if method == 'POST':
                    # Campaigns can only be built paused. Activation verifies real product ads.
                    if row.get('state') != 'PAUSED':
                        raise GuardBlocked('Create campaigns PAUSED, then validate and enable')
                    budget(row)
                    continue
                increased = budget(merged) > budget(old)
                activation = row.get('state') == 'ENABLED' and old.get('state') != 'ENABLED'
                placement = 'dynamicBidding' in row
                if activation or increased or placement:
                    self.verify_activation(cid, merged)
                    if activation or increased:
                        self.verify_budget_growth(cid, budget(merged))
                    self.account_room(cid, budget(merged))
                continue
            if not cid or cid not in self.campaigns:
                raise GuardBlocked('Campaign could not be resolved')
            campaign = self.campaigns[cid]
            bid_key = 'defaultBid' if endpoint == '/sp/adGroups' else 'bid'
            activation = row.get('state') == 'ENABLED' and old.get('state') != 'ENABLED'
            if endpoint == '/sp/productAds':
                if method == 'POST' or activation or set(row) - {'adId', 'campaignId', 'adGroupId', 'state'}:
                    # A new ASIN can invalidate every existing bid: add only while paused.
                    if campaign.get('state') != 'PAUSED':
                        raise GuardBlocked('Pause campaign before adding/enabling product ads')
                    products = self.data.get('products', {})
                    if not any((p['asin'] == row.get('asin') or p['sku'] == row.get('sku')) for p in products.values()):
                        raise GuardBlocked('Product requires verified economics')
                continue
            if bid_key not in row and not activation:
                continue
            current = amount(old.get(bid_key, 0))
            wanted = amount(row.get(bid_key, current))
            try:
                cap = self.ceiling(cid, merged)
                factor = self.effective_factor(campaign)
                cap /= factor
            except GuardBlocked:
                safe_fields = {'campaignId', 'adGroupId', id_key, bid_key}
                if method == 'PUT' and not activation and set(row) <= safe_fields and 0 < wanted <= current:
                    continue  # Reductions remain available on stale/missing data.
                raise
            pace = pacing(self.data, self.policy, self.now)
            if pace['stop'] or cap < self.policy.min_cpc:
                if method == 'POST':
                    raise GuardBlocked('No safe advertising allowance for new entity')
                row.pop(bid_key, None)
                row['state'] = 'PAUSED'
                self.adjustments.append({'id': str(row.get(id_key, 'new')), 'reason': 'margin_or_inventory_or_account_stop'})
                continue
            if self.policy.pacing_enabled:
                hourly = mature_rows(self.data, self.policy, self.now, grain='hourly', campaign_id=cid)
                wanted *= learned_multiplier(hourly, self.policy, self.now)
                wanted *= pace['multiplier']
            guarded = math.floor(min(wanted, cap) * 100 + 1e-9) / 100
            if guarded < self.policy.min_cpc:
                row.pop(bid_key, None)
                row['state'] = 'PAUSED'
                continue
            if method == 'POST' or activation or guarded > current:
                self.account_room(cid, budget(campaign))
            row[bid_key] = guarded
            if guarded != wanted:
                self.adjustments.append({'id': str(row.get(id_key, 'new')), 'reason': 'economic_ceiling', 'bid': guarded})
        return result


def guarded_write(client, method, endpoint, body, send):
    key, _ = ENDPOINTS[endpoint]
    rows = body.get(key, []) if isinstance(body, dict) else []
    if not rows or not isinstance(rows, list):
        raise GuardBlocked('Expected nonempty mutation batch')
    if restrictive_only(method, rows):
        return send(body)
    store = GCSState('profit-write-guard')
    with store.locked() as ledger:
        data = GCSState('profit-data').read()
        raw_policy = GCSState('profit-policy').read().get('policy')
        policy = Policy.model_validate(raw_policy) if raw_policy else None
        guard = Guard(client, data, policy, ledger, time.time())
        prepared = guard.prepare(method, endpoint, body)
        today = datetime.fromtimestamp(guard.now, ET).date().isoformat()
        days = ledger.setdefault('budget_days', {})
        day = days.setdefault(today, {})
        # Preserve the envelope already exposed today even if another workflow pauses it.
        for cid, campaign in guard.campaigns.items():
            if campaign.get('state') == 'ENABLED':
                day[cid] = max(day.get(cid, 0), budget(campaign))
        for cid, value in guard.reservations.items():
            day[cid] = max(day.get(cid, 0), value)
        ledger['budget_days'] = {k: v for k, v in days.items() if k >= today}
        audit_id = str(uuid4())
        audit = {'id': audit_id, 'at': guard.now, 'endpoint': endpoint, 'method': method,
                 'status': 'reserved', 'request': prepared, 'adjustments': guard.adjustments}
        GCSState('profit-audit/' + audit_id).save(audit)
        store.save(ledger)  # Must succeed before Amazon can receive a request.
        try:
            response = send(prepared)
        except Exception:
            audit['status'] = 'unconfirmed'
            GCSState('profit-audit/' + audit_id).save(audit)
            raise
        audit['status'] = 'responded'
        from amazon_results import batch_outcome
        audit['outcome'] = batch_outcome(response, key, len(rows))
        GCSState('profit-audit/' + audit_id).save(audit)
        response['_profit_guard'] = {'audit_id': audit_id, 'adjustments': guard.adjustments}
        return response
