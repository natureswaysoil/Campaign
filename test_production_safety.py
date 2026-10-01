"""Regression tests for the deployed final_server stack; no external requests."""
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import final_server
import optimize_campaigns as core
import server_with_bids as bids
import extended_server


@pytest.fixture(autouse=True)
def configured_token(monkeypatch):
    monkeypatch.setenv('DAILY_OPTIMIZER_TOKEN', 'test-safety-token')


client = TestClient(final_server.app)
HEADERS = {'X-Daily-Optimizer-Token': 'test-safety-token'}


def test_missing_config_rejects_api_but_health_works(monkeypatch):
    monkeypatch.delenv('DAILY_OPTIMIZER_TOKEN')
    with patch.object(final_server, 'AmazonAdsClient') as amazon:
        assert client.post('/api/update-campaign-state', json={'campaign_id': '1', 'state': 'PAUSED'}).status_code == 503
        amazon.assert_not_called()
    assert client.get('/health').status_code == 200


@pytest.mark.parametrize('path', ['/api/products', '/api/dashboard-data', '/api/agent/ppc/status'])
def test_all_api_reads_require_auth(path):
    assert client.get(path).status_code == 403


def test_bearer_token_supported():
    assert client.get('/api/agent/ppc/status', headers={'Authorization': 'Bearer test-safety-token'}).status_code == 200


@pytest.mark.parametrize('path', ['/api/create-campaign-from-product', '/api/retune-existing-bids', '/api/agent/ppc/run', '/api/harvest-all-discovery'])
@pytest.mark.parametrize('value', ['false', 'true', 1, None])
def test_non_boolean_flags_rejected_before_actions(path, value):
    with patch.object(core.AmazonAdsClient, '_get_token') as token:
        response = client.post(path, json={'apply_live': value}, headers=HEADERS)
        assert response.status_code == 422
        token.assert_not_called()


