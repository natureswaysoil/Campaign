"""Regression tests for real spending boundaries, delayed reports and safe recovery."""
import copy
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import profit_intelligence as intelligence
import profit_guard as guards
import profit_api
import profit_actions
import decision_sync
from profit_intelligence import Policy, Product, Metric, ET


@pytest.fixture
def now():
    return time.time()


@pytest.fixture
def product(now):
    return {'asin': 'B012345678', 'sku': 'soil', 'price': 40., 'cogs': 15., 'fees': 6., 'shipping': 10.,
            'other_cost': 0, 'fee_source': 'manual', 'updated_at': now, 'inventory': 100,
            'inventory_at': now, 'daily_units': 2., 'velocity_at': now, 'lead_days': 7}


def metric(now, **overrides):
    return {'grain': 'campaign', 'date': (datetime.fromtimestamp(now, ET).date() - timedelta(days=20)).isoformat(),
            'campaign_id': 'c', 'clicks': 100, 'orders': 10, 'sales': 400., 'spend': 20.,
            'impressions': 1000, 'observed_at': now, **overrides}


@pytest.fixture
def data(product, now):
    return {'products': {product['asin']: product}, 'metrics': [metric(now)], 'complete_spend_at': now}


@pytest.fixture
def client():
    client = MagicMock()
    client.profit_product_asin = None
    campaigns = [{'campaignId': 'c', 'state': 'ENABLED', 'budget': {'budget': 30., 'budgetType': 'DAILY'},
                  'dynamicBidding': {'strategy': 'MANUAL'}}]
    groups = [{'campaignId': 'c', 'adGroupId': 'g', 'state': 'ENABLED', 'defaultBid': .2}]
    keywords = [{'campaignId': 'c', 'adGroupId': 'g', 'keywordId': 'k', 'state': 'ENABLED', 'bid': .2}]
    client.list_campaigns.return_value = campaigns
    client.list_product_ads.return_value = [{'campaignId': 'c', 'adGroupId': 'g', 'adId': 'a', 'asin': 'B012345678', 'sku': 'soil', 'state': 'ENABLED'}]
    def listing(path, key, filters=None):
        return {'/sp/adGroups/list': groups, '/sp/keywords/list': keywords, '/sp/targets/list': [],
                '/sp/productAds/list': client.list_product_ads.return_value}[path]
    client.list_all.side_effect = listing
    client.list_keywords.return_value = keywords
    client.list_targets.return_value = []
    return client


def guard(client, data, now, **policy):
    return guards.Guard(client, data, Policy(daily_limit=100, **policy), {}, now)


def test_margin_and_cross_asin_aov_cap(product, now):
    policy = Policy(daily_limit=100)
    result = intelligence.cpc_ceiling(product, [metric(now, sales=4000)], policy, now)
    assert result['margin'] == .225
    assert result['target_acos'] == .125
    assert result['aov'] == 40  # Other purchased ASINs cannot inflate product AOV.
    assert result['max_cpc'] == .45


