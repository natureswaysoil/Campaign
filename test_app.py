"""Tests for Amazon Ads API SP v3 request format."""
import json as json_module
import pytest
from unittest.mock import MagicMock, patch, call

# Patch out Secret Manager before importing app
import sys
sys.modules.setdefault("google.cloud", MagicMock())
sys.modules.setdefault("google.cloud.secretmanager", MagicMock())

import os
os.environ.setdefault("AMAZON_ADS_CLIENT_ID", "test-client-id")
os.environ.setdefault("AMAZON_ADS_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("AMAZON_ADS_REFRESH_TOKEN", "test-refresh-token")
os.environ.setdefault("AMAZON_ADS_PROFILE_ID", "123456789")
os.environ.setdefault("AMAZON_ADS_REGION", "na")

import app as app_module


# ---------------------------------------------------------------------------
# keyword_rows: SP v3 format
# ---------------------------------------------------------------------------

class TestKeywordRows:
    def test_includes_campaign_id(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert all(r["campaignId"] == 111 for r in rows)

    def test_includes_ad_group_id(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert all(r["adGroupId"] == 222 for r in rows)

    def test_three_match_types_per_keyword(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert len(rows) == 3
        match_types = {r["matchType"] for r in rows}
        assert match_types == {"EXACT", "PHRASE", "BROAD"}

    def test_state_is_uppercase_enabled(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert all(r["state"] == "ENABLED" for r in rows)

    def test_no_lowercase_state(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert not any(r["state"] == "enabled" for r in rows)

    def test_no_lowercase_match_type(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        for r in rows:
            assert r["matchType"] == r["matchType"].upper(), (
                f"matchType {r['matchType']!r} should be uppercase"
            )

    def test_bid_values_are_scalars(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        for r in rows:
            assert isinstance(r["bid"], (int, float))
            assert not isinstance(r["bid"], list)

    def test_multiple_keywords(self):
        kws = ["fertilizer", "organic", "lawn care"]
        rows = app_module.keyword_rows(kws, 111, 222, 1.0)
        assert len(rows) == len(kws) * 3


# ---------------------------------------------------------------------------
# negative_keyword_rows: SP v3 format
# ---------------------------------------------------------------------------

class TestNegativeKeywordRows:
    def test_state_is_uppercase_enabled(self):
        rows = app_module.negative_keyword_rows(["free", "cheap"], 111)
        assert all(r["state"] == "ENABLED" for r in rows)

    def test_match_type_is_camel_case_negative_exact(self):
        rows = app_module.negative_keyword_rows(["free"], 111)
        assert rows[0]["matchType"] == "negativeExact"

    def test_includes_campaign_id(self):
        rows = app_module.negative_keyword_rows(["free"], 111)
        assert rows[0]["campaignId"] == 111

    def test_one_row_per_term(self):
        rows = app_module.negative_keyword_rows(["free", "cheap", "trial"], 111)
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# AmazonAdsClient.post: batch-key wrapping
# ---------------------------------------------------------------------------

class TestAmazonAdsClientPost:
    """Verify that the post() method wraps lists in the correct SP v3 batch key."""

    def _make_client(self):
        """Return a partially-mocked AmazonAdsClient."""
        with patch.object(app_module.AmazonAdsClient, "_get_token", return_value="tok"):
            client = app_module.AmazonAdsClient.__new__(app_module.AmazonAdsClient)
            client.client_id = "cid"
            client.client_secret = "csec"
            client.refresh_token = "reftok"
            client.profile_id = "123456789"
            client.region = "na"
            client.base_url = app_module.BASE_URLS["na"]
            client.access_token = "tok"
            import requests
            client.session = requests.Session()
        return client

    def _mock_response(self, body: dict, status_code: int = 200):
        resp = MagicMock()
        resp.ok = (status_code < 400)
        resp.status_code = status_code
        resp.text = json_module.dumps(body)
        resp.json.return_value = body
        return resp

    def _assert_wrapped(self, client, endpoint, batch_key, payload_list):
        """Assert that posting a list to an SP v3 endpoint wraps it correctly."""
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["body"] = json
            return self._mock_response(
                {batch_key: {"success": [], "error": []}}
            )

        with patch.object(client.session, "post", side_effect=fake_post):
            client.post(endpoint, payload_list)

        body = captured["body"]
        assert isinstance(body, dict), "Root body must be a dict, not a list"
        assert batch_key in body, f"Batch key '{batch_key}' must be present"
        assert isinstance(body[batch_key], list), f"Value of '{batch_key}' must be a list"

    def test_campaigns_wrapped(self):
        client = self._make_client()
        self._assert_wrapped(
            client, "/sp/campaigns", "campaigns",
            [{"name": "test", "state": "ENABLED"}]
        )

    def test_ad_groups_wrapped(self):
        client = self._make_client()
        self._assert_wrapped(
            client, "/sp/adGroups", "adGroups",
            [{"name": "test", "campaignId": 1, "state": "ENABLED"}]
        )

    def test_product_ads_wrapped(self):
        client = self._make_client()
        self._assert_wrapped(
            client, "/sp/productAds", "productAds",
            [{"campaignId": 1, "adGroupId": 2, "state": "ENABLED"}]
        )

    def test_keywords_wrapped(self):
        client = self._make_client()
        self._assert_wrapped(
            client, "/sp/keywords", "keywords",
            [{"campaignId": 1, "adGroupId": 2, "keywordText": "foo", "matchType": "EXACT", "state": "ENABLED", "bid": 1.0}]
        )

    def test_campaign_negative_keywords_wrapped(self):
        client = self._make_client()
        self._assert_wrapped(
            client, "/sp/campaignNegativeKeywords", "campaignNegativeKeywords",
            [{"campaignId": 1, "keywordText": "free", "matchType": "negativeExact", "state": "ENABLED"}]
        )

    def test_reports_not_wrapped(self):
        """The reporting endpoint uses a plain dict body — no batch key wrapping."""
        client = self._make_client()
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["body"] = json
            return self._mock_response({"reportId": "abc123"})

        body = {"startDate": "2024-01-01", "endDate": "2024-01-10", "configuration": {}}
        with patch.object(client.session, "post", side_effect=fake_post):
            client.post("/reporting/reports", body)

        assert captured["body"] == body, "Reporting endpoint body must not be wrapped"


# ---------------------------------------------------------------------------
# Campaign payload format: SP v3 budget object
# ---------------------------------------------------------------------------

class TestCampaignPayloadFormat:
    """Validate the campaign payload uses the SP v3 budget object format."""

    def test_campaign_uses_budget_object_not_daily_budget(self):
        """Campaign payload must use budget:{budget,budgetType} not dailyBudget."""
        product = {
            "sku": "TEST-SKU",
            "asin": "B000TEST01",
            "title": "Test Product for Fertilizer",
            "suggested_budget": 25.0,
            "suggested_bid": 0.85,
            "product_id": "TEST_001",
        }

        captured_bodies = []

        def fake_post(url, headers=None, json=None, timeout=None):
            captured_bodies.append((url, json))
            # Return a plausible SP v3 success response
            if "/sp/campaigns" in url:
                return _mock_ok({"campaigns": {"success": [{"campaign": {"campaignId": 123}, "index": 0}], "error": []}})
            if "/sp/adGroups" in url:
                return _mock_ok({"adGroups": {"success": [{"adGroup": {"adGroupId": 456}, "index": 0}], "error": []}})
            if "/sp/productAds" in url:
                return _mock_ok({"productAds": {"success": [], "error": []}})
            if "/sp/keywords" in url:
                return _mock_ok({"keywords": {"success": [], "error": []}})
            return _mock_ok({})

        def _mock_ok(body):
            r = MagicMock()
            r.ok = True
            r.status_code = 200
            r.text = json_module.dumps(body)
            r.json.return_value = body
            return r

        with patch.object(app_module.AmazonAdsClient, "_get_token", return_value="tok"), \
             patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.post.side_effect = fake_post
            mock_session_cls.return_value = mock_session

            app_module.create_live_campaign_for_product(product)

        # Find the campaign creation call
        campaign_call = next(
            (body for url, body in captured_bodies if "/sp/campaigns" in url),
            None,
        )
        assert campaign_call is not None, "No POST to /sp/campaigns found"

        # The body must be a dict with 'campaigns' key
        assert isinstance(campaign_call, dict), "Campaign request body must be a dict"
        assert "campaigns" in campaign_call, "Campaign body must have 'campaigns' key"

        # Each campaign must have 'budget' as an object, NOT 'dailyBudget'
        for campaign in campaign_call["campaigns"]:
            assert "dailyBudget" not in campaign, (
                "SP v3 does not accept 'dailyBudget'; use 'budget' object instead"
            )
            assert "budget" in campaign, (
                "SP v3 campaign must have 'budget' object"
            )
            budget = campaign["budget"]
            assert isinstance(budget, dict), "'budget' must be an object/dict"
            assert "budget" in budget, "'budget.budget' (amount) must be present"
            assert "budgetType" in budget, "'budget.budgetType' must be present"
            assert isinstance(budget["budget"], (int, float)), "'budget.budget' must be numeric"
            assert not isinstance(budget["budget"], list), "'budget.budget' must not be a list"

    def test_campaign_state_is_uppercase(self):
        """Campaign state must be 'ENABLED' (uppercase) for SP v3."""
        product = {
            "sku": "TEST-SKU",
            "asin": "B000TEST01",
            "title": "Test Product",
            "suggested_budget": 25.0,
            "suggested_bid": 0.85,
            "product_id": "TEST_001",
        }

        captured_bodies = []

        def fake_post(url, headers=None, json=None, timeout=None):
            captured_bodies.append((url, json))
            r = MagicMock()
            r.ok = True
            r.status_code = 200
            if "/sp/campaigns" in url:
                body = {"campaigns": {"success": [{"campaign": {"campaignId": 1}, "index": 0}], "error": []}}
            elif "/sp/adGroups" in url:
                body = {"adGroups": {"success": [{"adGroup": {"adGroupId": 2}, "index": 0}], "error": []}}
            else:
                body = {}
            r.text = json_module.dumps(body)
            r.json.return_value = body
            return r

        with patch.object(app_module.AmazonAdsClient, "_get_token", return_value="tok"), \
             patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.post.side_effect = fake_post
            mock_session_cls.return_value = mock_session
            app_module.create_live_campaign_for_product(product)

        for url, body in captured_bodies:
            if "/sp/campaigns" in url:
                for campaign in body.get("campaigns", []):
                    assert campaign.get("state") == "ENABLED", (
                        f"Campaign state must be 'ENABLED', got {campaign.get('state')!r}"
                    )

    def test_campaign_start_date_is_iso_format(self):
        """Campaign startDate must be YYYY-MM-DD (e.g. '2026-03-19'), not YYYYMMDD."""
        import re
        product = {
            "sku": "TEST-SKU",
            "asin": "B000TEST01",
            "title": "Test Product",
            "suggested_budget": 25.0,
            "suggested_bid": 0.85,
            "product_id": "TEST_001",
        }

        captured_bodies = []

        def fake_post(url, headers=None, json=None, timeout=None):
            captured_bodies.append((url, json))
            r = MagicMock()
            r.ok = True
            r.status_code = 200
            if "/sp/campaigns" in url:
                body = {"campaigns": {"success": [{"campaign": {"campaignId": 1}, "index": 0}], "error": []}}
            elif "/sp/adGroups" in url:
                body = {"adGroups": {"success": [{"adGroup": {"adGroupId": 2}, "index": 0}], "error": []}}
            else:
                body = {}
            r.text = json_module.dumps(body)
            r.json.return_value = body
            return r

        with patch.object(app_module.AmazonAdsClient, "_get_token", return_value="tok"), \
             patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.post.side_effect = fake_post
            mock_session_cls.return_value = mock_session
            app_module.create_live_campaign_for_product(product)

        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for url, body in captured_bodies:
            if "/sp/campaigns" in url:
                for campaign in body.get("campaigns", []):
                    start_date = campaign.get("startDate")
                    assert start_date is not None, "Campaign must have a startDate"
                    assert iso_pattern.match(start_date), (
                        f"startDate must be YYYY-MM-DD format, got {start_date!r}"
                    )
