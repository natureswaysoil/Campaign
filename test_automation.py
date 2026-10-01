import copy
import json
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch
import pytest
from fastapi.responses import JSONResponse
from amazon_results import batch_outcome, create_keywords_verified
from ppc_waste_rules import classify_search_terms
from keyword_dayparting import retune_keywords
from server_with_bids import _target_bid_from_baseline, _acos_protected_bid
from optimize_campaigns import AmazonAdsClient
import keyword_dayparting
import scheduled_harvest
import scheduled_bids
import final_server
import extended_server
import server as launch_server


class MemoryState:
    def __init__(self):
        self.data = {}
        self.saves = 0
    def read(self):
        return copy.deepcopy(self.data)
    def save(self, state):
        self.data = copy.deepcopy(state)
        self.saves += 1
    @contextmanager
    def locked(self):
        yield self.read()


@pytest.mark.parametrize('term', ['liquid kelp fertilizer', 'dog urine neutralizer', 'humic acid for lawns'])
def test_profitable_core_phrases_are_harvested(term):
    result = classify_search_terms([{'searchTerm': term, 'clicks': 20, 'purchases7d': 4, 'cost': 10, 'sales7d': 100}])
    assert [w['term'] for w in result['winners']] == [term]


@pytest.mark.parametrize('term', ['dog urine neutralizer', 'humic acid for lawns', 'soil'])
def test_protected_losing_terms_not_negated(term):
    result = classify_search_terms([{'searchTerm': term, 'clicks': 30, 'purchases7d': 0, 'cost': 30, 'sales7d': 0}])
    assert not result['negatives'] and not result['winners']


def test_broad_roots_still_not_promoted():
    assert not classify_search_terms([{'searchTerm': 'soil', 'clicks': 20, 'purchases7d': 4, 'cost': 10, 'sales7d': 100}])['winners']


def test_pagination_and_top_level_filter():
    client = AmazonAdsClient.__new__(AmazonAdsClient)
    client.post = MagicMock(side_effect=[{'keywords': [{'keywordId': '1'}], 'nextToken': 'next'}, {'keywords': [{'keywordId': '2'}]}])
    assert len(client.list_keywords('c1')) == 2
    assert client.post.call_args.args[1]['nextToken'] == 'next'
    assert client.post.call_args.args[1]['campaignIdFilter'] == {'include': ['c1']}
    assert 'filters' not in client.post.call_args.args[1]


@pytest.mark.parametrize('response,accepted,failed', [({}, 0, 2),
    ({'keywords': {'success': [{'keywordId': '1'}], 'error': [{'index': 1}]}}, 1, 1),
    ({'keywords': {'success': [{'keywordId': '1'}, {'keywordId': '2'}]}}, 2, 0)])
def test_verified_results(response, accepted, failed):
    result = batch_outcome(response, 'keywords', 2)
    assert (result['accepted'], result['failed']) == (accepted, failed)


def test_keyword_create_batches():
    client = MagicMock()
    client.create_keywords.side_effect = lambda rows: {'keywords': {'success': [{'index': i} for i, _ in enumerate(rows)]}}
    result = create_keywords_verified(client, [{}] * 205)
    assert result['accepted'] == 205
    assert [len(c.args[0]) for c in client.create_keywords.call_args_list] == [100, 100, 5]


def test_dayparting_persists_baseline_without_compounding():
    state = MemoryState()
    current = {'bid': 1.0}
    client = MagicMock()
    client.list_keywords.side_effect = lambda _: [{'keywordId': 'k1', 'adGroupId': 'a1', 'state': 'ENABLED', 'bid': current['bid']}]
    def put(endpoint, payload):
        assert state.data['baselines']['k1'] == 1.0  # saved before Amazon write
        current['bid'] = payload['keywords'][0]['bid']
        return {'keywords': {'success': [{'keywordId': 'k1'}]}}
    client.put.side_effect = put
    with patch.object(keyword_dayparting, 'GCSState', return_value=state):
        values = []
        for mode in ['PROTECT', 'PROTECT', 'PRIME', 'TAPER']:
            result = retune_keywords(client, [{'adGroupId': 'a1', 'campaignId': 'c1'}],
                {'c1': {'spend': 10, 'sales': 100}}, mode, True, _target_bid_from_baseline, _acos_protected_bid)
            values.append(current['bid'])
    assert values == [.35, .35, 1.15, .45]


def test_preview_does_not_write_and_bad_keyword_does_not_raise():
    client = MagicMock()
    client.list_keywords.return_value = [{'keywordId': 'k1', 'adGroupId': 'a1', 'state': 'ENABLED', 'bid': .2}]
    result = retune_keywords(client, [{'adGroupId': 'a1', 'campaignId': 'c1'}],
        {'c1': {'spend': 80, 'sales': 20}}, 'PRIME', False, _target_bid_from_baseline, _acos_protected_bid)
    assert result['preview'][0]['newBid'] <= .2
    client.put.assert_not_called()