@pytest.mark.parametrize('field', ['price', 'cogs', 'fees', 'shipping'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_nonfinite_and_negative_costs_rejected(product, field, value):
    with pytest.raises(ValueError):
        Product.model_validate({**product, field: value})


def test_recent_clicks_do_not_become_losses(data, now):
    data['metrics'].append(metric(now, date=datetime.fromtimestamp(now, ET).date().isoformat(), clicks=1000, orders=0))
    assert len(intelligence.mature_rows(data, Policy(daily_limit=100), now, grain='campaign')) == 1


def test_preliminary_old_report_is_not_mature(data, now):
    data['metrics'][0]['observed_at'] = now - 18 * 86400
    assert intelligence.mature_rows(data, Policy(daily_limit=100), now, grain='campaign') == []


def test_inventory_zero_stops_even_without_conversions(product, now):
    p = {**product, 'inventory': 0}
    assert intelligence.cpc_ceiling(p, [], Policy(daily_limit=100), now)['max_cpc'] == 0


def test_restock_eta_does_not_invent_stock(product, now):
    p = {**product, 'inventory': 10, 'restock_date': (datetime.now(ET).date() - timedelta(days=1)).isoformat()}
    factor, cover = intelligence.inventory_factor(p, Policy(daily_limit=100), now)
    assert cover == 5
    assert factor < 1


def test_stale_inventory_blocks_increases_but_allows_reduction(client, data, now):
    data['products']['B012345678']['inventory_at'] = now - 90000
    g = guard(client, data, now)
    with pytest.raises(guards.GuardBlocked, match='inventory'):
        g.prepare('PUT', '/sp/adGroups', {'adGroups': [{'adGroupId': 'g', 'defaultBid': .5}]})
    assert g.prepare('PUT', '/sp/adGroups', {'adGroups': [{'adGroupId': 'g', 'defaultBid': .1}]})['adGroups'][0]['defaultBid'] == .1


def test_individual_keyword_performance_controls_ceiling(client, data, now):
    data['metrics'].append(metric(now, grain='keyword', keyword_id='k', orders=1, sales=40))
    g = guard(client, data, now)
    result = g.prepare('PUT', '/sp/keywords', {'keywords': [{'keywordId': 'k', 'bid': 2.5}]})
    assert result['keywords'][0]['bid'] == .08


def test_margin_ceiling_accounts_for_placement_and_dynamic_uplift(client, data, now):
    client.list_campaigns.return_value[0]['dynamicBidding'] = {'strategy': 'AUTO_FOR_SALES', 'placementBidding': [{'placement': 'PLACEMENT_TOP', 'percentage': 100}]}
    result = guard(client, data, now).prepare('PUT', '/sp/adGroups', {'adGroups': [{'adGroupId': 'g', 'defaultBid': 2.5}]})
    assert result['adGroups'][0]['defaultBid'] * 4 <= .45


def test_zero_margin_pauses_instead_of_bid_floor(client, data, now):
    data['products']['B012345678']['shipping'] = 19
    result = guard(client, data, now).prepare('PUT', '/sp/adGroups', {'adGroups': [{'adGroupId': 'g', 'defaultBid': 1}]})
    assert result['adGroups'][0] == {'adGroupId': 'g', 'state': 'PAUSED'}


def test_identity_spoof_does_not_escape_guard(client, data, now):
    with pytest.raises(guards.GuardBlocked, match='mismatch'):
        guard(client, data, now).prepare('PUT', '/sp/keywords', {'keywords': [{'keywordId': 'k', 'campaignId': 'other', 'bid': .01}]})


def test_enabled_campaign_create_rejected(client, data, now):
    with pytest.raises(guards.GuardBlocked, match='PAUSED'):
        guard(client, data, now).prepare('POST', '/sp/campaigns', {'campaigns': [{'state': 'ENABLED', 'budget': {'budget': 5}}]})


def test_account_budget_reservations_count_other_campaigns(client, data, now):
    g = guard(client, data, now)
    g.ledger['budget_days'] = {datetime.fromtimestamp(now, ET).date().isoformat(): {'other': 80}}
    with pytest.raises(guards.GuardBlocked, match='Aggregate'):
        g.account_room('c', 30)


def test_stale_complete_spend_blocks_growth(client, data, now):
    data['complete_spend_at'] = now - 7200
    with pytest.raises(guards.GuardBlocked, match='Fresh complete'):
        guard(client, data, now).account_room('c', 31)


def test_buffered_spend_pauses_campaigns(client, data, now):
    data['metrics'].append(metric(now, date=datetime.fromtimestamp(now, ET).date().isoformat(), spend=86))
    result = profit_actions.build_plan(client, data, Policy(daily_limit=100), now)
    assert result['actions'][0]['row'] == {'campaignId': 'c', 'state': 'PAUSED'}


def test_missing_hourly_data_never_invents_a_peak(now):
    assert intelligence.learned_multiplier([], Policy(daily_limit=100), now) == 1


def test_learned_weekday_and_hour_bounded(now):
    rows = [metric(now, grain='hourly', hour=datetime.fromtimestamp(now, ET).hour,
                   date=(datetime.fromtimestamp(now, ET).date() - timedelta(days=21 + i * 7)).isoformat(),
                   clicks=100, sales=400, orders=10) for i in range(4)]
    rows.append(metric(now, grain='hourly', hour=(datetime.fromtimestamp(now, ET).hour+1)%24, clicks=1000, orders=10, sales=400))
    assert intelligence.learned_multiplier(rows, Policy(daily_limit=100), now) == 1.25


class MemoryStore:
    values = {}
    events = []
    def __init__(self, name): self.name = name
    def read(self): return copy.deepcopy(self.values.get(self.name, {}))
    def save(self, data):
        self.events.append(self.name)
        self.values[self.name] = copy.deepcopy(data)
    @contextmanager
    def locked(self): yield self.read()


@pytest.fixture
def storage(monkeypatch):
    MemoryStore.values = {}
    MemoryStore.events = []
    for mod in (intelligence, guards, profit_actions, profit_api, decision_sync):
        monkeypatch.setattr(mod, 'GCSState', MemoryStore)
    return MemoryStore


def test_replayed_import_replaces_not_adds(storage, product, now):
    intelligence.ingest(products=[product], metrics=[metric(now)], source='test')
    intelligence.ingest(products=[product], metrics=[metric(now)], source='test')
    rows = storage.values['profit-data']['metrics']
    assert len(rows) == 1 and rows[0]['sales'] == 400
    assert len([k for k in storage.values if k.startswith('profit-history/')]) == 1


def test_report_restatement_removes_disappeared_rows(storage, now):
    m = metric(now)
    intelligence.ingest(metrics=[m])
    intelligence.ingest(metrics=[], replace_window=('campaign', m['date'], m['date'], now+1))
    assert storage.values['profit-data']['metrics'] == []


def test_reservation_written_before_ambiguous_amazon_failure(storage, client, data, now):
    storage.values['profit-data'] = data
    storage.values['profit-policy'] = {'policy': Policy(daily_limit=100).model_dump()}
    def send(body):
        assert storage.values['profit-write-guard']['budget_days'][datetime.fromtimestamp(now, ET).date().isoformat()]['c'] == 30
        raise TimeoutError('unknown acceptance')
    with pytest.raises(TimeoutError):
        guards.guarded_write(client, 'PUT', '/sp/adGroups', {'adGroups': [{'adGroupId': 'g', 'defaultBid': .4}]}, send)
    assert list(v for k,v in storage.values.items() if k.startswith('profit-audit/'))[0]['status'] == 'unconfirmed'


def test_emergency_pause_does_not_require_storage(monkeypatch):
    monkeypatch.setattr(guards, 'GCSState', lambda n: pytest.fail('Must not open storage for emergency pause'))
    send = MagicMock(return_value={'campaigns': {'success': [{'campaignId': 'c'}]}})
    guards.guarded_write(MagicMock(), 'PUT', '/sp/campaigns', {'campaigns': [{'campaignId': 'c', 'state': 'PAUSED'}]}, send)
    send.assert_called_once()


def test_alerts_deduplicate_resolve_and_reopen(storage):
    a = {'key': 'stock:x', 'message': 'Low stock'}
    profit_actions.update_alerts([a], now=10)
    profit_actions.update_alerts([a], now=20)
    assert len(storage.values['profit-alerts']['alerts']) == 1
    profit_actions.update_alerts([], now=30)
    assert storage.values['profit-alerts']['alerts']['stock:x']['resolved_at'] == 30
    profit_actions.update_alerts([a], now=40)
    assert storage.values['profit-alerts']['alerts']['stock:x']['first_seen'] == 10
    assert storage.values['profit-alerts']['alerts']['stock:x']['resolved_at'] is None


def test_spapi_fbm_listing_does_not_use_fba_units(storage, product):
    storage.values['profit-data'] = {'products': {product['asin']: product}}
    client = MagicMock(marketplace='ATVPDKIKX0DER')
    client.listing.return_value = {'summaries': [{'asin': product['asin']}],
        'offers': [{'marketplaceId': 'ATVPDKIKX0DER', 'offerType': 'B2C', 'price': {'currencyCode': 'USD', 'amount': 40}}],
        'fulfillmentAvailability': [{'fulfillmentChannelCode': 'DEFAULT', 'quantity': 24}]}
    client.fees.return_value = 6
    client.request_sales.return_value = 'sales-report'
    result = decision_sync.sync_retail(client)
    assert result['sales'] == 'requested'
    client.fba_units.assert_not_called()
    assert storage.values['profit-data']['products'][product['asin']]['inventory'] == 24


def test_api_auth_preview_and_experiment_eligibility(storage, monkeypatch):
    import final_server
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'profit-test')
    api = TestClient(final_server.app)
    assert api.get('/api/profit/status').status_code == 403
    headers = {'X-Daily-Optimizer-Token': 'profit-test'}
    response = api.post('/api/profit/policy', headers=headers, json={'policy': {'daily_limit': 100}, 'dry_run': True})
    assert response.status_code == 200 and storage.values == {}
    bad = {'asin': 'B012345678', 'kind': 'title', 'hypothesis': 'A clearer title', 'status': 'running'}
    assert api.post('/api/profit/experiments', headers=headers, json={'experiment': bad, 'apply_live': True}).status_code == 422
    bad['status'] = 'planned'
    assert api.post('/api/profit/experiments', headers=headers, json={'experiment': bad, 'apply_live': True}).status_code == 200


def test_actual_client_mutation_cannot_bypass_guard(monkeypatch):
    from optimize_campaigns import AmazonAdsClient
    client = AmazonAdsClient.__new__(AmazonAdsClient)
    raw = MagicMock()
    client._request_raw = raw
    monkeypatch.setattr(guards, 'guarded_write', MagicMock(side_effect=guards.GuardBlocked('blocked')))
    with pytest.raises(guards.GuardBlocked):
        client.put('/sp/keywords', {'keywords': [{'keywordId': 'k', 'bid': 50}]})
    raw.assert_not_called()


def test_unknown_placement_fails_instead_of_silent_misattribution(now):
    with pytest.raises(ValueError, match='placement'):
        decision_sync.normalize_ads([{'date': '2026-09-01', 'campaignId': 'c', 'placementClassification': 'mystery'}], 'placement', now)


def test_yesterdays_snapshot_cannot_authorize_new_day_spending(now):
    just_after_midnight = datetime.now(ET).replace(hour=0, minute=5, second=0, microsecond=0).timestamp()
    d = {'metrics': [], 'complete_spend_at': just_after_midnight - 600}
    assert not intelligence.pacing(d, Policy(daily_limit=100), just_after_midnight)['fresh']


def test_paused_campaign_spend_still_counts(client, data, now):
    data['metrics'].append(metric(now, campaign_id='paused', date=datetime.fromtimestamp(now, ET).date().isoformat(), spend=75))
    with pytest.raises(guards.GuardBlocked, match='Aggregate'):
        guard(client, data, now).account_room('c', 30)


def test_simultaneous_workflow_uses_same_account_lock(storage, client, data, monkeypatch):
    @contextmanager
    def busy(self):
        raise guards.GuardBlocked('lock busy')
        yield
    monkeypatch.setattr(MemoryStore, 'locked', busy)
    send = MagicMock()
    with pytest.raises(guards.GuardBlocked, match='lock busy'):
        guards.guarded_write(client, 'PUT', '/sp/adGroups', {'adGroups': [{'adGroupId': 'g', 'defaultBid': .4}]}, send)
    send.assert_not_called()


def test_old_velocity_import_does_not_replace_new_inventory(storage, product, now):
    intelligence.ingest(products=[{**product, 'inventory': 50, 'inventory_at': now}])
    intelligence.ingest(products=[{**product, 'inventory': 100, 'inventory_at': now-60}])
    assert storage.values['profit-data']['products'][product['asin']]['inventory'] == 50


def test_import_cannot_claim_complete_spend_snapshot():
    with pytest.raises(ValueError):
        profit_api.DataImport.model_validate({'complete_spend_at': time.time(), 'apply_live': True})


def test_budget_growth_cannot_use_profitable_but_immature_sales(client, data, now):
    data['metrics'] = [metric(now, date=datetime.fromtimestamp(now, ET).date().isoformat())]
    g = guard(client, data, now)
    with pytest.raises(guards.GuardBlocked, match='mature profitable'):
        g.verify_budget_growth('c', 40)


def test_exploration_budget_has_separate_small_cap(client, data, now):
    data['metrics'] = []
    g = guard(client, data, now, exploration_cvr=.02)
    g.verify_budget_growth('c', 5)
    with pytest.raises(guards.GuardBlocked, match='bounded exploration'):
        g.verify_budget_growth('c', 25)


def test_search_term_reports_exclude_attribution_window():
    from optimize_campaigns import _build_search_term_report_body
    report = _build_search_term_report_body(14)
    assert intelligence.report_is_mature(report['endDate'])
    assert (datetime.fromisoformat(report['endDate']) - datetime.fromisoformat(report['startDate'])).days == 13


def test_monitor_detects_missing_profit_workflow():
    from scripts.monitor_automation import summarize
    result = summarize({'profile': {}})
    assert 'profit has no recorded apply result' in result['alerts']


def test_current_spend_cannot_refresh_stale_mature_evidence(now):
    stale = metric(now, observed_at=now - 48 * 3600)
    current = metric(now, date=datetime.fromtimestamp(now, ET).date().isoformat())
    assert intelligence.mature_rows({'metrics': [stale, current]}, Policy(daily_limit=100), now, grain='campaign') == []


def test_legacy_optimizer_cannot_send_live_bids():
    from campaign_optimizer import AmazonAdsClient
    client = AmazonAdsClient.__new__(AmazonAdsClient)
    client.session = MagicMock()
    with pytest.raises(RuntimeError, match='Legacy live writes'):
        client.put('/sp/keywords', {})
    client.session.put.assert_not_called()


def test_manual_import_cannot_supply_hourly_learning(storage, monkeypatch, now):
    import final_server
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'profit-test')
    response = TestClient(final_server.app).post('/api/profit/data',
        headers={'X-Daily-Optimizer-Token': 'profit-test'}, json={'apply_live': True,
        'metrics': [metric(now, grain='hourly', hour=12, utc_offset='-04:00')]})
    assert response.status_code == 422
    assert not storage.values


