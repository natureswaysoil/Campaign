"""Runtime-aligned tests for app.py."""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

sys.modules.setdefault("google.cloud", MagicMock())
sys.modules.setdefault("google.cloud.secretmanager", MagicMock())

os.environ.setdefault("AMAZON_ADS_CLIENT_ID", "test-client-id")
os.environ.setdefault("AMAZON_ADS_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("AMAZON_ADS_REFRESH_TOKEN", "test-refresh-token")
os.environ.setdefault("AMAZON_ADS_PROFILE_ID", "123456789")
os.environ.setdefault("AMAZON_ADS_REGION", "na")
os.environ.setdefault("DAILY_OPTIMIZER_TOKEN", "secret-token")

import app as app_module


class TestKeywordRows:
    def test_keyword_rows_emit_three_match_types(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert len(rows) == 3
        assert {r["matchType"] for r in rows} == {"EXACT", "PHRASE", "BROAD"}
        assert all(r["campaignId"] == "111" for r in rows)
        assert all(r["adGroupId"] == "222" for r in rows)
        assert all(r["state"] == "ENABLED" for r in rows)

    def test_keyword_rows_scale_for_multiple_keywords(self):
        rows = app_module.keyword_rows(["a", "b", "c"], 1, 2, 0.5)
        assert len(rows) == 9


class TestNegativeKeywordRows:
    def test_negative_keyword_rows_shape(self):
        rows = app_module.negative_keyword_rows(["free", "cheap"], 111)
        assert rows == [
            {
                "campaignId": "111",
                "keywordText": "free",
                "matchType": "NEGATIVE_EXACT",
                "state": "ENABLED",
            },
            {
                "campaignId": "111",
                "keywordText": "cheap",
                "matchType": "NEGATIVE_EXACT",
                "state": "ENABLED",
            },
        ]


class TestAmazonAdsClientPost:
    def _make_client(self):
        with patch.object(app_module.AmazonAdsClient, "_get_token", return_value="tok"):
            client = app_module.AmazonAdsClient.__new__(app_module.AmazonAdsClient)
            client.client_id = "cid"
            client.client_secret = "csec"
            client.refresh_token = "reftok"
            client.profile_id = "123456789"
            client.region = "na"
            client.base_url = app_module.BASE_URLS["na"]
            client.access_token = "tok"
            client.session = MagicMock()
        return client

    def _ok_response(self, body):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.text = "{}"
        resp.json.return_value = body
        return resp

    @pytest.mark.parametrize(
        "endpoint,batch_key,payload",
        [
            ("/sp/campaigns", "campaigns", [{"name": "test", "state": "ENABLED"}]),
            ("/sp/adGroups", "adGroups", [{"name": "g", "campaignId": "1", "state": "ENABLED"}]),
            ("/sp/productAds", "productAds", [{"campaignId": "1", "adGroupId": "2", "state": "ENABLED"}]),
            ("/sp/keywords", "keywords", [{"campaignId": "1", "adGroupId": "2", "keywordText": "k", "matchType": "EXACT", "state": "ENABLED", "bid": 0.9}]),
            ("/sp/campaignNegativeKeywords", "campaignNegativeKeywords", [{"campaignId": "1", "keywordText": "k", "matchType": "NEGATIVE_EXACT", "state": "ENABLED"}]),
        ],
    )
    def test_post_wraps_batch_endpoints(self, endpoint, batch_key, payload):
        client = self._make_client()
        client.session.post.return_value = self._ok_response({batch_key: {"success": [], "error": []}})

        client.post(endpoint, payload)

        sent_body = client.session.post.call_args.kwargs["json"]
        assert sent_body == {batch_key: payload}

    def test_post_does_not_wrap_reports_payload(self):
        client = self._make_client()
        body = {"startDate": "2026-01-01", "endDate": "2026-01-02", "configuration": {}}
        client.session.post.return_value = self._ok_response({"reportId": "abc"})

        client.post("/reporting/reports", body)

        sent_body = client.session.post.call_args.kwargs["json"]
        assert sent_body == body


class TestCampaignCreation:
    def test_duplicate_launch_is_prevented_for_existing_structures(self):
        list_resp = MagicMock()
        list_resp.raise_for_status.return_value = None
        list_resp.json.return_value = {
            "campaigns": [
                {"campaignId": "10", "name": "Example Product | AUTO DISCOVERY | 2026-06-01", "state": "ENABLED"},
                {"campaignId": "11", "name": "Example Product | MANUAL EXACT | 2026-06-01", "state": "PAUSED"},
            ]
        }

        mock_client = MagicMock()
        mock_client.session.post.return_value = list_resp
        mock_client.base_url = "https://example.test"
        mock_client.headers.return_value = {"Authorization": "***"}

        with patch.object(app_module, "AmazonAdsClient", return_value=mock_client):
            result = app_module.create_live_campaign_for_product(
                {
                    "product_id": "p1",
                    "sku": "sku-1",
                    "asin": "B001",
                    "title": "Example Product",
                    "suggested_budget": 25,
                    "suggested_bid": 0.8,
                    "category": "soil",
                    "keywords": "example",
                    "research_keywords": "",
                }
            )

        assert result["duplicate_launch_prevented"] is True
        assert set(result["existing_campaigns"].keys()) == {"AUTO_DISCOVERY", "MANUAL_EXACT"}

    def test_campaign_creation_uses_budget_object_uppercase_state_and_iso_date(self):
        list_resp = MagicMock()
        list_resp.raise_for_status.return_value = None
        list_resp.json.return_value = {"campaigns": []}

        mock_client = MagicMock()
        mock_client.session.post.return_value = list_resp
        mock_client.base_url = "https://example.test"
        mock_client.headers.return_value = {"Authorization": "***"}
        mock_client.post.side_effect = [
            {"campaigns": {"success": [{"campaign": {"campaignId": 11}, "index": 0}], "error": []}},
            {"adGroups": {"success": [{"adGroup": {"adGroupId": 21}, "index": 0}], "error": []}},
            {},
            {"campaigns": {"success": [{"campaign": {"campaignId": 12}, "index": 0}], "error": []}},
            {"adGroups": {"success": [{"adGroup": {"adGroupId": 22}, "index": 0}], "error": []}},
            {},
            {},
        ]

        with patch.object(app_module, "AmazonAdsClient", return_value=mock_client):
            result = app_module.create_live_campaign_for_product(
                {
                    "product_id": "p1",
                    "sku": "sku-1",
                    "asin": "B001",
                    "title": "Nature’s Way Soil® Orchid &amp; African Violet",
                    "suggested_budget": 25,
                    "suggested_bid": 0.8,
                    "category": "soil",
                    "keywords": "example product, premium soil blend, compost",
                    "research_keywords": "garden booster",
                }
            )

        campaign_calls = [
            c for c in mock_client.post.call_args_list if c.args[0] == app_module.ENDPOINTS["campaigns"]
        ]
        assert len(campaign_calls) == 2

        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for call in campaign_calls:
            body = call.args[1]
            assert isinstance(body, list) and body
            campaign = body[0]
            assert campaign["state"] == "ENABLED"
            assert isinstance(campaign["budget"], dict)
            assert "dailyBudget" not in campaign
            assert isinstance(campaign["budget"]["budget"], (int, float))
            assert campaign["budget"]["budgetType"] == "DAILY"
            assert iso_pattern.match(campaign["startDate"])
            campaign["name"].encode("ascii")

        assert [c["campaign_type"] for c in result["campaigns_created"]] == ["AUTO_DISCOVERY", "MANUAL_EXACT"]


class TestDashboardAndPublicRoutes:
    def test_dashboard_data_returns_cold_start_shape(self):
        app_module._dashboard_cache["data"] = None
        app_module._dashboard_cache["ts"] = 0.0
        app_module._dashboard_cache["rebuilding"] = False

        payload = app_module.api_dashboard_data(BackgroundTasks())

        assert payload["active_only"] is True
        assert payload["cache_rebuild_in_progress"] is True
        assert payload["summary"]["spend"] == 0
        assert payload["campaigns"] == []

    def test_run_optimizer_preview_matches_campaign_plan_contract(self):
        with patch.object(
            app_module,
            "load_products",
            return_value=[
                {
                    "Product_ID": "p1",
                    "SKU": "sku-1",
                    "ASIN": "B001",
                    "Title": "Example Product",
                    "Selling_Price": "19.99",
                    "Active": "TRUE",
                }
            ],
        ):
            payload = app_module.api_run_optimizer_preview()

        assert payload["success"] is True
        assert payload["dry_run"] is True
        assert payload["output_file"] == "campaign_plan.json"
        assert payload["product_count"] == 1

    def test_root_and_health_routes_are_public_and_healthy(self):
        client = TestClient(app_module.app)

        root = client.get("/")
        health = client.get("/health")

        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
        assert "Nature's Way Soil" in root.text

        assert health.status_code == 200
        payload = health.json()
        assert payload["status"] == "ok"
        assert isinstance(payload.get("time"), str)