@pytest.mark.parametrize('payload', [{}, {'dry_run': True}, {'apply_live': True, 'dry_run': True}])
def test_report_preview_never_arms_live_changes(payload):
    amazon = MagicMock()
    amazon.request_report.return_value = 'r1'
    with patch.object(core, 'AmazonAdsClient', return_value=amazon), patch.object(core, '_save_pending_report') as save:
        response = client.post('/api/run-optimizer', json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['dry_run'] is True
    settings = save.call_args.args[0]['settings']
    assert settings['apply_live'] is settings['apply_negatives'] is settings['apply_winners'] is False


@pytest.mark.parametrize('settings,request_payload,expected_live', [
    ({'apply_live': False}, {'apply_live': True}, False),
    ({'apply_live': True}, {}, False),
    ({'apply_live': True}, {'apply_live': True, 'dry_run': True}, False),
    ({}, {'apply_live': True}, False),
    ({'apply_live': True}, {'apply_live': True}, True),
])
def test_apply_requires_both_report_and_request_live_intent(settings, request_payload, expected_live):
    amazon = MagicMock()
    amazon.get.return_value = {'status': 'COMPLETED', 'url': 'https://example.test/report'}
    amazon.download_binary.return_value = b'[]'
    pending = {'report_id': 'r1', 'settings': {**settings, 'apply_negatives': True, 'apply_winners': True}}
    with (patch.object(core, 'AmazonAdsClient', return_value=amazon),
          patch.object(core, '_load_pending_report', return_value=pending),
          patch.object(core, 'apply_negatives_step', return_value=[]) as negatives,
          patch.object(core, '_apply_winner_keywords', return_value=[]) as winners,
          patch.object(core, '_save_pending_report') as save,
          patch.object(core, '_save_optimizer_history')):
        response = client.post('/api/apply-optimization', json=request_payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['dry_run'] is not expected_live
    assert negatives.called is winners.called is save.called is expected_live


@pytest.mark.parametrize('payload', [{}, {'apply_live': False}, {'apply_live': True, 'dry_run': True}])
def test_launch_requires_explicit_live_intent(payload):
    with (patch.object(extended_server, '_product_from_key', return_value=({'title': 'Example'}, {})),
          patch.object(extended_server, 'AmazonAdsClient'),
          patch.object(extended_server, '_find_existing_launch_campaigns', return_value={}),
          patch.object(extended_server.base, 'api_create_recommended_campaigns') as launch):
        response = client.post('/api/create-campaign-from-product', json={'product_id': 'p1', **payload}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['dry_run'] is True
    launch.assert_not_called()


@pytest.mark.parametrize('metrics', [{'spend': 80, 'sales': 40}, {'spend': 80, 'sales': 0}])
def test_bad_campaign_never_raises_current_bid(metrics):
    result, active, _ = bids._acos_protected_bid(2.30, 2.00, metrics, current_bid=0.20)
    assert active is True
    assert result <= 0.20


@pytest.mark.parametrize('age,has_metrics,live', [(0, True, True), (0, False, True), (3600, True, True), (0, True, False)])
def test_retune_safety_at_http_boundary(age, has_metrics, live):
    amazon = MagicMock()
    amazon.list_ad_groups.return_value = [{'adGroupId': 'a1', 'campaignId': 'c1', 'defaultBid': .20}]
    amazon.list_campaigns.return_value = [{'campaignId': 'c1', 'state': 'ENABLED'}]
    amazon.list_keywords.return_value = []
    amazon.get_ad_group_bid_recommendation.return_value = {'suggested': 2.0}
    amazon.put.return_value = {'adGroups': {'success': [{'adGroupId': 'a1'}]}}
    metrics = {'c1' if has_metrics else 'other': {'spend': 80, 'sales': 40}}
    with (patch.object(bids, 'AmazonAdsClient', return_value=amazon),
          patch.object(core, '_get_cached_dashboard_summary', return_value=({}, metrics)),
          patch.object(core, '_dash_summary_cache', {'ts': time.time() - age}),
          patch.object(bids, '_load_baseline_bids', return_value={}),
          patch.object(bids, '_save_baseline_bids') as save,
          patch.object(bids, 'get_budget_protection_mode', return_value='PRIME')):
        response = client.post('/api/retune-existing-bids', json={'apply_live': live}, headers=HEADERS)
    if age:
        assert response.status_code == 503
    else:
        assert response.status_code == 200
        assert response.json()['preview'][0]['newBid'] <= .20
    amazon.put.assert_not_called()
    if not live:
        save.assert_not_called()


@pytest.mark.parametrize('apply_live', [False, True])
def test_state_changes_require_explicit_live_flag(apply_live):
    amazon = MagicMock()
    amazon.put.return_value = {'campaigns': {'success': [{'campaignId': '1'}]}}
    with patch.object(final_server, 'AmazonAdsClient', return_value=amazon):
        response = client.post('/api/update-campaign-state', json={
            'campaign_id': '1', 'state': 'PAUSED', 'apply_live': apply_live,
        }, headers=HEADERS)
    assert response.status_code == 200
    assert amazon.put.called is apply_live


def test_healthy_campaign_can_still_raise_bid():
    bid, active, _ = bids._acos_protected_bid(1.15, 1.0, {'spend': 30, 'sales': 150}, current_bid=.20)
    assert bid == 1.15
    assert active is False


def test_live_report_explicitly_arms_selected_actions():
    amazon = MagicMock()
    amazon.request_report.return_value = 'r1'
    with patch.object(core, 'AmazonAdsClient', return_value=amazon), patch.object(core, '_save_pending_report') as save:
        response = client.post('/api/run-daily-optimization', json={
            'apply_live': True, 'apply_negatives_live': True, 'apply_winners_live': False,
        }, headers=HEADERS)
    assert response.status_code == 200
    settings = save.call_args.args[0]['settings']
    assert settings['apply_live'] is settings['apply_negatives'] is True
    assert settings['apply_winners'] is False