def test_stuck_ads_report_is_replaced(storage, now):
    client = MagicMock(profile_id='123')
    client.get.side_effect = [[{'profileId': '123', 'currencyCode': 'USD', 'timezone': ET.key}], {'status': 'PENDING'}]
    storage.values['profit-ads-sync'] = {'current': {'report_id': 'stuck', 'requested_at': now - 7300}}
    with pytest.raises(RuntimeError, match='replacement scheduled'):
        decision_sync.sync_ads(client)
    assert 'report_id' not in storage.values['profit-ads-sync']['current']


def test_activation_reuses_secrets_and_does_not_invent_policy(monkeypatch, capsys):
    import json
    from io import BytesIO
    from scripts import activate_profit_controls as activate
    calls = []
    def cloud(*args):
        calls.append(args)
        if args[:3] == ('run', 'services', 'describe'):
            return json.dumps({'status': {'url': 'https://example.test'}, 'spec': {'template': {'spec': {'containers': [{'env': []}]}}}})
        if args[:2] == ('secrets', 'list'):
            return '[]'
        if args[:3] == ('secrets', 'versions', 'access'):
            if '--secret=DAILY_OPTIMIZER_TOKEN' in args:
                return 'private-test-token'
            raise RuntimeError('NOT_FOUND')
        if args[:3] == ('scheduler', 'jobs', 'list'):
            return '[]'
        if args[:3] == ('scheduler', 'jobs', 'describe'):
            return '{"state":"ENABLED"}'
        return ''
    monkeypatch.setattr(activate, 'gcloud', cloud)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setattr(activate.urllib.request, 'urlopen', lambda *a, **k: BytesIO(b'{"products":[],"policy":{"daily_limit":75,"timezone":"America/New_York"}}'))
    activate.main()
    output = capsys.readouterr().out
    assert 'Saved spending policy: present' in output
    assert 'private-test-token' not in output
    assert any(c[:3] == ('scheduler', 'jobs', 'update') for c in calls)
    assert not any(c[:3] == ('run', 'services', 'update') for c in calls)


