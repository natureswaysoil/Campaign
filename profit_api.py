"""Authenticated profit dashboard APIs; no listing content is published here."""
import time
from datetime import date
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from automation_store import GCSState, StateBusy
from profit_intelligence import StrictModel, Policy, Product, Metric, ingest
from safety import live_requested


class DataImport(StrictModel):
    apply_live: bool = False
    dry_run: bool = False
    source: Literal['manual_verified', 'authorized_hourly_adapter'] = 'manual_verified'
    products: list[Product] = Field(default_factory=list, max_length=200)
    metrics: list[Metric] = Field(default_factory=list, max_length=10000)


class CostConfirmation(StrictModel):
    apply_live: bool = False
    dry_run: bool = False
    asin: str = Field(pattern=r'^[A-Z0-9]{10}$')
    sku: str = Field(min_length=1, max_length=100)
    cogs: float = Field(ge=0)
    shipping: float = Field(ge=0)
    other_cost: float = Field(ge=0)


def save_confirmed_costs(payload):
    from decision_sync import SellingPartnerClient, refresh_product_market
    with GCSState('profit-write-guard').locked():
        existing = GCSState('profit-data').read().get('products', {}).get(payload.asin, {})
        candidate = {**existing, 'asin': payload.asin, 'sku': payload.sku,
                     'cogs': payload.cogs, 'shipping': payload.shipping,
                     'other_cost': payload.other_cost, 'updated_at': time.time()}
        # Verify exact SKU/ASIN against Amazon; no placeholder fees or sales velocity.
        product = Product.model_validate(refresh_product_market(
            SellingPartnerClient(), candidate, fbm_only=True)).model_dump(mode='json')
        ingest(products=[product], source='owner_confirmed:dashboard')
        saved = GCSState('profit-data').read().get('products', {}).get(payload.asin)
        if not saved or any(saved.get(k) != product[k] for k in ('sku', 'cogs', 'shipping', 'other_cost', 'updated_at')):
            raise RuntimeError('Cost save read-back failed')
        return {'success': True, 'asin': payload.asin}


class Experiment(StrictModel):
    asin: str = Field(pattern=r'^[A-Z0-9]{10}$')
    kind: Literal['title', 'main_image', 'bullets', 'description', 'a_plus', 'brand_story']
    hypothesis: str = Field(min_length=5, max_length=2000)
    status: Literal['planned', 'running', 'completed'] = 'planned'
    brand_registry_verified: bool = False
    asin_eligible_verified: bool = False
    eligibility_checked_on: date | None = None
    amazon_experiment_id: str = Field(default='', max_length=200)
    result_notes: str = Field(default='', max_length=4000)
    winner: Literal['', 'A', 'B', 'inconclusive'] = ''

    def validate_eligibility(self):
        if self.status != 'planned':
            if not (self.brand_registry_verified and self.asin_eligible_verified and self.amazon_experiment_id):
                raise ValueError('Verify Brand Registry/ASIN eligibility and enter the Amazon experiment ID')
            if self.eligibility_checked_on is None or not 0 <= (date.today() - self.eligibility_checked_on).days <= 30:
                raise ValueError('Eligibility must have been checked in Seller Central within 30 days')
        if self.status == 'completed' and (not self.winner or not self.result_notes):
            raise ValueError('Record Amazon result notes and winner (or inconclusive)')


