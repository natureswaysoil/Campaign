"""Reproduce the production review defects using no live credentials or writes."""
import json
import time
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
import final_server as final
import extended_server as extended
import optimize_campaigns as core
import opportunity_monitor as opportunity
import sales_growth_engine as growth
import server_with_bids as bids
from dashboard_bids import enrich_campaign_bids
from ppc_waste_rules import apply_negatives_step_with_match_types
from scripts.monitor_automation import summarize


@pytest.mark.parametrize('raw', [{}, {'Price': '40'}, {'Gross_Margin': 'NaN'}, {'Gross_Margin': '-20'}, {'Gross_Margin': 'Infinity'}])
def test_missing_or_invalid_costs_cannot_scale_or_auto_launch(raw):
    economics = growth.product_economics(raw)
    metrics = {'sales': 100, 'spend': 20, 'orders': 4, 'clicks': 20}
    assert growth.classify_product({}, metrics, economics)['decision'] != 'SCALE'
    assert opportunity.classify_opportunity({**metrics, 'acos': .2, 'conversion_rate': .2, 'economics': economics})['decision'] != 'AUTO_LAUNCH'


def test_loss_making_opportunity_requires_review():
    economics = growth.product_economics({'Price': '100', 'COGS': '50', 'Amazon_Fees': '15', 'Shipping_Cost': '15'})
    assert opportunity.classify_opportunity({'sales': 100, 'spend': 30, 'orders': 3, 'clicks': 10, 'acos': .3, 'conversion_rate': .3, 'economics': economics})['decision'] != 'AUTO_LAUNCH'
    assert growth.product_economics({'Gross_Margin': '1%'})['gross_margin'] == .01


def test_pause_rejection_is_http_failure(monkeypatch):
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'review')
    amazon = MagicMock()
    amazon.put.return_value = {'campaigns': {'error': [{'code': 'INVALID_ARGUMENT'}]}}
    with patch.object(final, 'AmazonAdsClient', return_value=amazon):
        response = TestClient(final.app).post('/api/update-campaign-state', headers={'X-Daily-Optimizer-Token': 'review'}, json={'apply_live': True, 'campaign_id': '1', 'state': 'PAUSED'})
    assert response.status_code == 502
    assert response.json()['success'] is False


def test_negative_rejection_is_not_counted_as_applied():
    amazon = MagicMock()
    amazon.list_campaign_negative_keywords.return_value = []
    amazon.create_negative_keywords.return_value = {'campaignNegativeKeywords': {'error': [{'code': 'INVALID_ARGUMENT'}]}}
    with pytest.raises(RuntimeError, match='incomplete'):
        apply_negatives_step_with_match_types(amazon, {'negatives': [{'campaign_id': 1, 'term': 'wrong intent'}]})


def test_negative_retry_skips_already_accepted_terms():
    amazon = MagicMock()
    amazon.list_campaign_negative_keywords.return_value = [{'keywordText': 'wrong intent', 'matchType': 'NEGATIVE_EXACT', 'state': 'ENABLED'}]
    result = apply_negatives_step_with_match_types(amazon, {'negatives': [{'campaign_id': 1, 'term': 'wrong intent'}]})
    assert result[0]['count'] == 0
    amazon.create_negative_keywords.assert_not_called()


def test_harvest_respects_final_bid_ceiling():
    amazon = MagicMock()
    amazon.list_keywords.return_value = []
    amazon.create_keywords.return_value = {'keywords': {'success': [{'keywordId': 'k'}]}}
    with patch.object(final, 'load_products', return_value=[{'Product_ID': 'p', 'Title': 'Example', 'SKU': 's'}]), patch.object(extended, '_find_existing_launch_campaigns', return_value={'AUTO_DISCOVERY': {'campaignId': '1'}, 'MANUAL_EXACT': {'campaignId': '2'}}), patch.object(extended, '_first_ad_group_id', return_value='3'), patch.object(extended, 'classify_search_terms', return_value={'winners': [{'term': 'soil booster', 'sales': 100, 'acos': .1}]}), patch.object(extended, 'choose_budget_protected_bid', return_value=(0, 0, 2.5)):
        response = final.harvest_report_rows({'apply_live': True}, amazon, [], 'r', 'start', 'end')
    assert response.status_code == 200
    assert amazon.create_keywords.call_args.args[0][0]['bid'] == 2.5


def test_missing_recommendation_still_reduces_bad_campaign(monkeypatch):
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'review')
    amazon = MagicMock()
    amazon.list_campaigns.return_value = [{'campaignId': '1', 'state': 'ENABLED'}]
    amazon.list_ad_groups.return_value = [{'campaignId': '1', 'adGroupId': '2', 'defaultBid': 1.0}]
    amazon.get_ad_group_bid_recommendation.return_value = {}
    amazon.put.return_value = {'adGroups': {'success': [{'adGroupId': '2'}]}}
    with patch.object(bids, 'AmazonAdsClient', return_value=amazon), patch.object(bids, '_load_baseline_bids', return_value={'2': 1.0}), patch.object(bids, '_sales_accelerator_bid_multipliers', return_value={}), patch.object(bids, 'get_budget_protection_mode', return_value='TAPER'), patch.object(core, '_get_cached_dashboard_summary', return_value=({}, {'1': {'spend': 50, 'sales': 0}})), patch.object(core, '_dash_summary_cache', {'ts': time.time()}), patch.object(bids, '_save_baseline_bids'), patch.object(bids, 'retune_keywords', return_value={'update_errors': 0}):
        result = json.loads(bids.api_retune_existing_bids({'apply_live': True}, None, 'review').body)
    assert result['updates_applied'] == 1
    assert amazon.put.call_args.args[1]['adGroups'][0]['defaultBid'] <= .45


