from unittest.mock import MagicMock, patch
import os
import sys

sys.modules.setdefault('google.cloud', MagicMock())
sys.modules.setdefault('google.cloud.secretmanager', MagicMock())

os.environ.setdefault('AMAZON_ADS_CLIENT_ID', 'test-client-id')
os.environ.setdefault('AMAZON_ADS_CLIENT_SECRET', 'test-client-secret')
os.environ.setdefault('AMAZON_ADS_REFRESH_TOKEN', 'test-refresh-token')
os.environ.setdefault('AMAZON_ADS_PROFILE_ID', '123456789')
os.environ.setdefault('AMAZON_ADS_REGION', 'na')
os.environ.setdefault('DAILY_OPTIMIZER_TOKEN', 'secret-token')

from starlette.requests import Request

import app as app_module


def _request_with_cookie(cookie_value: str) -> Request:
    return Request({
        'type': 'http',
        'method': 'POST',
        'path': '/api/test',
        'headers': [(b'cookie', f'{app_module.SESSION_COOKIE}={cookie_value}'.encode())],
    })


class TestRuntimeAuth:
    def test_verify_internal_token_accepts_session_cookie(self):
        cookie = app_module._session_token('secret-token')
        request = _request_with_cookie(cookie)

        app_module.verify_internal_token(None, request)


class TestDashboardCache:
    def test_rebuild_dashboard_cache_keeps_only_enabled_campaigns(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            'campaigns': [
                {'campaignId': '1', 'state': 'ENABLED', 'name': 'Enabled Campaign'},
                {'campaignId': '2', 'state': 'PAUSED', 'name': 'Paused Campaign'},
            ]
        }
        mock_client = MagicMock()
        mock_client.session.post.return_value = mock_resp
        mock_client.base_url = 'https://example.test'
        mock_client.access_token = 'tok'
        mock_client.client_id = 'cid'
        mock_client.profile_id = 'pid'

        with patch.object(app_module, 'AmazonAdsClient', return_value=mock_client):
            app_module._perf_cache['data'] = {
                'campaigns': [
                    {'campaignId': '1', 'spend': 9.5, 'sales': 20.0, 'clicks': 5, 'orders': 2, 'impressions': 100, 'acos': 0.475}
                ]
            }
            app_module._rebuild_dashboard_cache()

        payload = app_module._dashboard_cache['data']
        assert payload['active_only'] is True
        assert [c['campaignId'] for c in payload['campaigns']] == ['1']
        assert payload['summary']['spend'] == 9.5


class TestLaunchStructure:
    def test_create_live_campaign_prevents_duplicate_launches(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            'campaigns': [
                {'campaignId': '10', 'name': 'Example Product | AUTO DISCOVERY | 2026-06-01', 'state': 'ENABLED'}
            ]
        }
        mock_client = MagicMock()
        mock_client.session.post.return_value = mock_resp
        mock_client.base_url = 'https://example.test'
        mock_client.headers.return_value = {'Authorization': '******'}

        with patch.object(app_module, 'AmazonAdsClient', return_value=mock_client):
            result = app_module.create_live_campaign_for_product({
                'product_id': 'p1',
                'sku': 'sku-1',
                'asin': 'B001',
                'title': 'Example Product',
                'suggested_budget': 25,
                'suggested_bid': 0.8,
                'category': 'soil',
                'keywords': 'example product',
                'research_keywords': '',
            })

        assert result['duplicate_launch_prevented'] is True
        assert 'AUTO_DISCOVERY' in result['existing_campaigns']

    def test_create_live_campaign_builds_auto_and_exact_structure(self):
        list_resp = MagicMock()
        list_resp.raise_for_status.return_value = None
        list_resp.json.return_value = {'campaigns': []}

        mock_client = MagicMock()
        mock_client.session.post.return_value = list_resp
        mock_client.base_url = 'https://example.test'
        mock_client.headers.return_value = {'Authorization': '******'}
        mock_client.post.side_effect = [
            {'campaignId': 11},
            {'adGroupId': 21},
            {},
            {'campaignId': 12},
            {'adGroupId': 22},
            {},
            {},
        ]

        with patch.object(app_module, 'AmazonAdsClient', return_value=mock_client):
            result = app_module.create_live_campaign_for_product({
                'product_id': 'p1',
                'sku': 'sku-1',
                'asin': 'B001',
                'title': 'Example Product',
                'suggested_budget': 25,
                'suggested_bid': 0.8,
                'category': 'soil',
                'keywords': 'example product, premium soil blend, compost',
                'research_keywords': 'garden booster',
            })

        assert [c['campaign_type'] for c in result['campaigns_created']] == ['AUTO_DISCOVERY', 'MANUAL_EXACT']
        assert result['campaigns_created'][1]['keyword_rows_created'] >= 1
        assert result['keyword_filtering']['exact_keywords_selected'] >= 1


class TestDashboardCompatibilityRoute:
    def test_run_optimizer_preview_returns_campaign_plan_shape(self):
        with patch.object(app_module, 'load_products', return_value=[
            {'Product_ID': 'p1', 'SKU': 'sku-1', 'ASIN': 'B001', 'Title': 'Example Product', 'Selling_Price': '19.99', 'Active': 'TRUE'}
        ]):
            payload = app_module.api_run_optimizer_preview()

        assert payload['success'] is True
        assert payload['dry_run'] is True
        assert payload['product_count'] == 1
        assert payload['output_file'] == 'campaign_plan.json'