def register(app):
    @app.post('/api/profit/cost-import/key')
    def cost_key(payload: dict):
        if not live_requested(payload):
            return {'dry_run': True}
        from verified_costs import import_public_key
        return {'public_key': import_public_key()}

    @app.post('/api/profit/cost-import')
    def cost_import(payload: dict):
        if not live_requested(payload):
            return {'dry_run': True}
        from verified_costs import import_encrypted_costs
        packet = payload.get('packet')
        if not isinstance(packet, str) or len(packet) > 1024:
            raise HTTPException(422, 'Invalid encrypted cost packet')
        try:
            return import_encrypted_costs(packet, update_existing=payload.get('confirm_existing_updates') is True)
        except Exception as exc:
            # Never include decrypted data, key material or raw Amazon errors.
            raise HTTPException(503, 'Private cost import failed: ' + type(exc).__name__) from None

    @app.get('/profit')
    def page():
        from pathlib import Path
        return FileResponse(Path(__file__).parent / 'templates' / 'profit.html')

    @app.get('/api/profit/launch-diagnostic/{campaign_id}')
    def launch_diagnostic(campaign_id: str):
        # Read-only, authenticated by the same middleware as all profit APIs.
        # Names are used only to locate candidates, never to authorize spending.
        from optimize_campaigns import AmazonAdsClient, load_products, normalized_product
        from server import _sanitize_name
        client = AmazonAdsClient()
        campaign = next((c for c in client.list_campaigns()
                         if str(c.get('campaignId')) == campaign_id), None)
        if not campaign:
            raise HTTPException(404, 'Campaign not found')
        products = GCSState('profit-data').read().get('products', {})
        ads = client.list_product_ads(campaign_id)
        candidates = []
        name = str(campaign.get('name') or '')
        for row in load_products():
            product = normalized_product(row)
            prefix = _sanitize_name(str(product.get('title') or 'Product'))[:70] + ' | '
            if name.startswith(prefix):
                asin = str(product.get('asin') or '')
                candidates.append({'asin': asin, 'verified': asin in products})
        return {'campaign_id': campaign_id, 'state': campaign.get('state'),
                'product_ads': len(ads), 'catalog_candidates': candidates}

    @app.get('/api/profit/eligibility/{campaign_id}')
    def eligibility(campaign_id: str, keyword_id: str = '', target_id: str = ''):
        from optimize_campaigns import AmazonAdsClient
        from profit_guard import bid_preflight, GuardBlocked
        client = AmazonAdsClient()
        if keyword_id and target_id:
            raise HTTPException(422, 'Select a keyword or target')
        if keyword_id or target_id:
            endpoint = '/sp/keywords' if keyword_id else '/sp/targets'
            row = {'campaignId': campaign_id, 'keywordId' if keyword_id else 'targetId': keyword_id or target_id}
        else:
            groups = client.list_ad_groups(campaign_id)
            if not groups:
                raise HTTPException(409, 'Campaign has no ad group; launch preflight is required')
            endpoint, row = '/sp/adGroups', {'campaignId': campaign_id, 'adGroupId': str(groups[0]['adGroupId'])}
        try:
            return bid_preflight(client, endpoint, row)
        except GuardBlocked as exc:
            return {'status': 'blocked', 'reason': str(exc)}

    @app.get('/api/profit/status')
    def status():
        try:
            policy = GCSState('profit-policy').read()
            data = GCSState('profit-data').read()
            from optimizer_settings import effective_settings
            return {'effective_settings': effective_settings(policy.get('policy')), 'policy': policy.get('policy'), 'products': list(data.get('products', {}).values()),
                    'metric_count': len(data.get('metrics', [])), 'data_updated_at': data.get('updated_at'),
                    'complete_spend_at': data.get('complete_spend_at'),
                    'alerts': GCSState('profit-alerts').read(), 'controls': GCSState('profit-control-result').read(),
                    'sync': GCSState('profit-sync-status').read(), 'experiments': GCSState('profit-experiments').read()}
        except Exception as exc:
            raise HTTPException(503, 'Profit state unavailable: ' + type(exc).__name__)

    @app.post('/api/profit/policy')
    def policy(payload: dict):
        try:
            parsed = Policy.model_validate(payload.get('policy', {})).model_dump()
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not live_requested(payload):
            return {'dry_run': True, 'policy': parsed}
        store = GCSState('profit-policy')
        # Serialize policy changes with ad mutations; never change a limit mid-write.
        with GCSState('profit-write-guard').locked():
            with store.locked() as state:
                state.update(policy=parsed, updated_at=time.time())
                store.save(state)
        return {'success': True, 'policy': parsed}

    @app.post('/api/profit/confirm-costs')
    def confirm_costs(payload: CostConfirmation):
        if not payload.apply_live or payload.dry_run:
            return {'dry_run': True}
        try:
            return save_confirmed_costs(payload)
        except Exception:
            raise HTTPException(503, 'Could not verify and save this product. Check its exact Amazon SKU and ASIN, then retry. No launch was performed.') from None

    @app.post('/api/profit/data')
    def import_data(payload: DataImport):
        if not payload.apply_live or payload.dry_run:
            return {'dry_run': True, 'products': len(payload.products), 'metrics': len(payload.metrics)}
        # Imports cannot claim a complete account spend snapshot. Only verified Ads sync can.
        if payload.source == 'authorized_hourly_adapter' and any(m.grain != 'hourly' for m in payload.metrics):
            raise HTTPException(422, 'Hourly adapter accepts hourly cumulative buckets only')
        if payload.source != 'authorized_hourly_adapter' and any(m.grain == 'hourly' for m in payload.metrics):
            raise HTTPException(422, 'Hourly metrics require the authorized hourly adapter source')
        return ingest(products=[p.model_dump(mode='json') for p in payload.products],
                      metrics=[m.model_dump(mode='json') for m in payload.metrics], source=payload.source)

    @app.post('/api/profit/control')
    def controls(payload: dict):
        from optimize_campaigns import AmazonAdsClient
        from profit_actions import control_tick
        try:
            result = control_tick(AmazonAdsClient(), live_requested(payload))
            return JSONResponse(result, status_code=200 if result['success'] else 502)
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, 'Configure verified profit data and policy: ' + str(exc))

    @app.post('/api/automation/profit-tick')
    def sync(payload: dict):
        if not live_requested(payload):
            return {'dry_run': True, 'message': 'No reports, state writes or advertising changes requested'}
        from optimize_campaigns import AmazonAdsClient
        from decision_sync import sync_ads, sync_retail, SellingPartnerClient
        from profit_actions import control_tick
        result = {'at': time.time(), 'success': True}
        # Independent feeds do not hide each other's failures.
        for name, fn in [('ads', lambda: sync_ads(AmazonAdsClient())),
                         ('retail', lambda: sync_retail(SellingPartnerClient())),
                         ('controls', lambda: control_tick(AmazonAdsClient(), True))]:
            try:
                result[name] = fn()
                if isinstance(result[name], dict) and result[name].get('success') is False:
                    result['success'] = False
            except Exception as exc:
                result[name] = {'error': type(exc).__name__, 'message': str(exc)}
                result['success'] = False
        GCSState('profit-sync-status').save(result)
        GCSState('profit-workflow').save({'last_result': {'success': result['success']}, 'last_run_at': time.time()})
        return JSONResponse(result, status_code=200 if result['success'] else 503)

    @app.post('/api/profit/experiments')
    def experiment(payload: dict):
        try:
            item = Experiment.model_validate(payload.get('experiment', {}))
            item.validate_eligibility()
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not live_requested(payload):
            return {'dry_run': True, 'experiment': item.model_dump(mode='json')}
        store = GCSState('profit-experiments')
        with store.locked() as state:
            key = str(payload.get('id') or uuid4())
            records = state.setdefault('experiments', {})
            if payload.get('id') and key not in records:
                raise HTTPException(404, 'Experiment not found')
            record = {**item.model_dump(mode='json'), 'id': key, 'updated_at': time.time()}
            GCSState('profit-experiment-history/' + str(uuid4())).save(record)
            records[key] = record
            store.save(state)
        return {'success': True, 'experiment': record,
                'message': 'Tracking saved. Start and manage the actual test in Amazon Manage Your Experiments.'}

    @app.post('/api/profit/alerts/acknowledge')
    def acknowledge(payload: dict):
        if not live_requested(payload):
            return {'dry_run': True}
        store = GCSState('profit-alerts')
        with store.locked() as state:
            item = state.get('alerts', {}).get(payload.get('key'))
            if item is None:
                raise HTTPException(404, 'Alert not found')
            item['acknowledged_at'] = time.time()
            store.save(state)
        return {'success': True}