def test_report_survives_ticks_and_partial_failure():
    state = MemoryState()
    amazon = MagicMock()
    amazon.request_report.return_value = 'report1'
    amazon.get.side_effect = [{'status': 'PROCESSING'}, {'status': 'COMPLETED', 'url': 'https://example.test/report'}, {'status': 'COMPLETED', 'url': 'https://example.test/report'}]
    amazon.download_binary.return_value = b'[]'
    apply = MagicMock(side_effect=[JSONResponse({'success': False, 'keyword_errors': 1}, status_code=502), JSONResponse({'success': True, 'keywords_created': 1})])
    with patch.object(scheduled_harvest, 'GCSState', return_value=state), patch.object(scheduled_harvest, 'AmazonAdsClient', return_value=amazon), patch.object(scheduled_harvest, 'eastern_day', return_value=date(2026, 10, 1)):
        assert scheduled_harvest.harvest_tick({'apply_live': True}, apply).status_code == 202
        assert state.data['pending']['report_id'] == 'report1'
        assert scheduled_harvest.harvest_tick({'apply_live': True}, apply).status_code == 202
        apply.assert_not_called()
        assert scheduled_harvest.harvest_tick({'apply_live': True}, apply).status_code == 502
        assert 'pending' in state.data
        assert scheduled_harvest.harvest_tick({'apply_live': True}, apply).status_code == 200
        assert 'pending' not in state.data
        assert json.loads(scheduled_harvest.harvest_tick({'apply_live': True}, apply).body)['status'] == 'already_completed'
    amazon.request_report.assert_called_once()
    assert apply.call_count == 2


def test_harvest_verified_rejection_and_retry_deduplicates():
    amazon = MagicMock()
    amazon.list_keywords.return_value = []
    amazon.create_keywords.return_value = {'keywords': {'success': [], 'error': [{'index': 0}]}}
    rows = [{'campaignId': '1', 'adGroupId': '2', 'searchTerm': 'liquid kelp fertilizer', 'clicks': 20, 'purchases7d': 4, 'cost': 10, 'sales7d': 100}]
    with patch.object(final_server, 'load_products', return_value=[{'Product_ID': 'p1', 'Title': 'Example', 'SKU': 'sku'}]), patch.object(extended_server, '_find_existing_launch_campaigns', return_value={'AUTO_DISCOVERY': {'campaignId': '1'}, 'MANUAL_EXACT': {'campaignId': '3'}}), patch.object(extended_server, '_first_ad_group_id', return_value='4'):
        response = final_server.harvest_report_rows({'apply_live': True}, amazon, rows, 'r1', 'start', 'end')
        assert response.status_code == 502
        assert json.loads(response.body)['keywords_created'] == 0
        amazon.list_keywords.return_value = [{'keywordText': 'liquid kelp fertilizer', 'matchType': 'EXACT', 'state': 'ENABLED'}]
        response = final_server.harvest_report_rows({'apply_live': True}, amazon, rows, 'r1', 'start', 'end')
        assert response.status_code == 200
        assert amazon.create_keywords.call_count == 1


def test_scheduled_metrics_request_poll_apply():
    state = MemoryState()
    amazon = MagicMock()
    amazon.request_report.return_value = 'metrics1'
    amazon.get.side_effect = [{'status': 'PROCESSING'}, {'status': 'COMPLETED', 'url': 'https://example.test/report'}]
    amazon.download_binary.return_value = b'[{"campaignId":"c1","cost":10,"sales14d":100}]'
    retune = MagicMock(return_value=JSONResponse({'success': True}))
    with patch.object(scheduled_bids, 'GCSState', return_value=state), patch.object(scheduled_bids.core, 'AmazonAdsClient', return_value=amazon), patch.object(scheduled_bids.core, '_dash_summary_cache', {}):
        assert scheduled_bids.retune_tick({'apply_live': True}, retune).status_code == 202
        assert scheduled_bids.retune_tick({'apply_live': True}, retune).status_code == 202
        retune.assert_not_called()
        assert scheduled_bids.retune_tick({'apply_live': True}, retune).status_code == 200
        retune.assert_called_once()
        assert state.data['per_campaign']['c1']['sales'] == 100


@pytest.mark.parametrize('name', ['harvest', 'bids'])
def test_scheduler_preview_never_opens_storage(name):
    module = scheduled_harvest if name == 'harvest' else scheduled_bids
    function = module.harvest_tick if name == 'harvest' else module.retune_tick
    with patch.object(module, 'GCSState') as store:
        assert json.loads(function({'apply_live': True, 'dry_run': True}, MagicMock()).body)['dry_run']
        store.assert_not_called()