def test_owner_approved_limit_saved_and_read_back(monkeypatch):
    from io import BytesIO
    import json
    from scripts import activate_profit_controls as activate
    calls = []
    def request(req, **kwargs):
        calls.append(req)
        if req.get_method() == 'POST':
            assert json.loads(req.data) == {'apply_live': True, 'policy': {'daily_limit': 75.0}}
            return BytesIO(b'{"success":true,"policy":{"daily_limit":75}}')
        return BytesIO(b'{"policy":{"daily_limit":75},"products":[]}')
    monkeypatch.setattr(activate.urllib.request, 'urlopen', request)
    assert activate.initialize_spending_policy('https://example.test', 'private-token', {})['policy']['daily_limit'] == 75
    assert len(calls) == 2
    assert activate.initialize_spending_policy('https://example.test', 'private-token', {'policy': {'daily_limit': 60}})['policy']['daily_limit'] == 60
    assert len(calls) == 2


def test_named_spapi_credentials_bound_even_when_seller_metadata_missing(monkeypatch):
    import json
    from scripts import activate_profit_controls as activate
    config = {'spec': {'template': {'spec': {'containers': [{'env': []}]}}}}
    calls = []
    credential_names = list(activate.SP_API_SECRET_NAMES)[:3]
    def cloud(*args):
        calls.append(args)
        if args[:3] == ('secrets', 'versions', 'access'):
            if any('--secret=' + activate.SP_API_SECRET_NAMES[n] in args for n in credential_names):
                return 'secret-value-never-logged'
            raise RuntimeError('NOT_FOUND')
        if args[:3] == ('run', 'services', 'describe'):
            config['spec']['template']['spec']['containers'][0]['env'] = [
                {'name': n, 'valueFrom': {'secretKeyRef': {'name': activate.SP_API_SECRET_NAMES[n], 'key': 'latest'}}}
                for n in credential_names]
            return json.dumps(config)
        return ''
    monkeypatch.setattr(activate, 'gcloud', cloud)
    activate.configure_sp_api_secrets(config, 'campaign-optimizer', [], 'test-project')
    updates = [c for c in calls if c[:3] == ('run', 'services', 'update')]
    assert len(updates) == 1
    mapping = updates[0][updates[0].index('--update-secrets') + 1]
    assert mapping == 'SP_API_CLIENT_ID=PPCADSCLIENT:latest,SP_API_CLIENT_SECRET=PPCADSSECRET:latest,SP_API_REFRESH_TOKEN=PPCADSREFRESHTOKEN:latest'
    assert 'AMAZON_ADS' not in mapping


