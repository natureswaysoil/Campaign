"""Validated, replayable decision data. Money is USD; timestamps are UTC.

Hourly records must be complete cumulative buckets, never unmerged Stream deltas.
Different grains remain separate so campaign/keyword/placement totals cannot double count.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from automation_store import GCSState

ET = ZoneInfo(os.getenv('AMAZON_ADS_TIMEZONE', 'America/New_York'))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ProductExploration(StrictModel):
    cvr: float = Field(gt=0, le=.03)
    daily_budget: float = Field(gt=0, le=5)


class Policy(StrictModel):
    daily_limit: float = Field(gt=0, le=100000)
    product_exploration: dict[str, ProductExploration] = Field(default_factory=dict)
    currency: Literal['USD'] = 'USD'
    timezone: str = ET.key
    target_acos: float = Field(default=.25, gt=0, le=1)
    profit_reserve: float = Field(default=.10, ge=0, lt=1)
    max_cpc: float = Field(default=2.50, gt=0, le=100)
    min_cpc: float = Field(default=.02, gt=0, le=1)
    attribution_days: int = Field(default=14, ge=14, le=30)
    minimum_clicks: int = Field(default=30, ge=10)
    # Explicit, opt-in exploration when mature conversion evidence is missing.
    exploration_daily_budget: float = Field(default=5., gt=0, le=25)
    exploration_cvr: float = Field(default=0, ge=0, le=.03)
    spend_buffer: float = Field(default=.15, ge=.05, le=.5)
    costs_max_age_days: int = Field(default=30, ge=1, le=90)
    data_max_age_hours: float = Field(default=36, gt=0, le=72)
    inventory_max_age_hours: float = Field(default=24, gt=0, le=48)
    spend_max_age_minutes: int = Field(default=60, ge=15, le=120)
    reserve_days: float = Field(default=7, ge=1, le=90)
    pacing_enabled: bool = False
    placements_enabled: bool = False

    @model_validator(mode='after')
    def valid_range(self):
        if self.timezone != ET.key:
            raise ValueError('Policy timezone must match AMAZON_ADS_TIMEZONE and the Ads profile')
        if self.min_cpc > self.max_cpc:
            raise ValueError('min_cpc exceeds max_cpc')
        return self


class Product(StrictModel):
    asin: str = Field(pattern=r'^[A-Z0-9]{10}$')
    sku: str = Field(min_length=1, max_length=100)
    price: float = Field(gt=0)
    cogs: float = Field(ge=0)
    fees: float = Field(ge=0)
    shipping: float = Field(ge=0)
    other_cost: float = Field(default=0, ge=0)
    fee_source: Literal['estimate', 'actual', 'manual'] = 'manual'
    updated_at: float = Field(gt=0)
    market_at: float | None = Field(default=None, gt=0)
    inventory: int | None = Field(default=None, ge=0)
    inventory_at: float | None = Field(default=None, gt=0)
    daily_units: float | None = Field(default=None, ge=0)
    velocity_at: float | None = Field(default=None, gt=0)
    restock_date: date | None = None
    lead_days: float = Field(default=7, ge=0, le=365)
    category: str = ''

    @model_validator(mode='after')
    def timestamps(self):
        if any(v and v > time.time() + 300 for v in (self.updated_at, self.market_at, self.inventory_at, self.velocity_at)):
            raise ValueError('Future observation timestamp')
        return self


class Metric(StrictModel):
    grain: Literal['campaign', 'keyword', 'placement', 'hourly', 'retail']
    date: date
    campaign_id: str = ''
    asin: str = ''
    keyword_id: str = ''
    target_id: str = ''
    placement: Literal['', 'PLACEMENT_TOP', 'PLACEMENT_REST_OF_SEARCH', 'PLACEMENT_PRODUCT_PAGE', 'PLACEMENT_OFF_AMAZON'] = ''
    hour: int | None = Field(default=None, ge=0, le=23)
    # offset distinguishes the repeated Eastern hour at DST fall-back.
    utc_offset: str = ''
    clicks: int = Field(default=0, ge=0)
    orders: int = Field(default=0, ge=0)
    units: int = Field(default=0, ge=0)
    impressions: int = Field(default=0, ge=0)
    spend: float = Field(default=0, ge=0)
    sales: float = Field(default=0, ge=0)
    observed_at: float = Field(gt=0)

    @model_validator(mode='after')
    def dimensions(self):
        if self.grain == 'retail' and not self.asin:
            raise ValueError('Retail data requires asin')
        if self.grain != 'retail' and not self.campaign_id:
            raise ValueError('Ad metrics require campaign_id')
        if self.grain == 'keyword' and not (self.keyword_id or self.target_id):
            raise ValueError('Targeting metrics require keyword_id or target_id')
        if self.grain == 'placement' and not self.placement:
            raise ValueError('Placement required')
        if self.grain == 'hourly' and (self.hour is None or self.utc_offset not in ('-04:00', '-05:00', '-06:00', '-07:00', '-08:00', '-09:00', '-10:00')):
            raise ValueError('Hourly buckets require Eastern hour and UTC offset')
        if self.observed_at > time.time() + 300 or self.date > datetime.now(ET).date():
            raise ValueError('Future metrics')
        return self


def metric_key(row):
    return '|'.join(str(row.get(k, '')) for k in
                    ('grain', 'date', 'campaign_id', 'asin', 'keyword_id', 'target_id', 'placement', 'hour', 'utc_offset'))


def ingest(products=None, metrics=None, source='manual', complete_spend_at=None, replace_window=None):
    """Replace by natural key, retain original imports; retries never add totals twice."""
    products = [Product.model_validate(p).model_dump(mode='json') for p in (products or [])]
    metrics = [Metric.model_validate(m).model_dump(mode='json') for m in (metrics or [])]
    document = {'source': source, 'products': products, 'metrics': metrics,
                'complete_spend_at': complete_spend_at, 'replace_window': replace_window}
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    store = GCSState('profit-data')
    with store.locked() as state:
        # Archive before updating the materialized view. Failure prevents publication.
        GCSState('profit-history/' + digest).save(document)
        catalog = state.setdefault('products', {})
        for p in products:
            old = catalog.get(p['asin'])
            if not old or p['updated_at'] >= old['updated_at']:
                if old:
                    for stamp, fields in [('inventory_at', ('inventory',)), ('velocity_at', ('daily_units',)), ('market_at', ('price', 'fees', 'fee_source'))]:
                        if (old.get(stamp) or 0) > (p.get(stamp) or 0):
                            p[stamp] = old[stamp]
                            for field in fields:
                                p[field] = old[field]
                catalog[p['asin']] = p
        current = {metric_key(r): r for r in state.get('metrics', [])}
        if replace_window:
            grain, start, end, observed = replace_window
            current = {k: r for k, r in current.items() if not (
                r['grain'] == grain and start <= r['date'] <= end and r['observed_at'] <= observed)}
        for row in metrics:
            key = metric_key(row)
            if key not in current or row['observed_at'] >= current[key]['observed_at']:
                current[key] = row
        oldest = (datetime.now(ET).date() - timedelta(days=400)).isoformat()
        state['metrics'] = [r for r in current.values() if r['date'] >= oldest]
        if complete_spend_at is not None:
            state['complete_spend_at'] = max(state.get('complete_spend_at', 0), complete_spend_at)
        state['updated_at'] = time.time()
        state['last_import'] = digest
        store.save(state)
    return {'products': len(products), 'metrics': len(metrics), 'import_id': digest}


def fresh(ts, hours, now):
    return ts is not None and 0 <= now - float(ts) <= hours * 3600


def mature_rows(data, policy, now, **filters):
    today = datetime.fromtimestamp(now, ET).date()
    last = (today - timedelta(days=policy.attribution_days + 1)).isoformat()
    first = (today - timedelta(days=90)).isoformat()
    return [r for r in data.get('metrics', [])
            if first <= r['date'] <= last
            and fresh(r['observed_at'], policy.data_max_age_hours, now)
            and r['observed_at'] >= datetime.combine(date.fromisoformat(r['date']) + timedelta(days=policy.attribution_days + 1), datetime.min.time(), ET).timestamp()
            and all(r.get(k) == v for k, v in filters.items())]


def totals(rows):
    return {k: sum(r.get(k, 0) for r in rows) for k in ('clicks', 'orders', 'sales', 'spend', 'impressions')}


def economics(product, policy, now):
    p = Product.model_validate(product)
    if not fresh(p.updated_at, policy.costs_max_age_days * 24, now):
        raise ValueError('stale_economics')
    if not fresh(p.market_at or p.updated_at, policy.data_max_age_hours, now):
        raise ValueError('stale_price_or_fees')
    margin = (p.price - p.cogs - p.fees - p.shipping - p.other_cost) / p.price
    return {'margin': margin, 'target_acos': max(0, min(policy.target_acos, margin - policy.profit_reserve)),
            'fee_source': p.fee_source}


def inventory_factor(product, policy, now):
    if product.get('inventory') is None or not fresh(product.get('inventory_at'), policy.inventory_max_age_hours, now):
        raise ValueError('missing_or_stale_inventory')
    if product['inventory'] == 0:
        return 0., 0.
    if product.get('daily_units') is None or not fresh(product.get('velocity_at'), policy.data_max_age_hours, now):
        raise ValueError('missing_or_stale_sales_velocity')
    velocity = product['daily_units']
    if velocity == 0:
        return 1., None
    cover = product['inventory'] / velocity
    needed = product.get('lead_days', 7) + policy.reserve_days
    if product.get('restock_date'):
        days = (date.fromisoformat(str(product['restock_date'])) - datetime.fromtimestamp(now, ET).date()).days
        # Past restock dates do not imply stock has arrived.
        if days >= 0:
            needed = max(needed, days + policy.reserve_days)
    return min(1., cover / max(1., needed)), cover


def cpc_ceiling(product, rows, policy, now):
    econ = economics(product, policy, now)
    stock_factor, cover = inventory_factor(product, policy, now)
    if econ['target_acos'] <= 0 or stock_factor == 0:
        return {'max_cpc': 0., 'aov': product['price'], 'cvr': 0., 'days_cover': cover, 'inventory_factor': stock_factor, 'source': 'no_ad_allowance', **econ}
    t = totals(rows)
    if t['clicks'] >= policy.minimum_clicks:
        # Beta(1,19) prior prevents one lucky conversion from dominating the bid.
        cvr = min(1., (t['orders'] + 1) / (t['clicks'] + 20))
        # Do not let cross-ASIN attributed basket sales inflate this ASIN's ceiling.
        aov = min(product['price'], t['sales'] / t['orders']) if t['orders'] else product['price']
        source = 'mature_target_metrics'
    elif product.get('asin') in policy.product_exploration or policy.exploration_cvr > 0:
        opt_in = policy.product_exploration.get(product.get('asin'))
        cvr, aov, source = opt_in.cvr if opt_in else policy.exploration_cvr, product['price'], 'explicit_exploration'
    else:
        raise ValueError('insufficient_mature_conversion_data')
    ceiling = min(policy.max_cpc, aov * econ['target_acos'] * cvr) * stock_factor
    # Floor down, never round above the economic limit.
    return {'max_cpc': math.floor(ceiling * 100 + 1e-9) / 100,
            'aov': aov, 'cvr': cvr, 'days_cover': cover, 'inventory_factor': stock_factor,
            'source': source, **econ}


def learned_multiplier(rows, policy, now, placement=None):
    """Conservative weekday/hour or placement lift; insufficient evidence is neutral."""
    base = totals(rows)
    if base['clicks'] < policy.minimum_clicks * 3 or base['orders'] < 6:
        return 1.
    current = datetime.fromtimestamp(now, ET)
    sample = [r for r in rows if (r.get('placement') == placement if placement else
              date.fromisoformat(r['date']).weekday() == current.weekday() and r.get('hour') == current.hour)]
    t = totals(sample)
    if t['clicks'] < policy.minimum_clicks or len({r['date'] for r in sample}) < 3:
        return 1.
    base_value = base['sales'] / max(1, base['clicks'])
    return max(.5, min(1.25, (t['sales'] / t['clicks']) / base_value)) if base_value else .5


def pacing(data, policy, now):
    today = datetime.fromtimestamp(now, ET).date().isoformat()
    spend = sum(r['spend'] for r in data.get('metrics', []) if r['grain'] == 'campaign' and r['date'] == today)
    observed = data.get('complete_spend_at')
    is_fresh = fresh(observed, policy.spend_max_age_minutes / 60, now) and datetime.fromtimestamp(observed, ET).date().isoformat() == today
    threshold = policy.daily_limit * (1 - policy.spend_buffer)
    if not is_fresh:
        return {'fresh': False, 'spend': spend, 'multiplier': 1., 'stop': False}
    if spend >= threshold:
        return {'fresh': True, 'spend': spend, 'multiplier': 0., 'stop': True}
    hour = datetime.fromtimestamp(now, ET).hour
    rows = mature_rows(data, policy, now, grain='hourly')
    weights = [1. for _ in range(24)]
    if sum(r['clicks'] for r in rows) >= policy.minimum_clicks * 24:
        weights = [1 + sum(r['sales'] for r in rows if r['hour'] == h) for h in range(24)]
    fraction = sum(weights[:hour + 1]) / sum(weights)
    allowance = threshold * fraction
    multiplier = max(.25, min(1., allowance / spend)) if spend else 1.
    return {'fresh': True, 'spend': spend, 'multiplier': multiplier, 'stop': False, 'allowance': allowance}


def report_is_mature(end_date, now=None):
    try:
        end = date.fromisoformat(str(end_date))
        return end <= datetime.fromtimestamp(now or time.time(), ET).date() - timedelta(days=15)
    except ValueError:
        return False