def test_failed_report_is_replaced_on_next_tick():
    state = MemoryState()
    state.data = {'pending': {'report_id': 'failed', 'day': '2026-10-01'}}
    amazon = MagicMock()
    amazon.get.return_value = {'status': 'FAILED'}
    amazon.request_report.return_value = 'replacement'
    with patch.object(scheduled_harvest, 'GCSState', return_value=state), patch.object(scheduled_harvest, 'AmazonAdsClient', return_value=amazon):
        assert scheduled_harvest.harvest_tick({'apply_live': True}, MagicMock()).status_code == 502
        assert 'pending' not in state.data
        assert scheduled_harvest.harvest_tick({'apply_live': True}, MagicMock()).status_code == 202
        assert state.data['pending']['report_id'] == 'replacement'


def test_disabled_and_inherited_keywords_not_updated():
    amazon = MagicMock()
    amazon.list_keywords.return_value = [
        {'keywordId': 'paused', 'adGroupId': 'a1', 'state': 'PAUSED', 'bid': 1.0},
        {'keywordId': 'inherited', 'adGroupId': 'a1', 'state': 'ENABLED'},
        {'keywordId': 'other_group', 'adGroupId': 'a2', 'state': 'ENABLED', 'bid': 1.0},
    ]
    state = MemoryState()
    with patch.object(keyword_dayparting, 'GCSState', return_value=state):
        result = retune_keywords(amazon, [{'adGroupId': 'a1', 'campaignId': 'c1'}], {},
            'PROTECT', True, _target_bid_from_baseline, _acos_protected_bid)
    assert result['updates_needed'] == 0
    amazon.put.assert_not_called()


def test_lock_rejects_overlap_and_releases_after_exception(monkeypatch):
    import sys
    import types
    from automation_store import GCSState, StateBusy
    class Conflict(Exception): pass
    class Missing(Exception): pass
    errors = types.ModuleType('google.api_core.exceptions')
    errors.PreconditionFailed, errors.NotFound = Conflict, Missing
    monkeypatch.setitem(sys.modules, 'google.api_core.exceptions', errors)
    store = GCSState.__new__(GCSState)
    store.lock = MagicMock()
    store.lock.generation = 7
    store.read = MagicMock(return_value={})
    with pytest.raises(RuntimeError):
        with store.locked():
            raise RuntimeError('simulated Amazon error')
    store.lock.delete.assert_called_once_with(if_generation_match=7)
    assert store.lock.upload_from_string.call_args.kwargs['if_generation_match'] == 0
    store.lock.reset_mock()
    store.lock.upload_from_string.side_effect = Conflict()
    with pytest.raises(StateBusy):
        with store.locked():
            pytest.fail('Concurrent writer acquired lock')
    store.lock.delete.assert_not_called()



def test_launch_campaign_is_created_paused():
    amazon = MagicMock()
    amazon.post.return_value = {
        'campaigns': {'success': [{'campaign': {'campaignId': 'c1'}}]}
    }
    campaign_id = launch_server._create_campaign(
        amazon, 'Example | AUTO DISCOVERY | 2026-10-01', 'AUTO', 10.0, '2026-10-01'
    )
    assert campaign_id == 'c1'
    payload = amazon.post.call_args.args[1]
    assert payload['campaigns'][0]['state'] == 'PAUSED'


def test_product_ad_rejection_fails_launch_closed():
    amazon = MagicMock()
    amazon.post.return_value = {
        'productAds': {'success': [], 'error': [{'index': 0, 'code': 'REJECTED'}]}
    }
    with pytest.raises(RuntimeError, match='Product ad creation was not acknowledged'):
        launch_server._create_product_ad(amazon, 'c1', 'a1', 'sku1', 'asin1')


def test_seed_negative_rejection_fails_launch_closed():
    amazon = MagicMock()
    amazon.create_negative_keywords.return_value = {
        'campaignNegativeKeywords': {
            'success': [],
            'error': [{'index': 0, 'code': 'REJECTED'}],
        }
    }
    with pytest.raises(RuntimeError, match='Seed negative creation was not fully acknowledged'):
        launch_server._apply_launch_seed_negatives(amazon, ['c1'])


def test_launch_bid_never_exceeds_protected_ceiling():
    discovery_bid, exact_bid = launch_server._protected_launch_bids(2.50)
    assert discovery_bid <= 2.50
    assert exact_bid == 2.50