def test_policy_timezone_alignment_preserves_owner_limits(monkeypatch):
    import json
    from io import BytesIO
    from scripts import activate_profit_controls as activate
    old = {'daily_limit': 75, 'timezone': 'America/New_York', 'max_cpc': 1.25}
    expected = {**old, 'timezone': 'America/Los_Angeles'}
    calls = []
    def request(req, **kwargs):
        calls.append(req)
        if req.get_method() == 'POST':
            assert json.loads(req.data) == {'apply_live': True, 'policy': expected}
        return BytesIO(json.dumps({'policy': expected}).encode())
    monkeypatch.setattr(activate.urllib.request, 'urlopen', request)
    result = activate.align_policy_timezone('https://example.test', 'private-token', {'policy': old}, 'America/Los_Angeles')
    assert result['policy'] == expected
    assert len(calls) == 2
    activate.align_policy_timezone('https://example.test', 'private-token', result, 'America/Los_Angeles')
    assert len(calls) == 2


def test_profit_diagnostics_never_echo_raw_errors():
    from scripts.monitor_automation import sync_summary
    result = sync_summary({'retail': {'error': 'HTTPError', 'message': '403 Client Error for url: https://sellingpartnerapi-na.amazon.com/private-value'}, 'ads': {'error': 'RuntimeError', 'message': 'Set AMAZON_ADS_TIMEZONE; observed: America/Los_Angeles'}})
    assert result['retail'] == {'error': 'HTTPError', 'http_status': 403, 'category': 'selling_partner_api'}
    assert result['ads']['observed_timezone'] == 'America/Los_Angeles'
    assert 'private-value' not in str(result)