def test_product_prefix_does_not_match_other_product():
    assert opportunity._matching_launch_campaigns([{'name': 'Soil Booster Extra | AUTO DISCOVERY | date', 'state': 'ENABLED'}], 'Soil Booster', 'AUTO_DISCOVERY') == []


def test_growth_write_failure_remains_failure_on_retry():
    item = {'product_id': 'p', 'decision': 'SCALE', 'campaigns': [{'campaignId': '1', 'state': 'ENABLED', 'name': 'P | MANUAL EXACT | date', 'budget': {'budget': 20}}]}
    state = {}
    with patch.object(growth, 'evaluate_products', return_value=[item]), patch.object(growth, '_set_budget_verified', side_effect=RuntimeError('rejected')) as write:
        assert growth.run_growth(MagicMock(), state, True)['success'] is False
        assert growth.run_growth(MagicMock(), state, True)['success'] is False
    assert write.call_count == 1


def test_monitor_reports_missing_and_failed_growth():
    report = summarize({'profile': {'sales-growth': {'last_result': {'success': False}}}})
    assert any('sales-growth' in message for message in report['alerts'])
    assert any('opportunity' in message for message in report['alerts'])


def test_dashboard_reads_campaign_specific_bids_and_labels_partial():
    amazon = MagicMock()
    amazon.list_ad_groups.return_value = [{'campaignId': '1', 'adGroupId': 'g1', 'state': 'ENABLED', 'defaultBid': .4}, {'campaignId': '2', 'adGroupId': 'g2', 'state': 'ENABLED', 'defaultBid': .9}]
    amazon.list_keywords.side_effect = lambda cid: [{'adGroupId': 'g'+cid, 'state': 'ENABLED', 'bid': 1.2 if cid == '1' else .8}]
    amazon.list_targets.side_effect = RuntimeError('unavailable')
    campaigns = [{'campaignId': '1'}, {'campaignId': '2'}]
    enrich_campaign_bids(amazon, campaigns)
    assert [(c['configuredBidLow'], c['configuredBidHigh']) for c in campaigns] == [(.4, 1.2), (.8, .9)]
    assert all(c['bidDataStatus'] == 'partial' for c in campaigns)


def test_manual_reports_survive_other_process(monkeypatch):
    monkeypatch.setenv('AUTOMATION_STATE_BUCKET', 'test')
    stored = {}
    class MemoryStore:
        def __init__(self, name): self.name = name
        def save(self, value): stored[self.name] = value
        def read(self): return stored.get(self.name, {})
    with patch('automation_store.GCSState', MemoryStore):
        core._save_pending_report({'report_id': 'report-1', 'settings': {'apply_live': True}})
        assert core._load_pending_report()['report_id'] == 'report-1'
        core._save_optimizer_history({'report_id': 'report-1'})
        assert core._load_optimizer_history() == [{'report_id': 'report-1'}]


def test_unverified_opportunity_write_is_failure(monkeypatch):
    item = {'product_id': 'p', 'key': 'p:k:test', 'clicks': 10, 'orders': 3, 'sales': 100, 'spend': 10, 'acos': .1, 'conversion_rate': .3, 'economics': growth.product_economics({'Gross_Margin': '40%'})}
    monkeypatch.setattr(opportunity, 'build_discovery_map', lambda client: {})
    monkeypatch.setattr(opportunity, 'aggregate_opportunities', lambda rows, mapping: [item])
    monkeypatch.setattr(opportunity, 'launch_opportunity', MagicMock(side_effect=RuntimeError('rejected')))
    assert opportunity.process_opportunities([], MagicMock(), {}, True)['success'] is False


def test_manual_http_apply_refuses_occupied_lock(monkeypatch):
    from automation_store import StateBusy
    monkeypatch.setenv('AUTOMATION_STATE_BUCKET', 'test')
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'review')
    with patch('automation_store.GCSState') as store, patch.object(core, 'AmazonAdsClient') as amazon:
        store.return_value.locked.side_effect = StateBusy('busy')
        response = TestClient(final.app).post('/api/apply-optimization', headers={'X-Daily-Optimizer-Token': 'review'}, json={'apply_live': True})
    assert response.status_code == 409
    amazon.assert_not_called()


def test_dashboard_bid_read_failure_does_not_invent_values():
    amazon = MagicMock()
    amazon.list_ad_groups.side_effect = RuntimeError('unavailable')
    campaigns = [{'campaignId': '1'}]
    enrich_campaign_bids(amazon, campaigns)
    assert campaigns == [{'campaignId': '1', 'bidDataStatus': 'unavailable'}]