def test_partial_launch_is_resumed_instead_of_blocked():
    amazon = MagicMock()
    existing = {
        'AUTO_DISCOVERY': {
            'campaignId': 'auto-1',
            'name': 'Example | AUTO DISCOVERY | 2026-10-01',
            'state': 'ENABLED',
        }
    }
    captured = {}

    def fake_launch(payload, authorization, token):
        captured.update(payload)
        return JSONResponse({'success': True, 'resumed_partial_launch': True})

    with (
        patch.object(extended_server, '_optional_dashboard_auth', return_value=None),
        patch.object(extended_server, '_product_from_key',
                     return_value=({'title': 'Example', 'sku': 'sku1', 'asin': 'asin1'}, {})),
        patch.object(extended_server, 'AmazonAdsClient', return_value=amazon),
        patch.object(extended_server, '_find_existing_launch_campaigns', return_value=existing),
        patch.object(extended_server.base, 'api_create_recommended_campaigns',
                     side_effect=fake_launch),
    ):
        response = extended_server.api_create_campaign_with_duplicate_protection(
            {'product_id': 'p1', 'apply_live': True}, 'Bearer test', 'test'
        )

    assert response.status_code == 200
    assert captured['_resume_campaigns'] == {'AUTO_DISCOVERY': 'auto-1'}


def test_paused_pair_is_resumed_not_treated_as_complete_duplicate():
    amazon = MagicMock()
    existing = {
        'AUTO_DISCOVERY': {'campaignId': 'auto-1', 'state': 'PAUSED'},
        'MANUAL_EXACT': {'campaignId': 'exact-1', 'state': 'PAUSED'},
    }
    captured = {}

    def fake_launch(payload, authorization, token):
        captured.update(payload)
        return JSONResponse({'success': True})

    with (
        patch.object(extended_server, '_optional_dashboard_auth', return_value=None),
        patch.object(extended_server, '_product_from_key',
                     return_value=({'title': 'Example', 'sku': 'sku1', 'asin': 'asin1'}, {})),
        patch.object(extended_server, 'AmazonAdsClient', return_value=amazon),
        patch.object(extended_server, '_find_existing_launch_campaigns', return_value=existing),
        patch.object(extended_server.base, 'api_create_recommended_campaigns',
                     side_effect=fake_launch),
    ):
        response = extended_server.api_create_campaign_with_duplicate_protection(
            {'product_id': 'p1', 'apply_live': True}, 'Bearer test', 'test'
        )

    assert response.status_code == 200
    assert captured['_resume_campaigns'] == {
        'AUTO_DISCOVERY': 'auto-1',
        'MANUAL_EXACT': 'exact-1',
    }


def test_extended_dashboard_uses_selected_launch_settings():
    patch_js = extended_server.DASHBOARD_PATCH_JS
    assert "byId('lDiscoveryPct')" in patch_js
    assert "byId('lMaxExact')" in patch_js
    assert 'discovery_budget_pct: 0.30' not in patch_js
    assert 'max_exact_keywords: 40' not in patch_js



def test_recovery_recreates_missing_product_ad():
    amazon = MagicMock()
    amazon.list_product_ads.return_value = []
    amazon.post.return_value = {
        'productAds': {'success': [{'productAd': {'adId': 'pa1'}}]}
    }
    created = launch_server._ensure_product_ad(
        amazon, 'c1', 'a1', 'sku1', 'asin1'
    )
    assert created is True
    amazon.post.assert_called_once()


def test_recovery_keeps_existing_product_ad_without_duplicate_write():
    amazon = MagicMock()
    amazon.list_product_ads.return_value = [{
        'adGroupId': 'a1',
        'sku': 'sku1',
        'asin': 'asin1',
        'state': 'ENABLED',
    }]
    created = launch_server._ensure_product_ad(
        amazon, 'c1', 'a1', 'sku1', 'asin1'
    )
    assert created is False
    amazon.post.assert_not_called()


def test_seed_negative_retry_skips_terms_already_present():
    amazon = MagicMock()
    seeded = launch_server._seed_negative_rows('c1')
    assert seeded
    amazon.list_campaign_negative_keywords.return_value = [
        {'keywordText': row['keywordText'], 'state': 'ENABLED'} for row in seeded
    ]
    result = launch_server._apply_launch_seed_negatives(amazon, ['c1'])
    assert result['negative_rows_created'] == 0
    amazon.create_negative_keywords.assert_not_called()


def test_product_ad_and_negative_list_helpers_use_campaign_filters():
    amazon = AmazonAdsClient.__new__(AmazonAdsClient)
    amazon.post = MagicMock(side_effect=[
        {'productAds': []},
        {'campaignNegativeKeywords': []},
    ])
    assert amazon.list_product_ads('c1') == []
    assert amazon.post.call_args_list[0].args[1]['campaignIdFilter'] == {'include': ['c1']}
    assert amazon.list_campaign_negative_keywords('c2') == []
    assert amazon.post.call_args_list[1].args[1]['campaignIdFilter'] == {'include': ['c2']}