def test_verified_cost_schedule_rejects_stale_and_invalid_values(now):
    from verified_costs import validate_schedule
    data = {'verified_at': now - 10, 'sizes': {s: [1, 2] for s in ('32oz', '1gal', '2.5gal')}}
    assert validate_schedule(data, now) == data
    with pytest.raises(ValueError):
        validate_schedule({**data, 'verified_at': now - 31 * 86400}, now)
    with pytest.raises(ValueError):
        validate_schedule({**data, 'verified_at': now + 10}, now)
    data['sizes']['32oz'] = [float('nan'), 1]
    with pytest.raises(ValueError):
        validate_schedule(data, now)


def test_owner_fbm_costs_require_matching_listing_and_real_fees(product):
    from decision_sync import refresh_product_market
    client = MagicMock()
    client.marketplace = 'market'
    listing = {'summaries': [{'asin': product['asin']}],
        'offers': [{'marketplaceId': 'market', 'offerType': 'B2C', 'price': {'currencyCode': 'USD', 'amount': 40}}],
        'fulfillmentAvailability': [{'fulfillmentChannelCode': 'DEFAULT', 'quantity': 20}]}
    client.listing.return_value = listing
    client.fees.return_value = 6
    result = refresh_product_market(client, product, fbm_only=True)
    assert result['updated_at'] == product['updated_at']
    assert result['fees'] == 6 and result['inventory'] == 20
    client.fees.side_effect = RuntimeError('fees unavailable')
    with pytest.raises(RuntimeError):
        refresh_product_market(client, product, fbm_only=True)
    listing['fulfillmentAvailability'] = []
    with pytest.raises(RuntimeError, match='only to FBM'):
        refresh_product_market(client, product, fbm_only=True)


def test_private_cost_packet_round_trip_and_no_automatic_reverification(monkeypatch, now):
    import base64
    import json
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    import verified_costs
    import automation_store
    state = {}
    class Store:
        def __init__(self, name):
            self.name = name
        def read(self):
            return copy.deepcopy(state.get(self.name, {}))
        def save(self, value):
            state[self.name] = copy.deepcopy(value)
        @contextmanager
        def locked(self):
            yield self.read()
    monkeypatch.setattr(automation_store, 'GCSState', Store)
    public = verified_costs.import_public_key()
    assert 'PRIVATE KEY' not in public
    assert public == verified_costs.import_public_key()
    key = serialization.load_pem_public_key(public.encode())
    schedule = {'verified_at': now - 10, 'sizes': {s: [1, 2] for s in ('32oz', '1gal', '2.5gal')}}
    def encrypt(value):
        return base64.b64encode(key.encrypt(json.dumps(value).encode(), padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()
    monkeypatch.setattr(decision_sync, 'SellingPartnerClient', lambda: object())
    monkeypatch.setattr(verified_costs, 'seed_products', lambda client, value: value)
    assert verified_costs.import_encrypted_costs(encrypt(schedule)) == schedule
    expired = {**schedule, 'verified_at': now - 31 * 86400}
    assert verified_costs.import_encrypted_costs(encrypt(expired))['saved'] == 0


def test_off_amazon_report_rows_preserved_separately(now):
    from decision_sync import normalize_ads
    raw = [{'date': '2026-10-14', 'campaignId': 'c', 'cost': 3,
            'placementClassification': 'Off Amazon'},
           {'date': '2026-10-14', 'campaignId': 'c', 'cost': 4,
            'placementClassification': 'Other on-Amazon'}]
    rows = normalize_ads(raw, 'placement', now)
    assert Metric.model_validate(rows[0]).placement == 'PLACEMENT_OFF_AMAZON'
    assert Metric.model_validate(rows[1]).placement == 'PLACEMENT_REST_OF_SEARCH'
    assert intelligence.metric_key(rows[0]) != intelligence.metric_key(rows[1])
    assert sum(r['spend'] for r in rows) == 7


def test_amazon_error_diagnostics_do_not_leak_request_or_message():
    import requests
    from decision_sync import safe_api_error
    response = requests.Response()
    response.status_code = 400
    response.url = 'https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items/private-id/private-sku'
    response._content = b'{"errors":[{"code":"InvalidInput","message":"Invalid seller private-id and sku private-sku"}]}'
    result = safe_api_error(requests.HTTPError(response=response))
    assert result == {'operation': 'listing', 'amazon_code': 'InvalidInput', 'fields': ['seller_id', 'sku']}
    assert 'private' not in str(result)
