"""Tests for Amazon Ads API SP v3 request format."""
import json as json_module
import pytest
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient

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
        assert all(r["campaignId"] == "111" for r in rows)

    def test_includes_ad_group_id(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert all(r["adGroupId"] == "222" for r in rows)

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

    def test_campaign_id_is_string(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert all(isinstance(r["campaignId"], str) for r in rows), (
            "campaignId must be a string for SP v3 API compatibility"
        )

    def test_ad_group_id_is_string(self):
        rows = app_module.keyword_rows(["fertilizer"], 111, 222, 1.0)
        assert all(isinstance(r["adGroupId"], str) for r in rows), (
            "adGroupId must be a string for SP v3 API compatibility"
        )

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

    def test_match_type_is_uppercase_negative_exact(self):
        rows = app_module.negative_keyword_rows(["free"], 111)
        assert rows[0]["matchType"] == "NEGATIVE_EXACT"

    def test_includes_campaign_id(self):
        rows = app_module.negative_keyword_rows(["free"], 111)
        assert rows[0]["campaignId"] == "111"

    def test_campaign_id_is_string(self):
        rows = app_module.negative_keyword_rows(["free"], 111)
        assert isinstance(rows[0]["campaignId"], str), (
            "campaignId must be a string for SP v3 API compatibility"
        )

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
            [{"campaignId": 1, "keywordText": "free", "matchType": "NEGATIVE_EXACT", "state": "ENABLED"}]
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

    def test_non_report_425_retries_and_succeeds(self):
        client = self._make_client()
        first = self._mock_response({"code": "425", "detail": "duplicate"}, status_code=425)
        second = self._mock_response({"campaigns": {"success": [], "error": []}}, status_code=200)

        with patch.object(client.session, "post", side_effect=[first, second]) as mock_post:
            result = client.post("/sp/campaigns", [{"name": "x", "state": "ENABLED"}])

        assert mock_post.call_count == 2
        assert isinstance(result, dict)
        assert "campaigns" in result


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


# ---------------------------------------------------------------------------
# sanitize_campaign_name: forbidden-character stripping
# ---------------------------------------------------------------------------

class TestSanitizeCampaignName:
    """Verify that sanitize_campaign_name removes characters forbidden by Amazon Ads."""

    def test_html_entities_decoded(self):
        """HTML entities like &amp; must be decoded before sending to Amazon Ads."""
        result = app_module.sanitize_campaign_name("Orchid &amp; Violet Mix")
        assert "&amp;" not in result
        assert "&" in result

    def test_smart_apostrophe_replaced(self):
        """\u2019 (right single quotation mark) must be replaced with ASCII apostrophe."""
        result = app_module.sanitize_campaign_name("Nature\u2019s Way")
        assert "\u2019" not in result
        assert "Nature's Way" == result

    def test_registered_trademark_stripped(self):
        """\u00ae (registered sign) must be removed."""
        result = app_module.sanitize_campaign_name("Soil\u00ae Mix")
        assert "\u00ae" not in result
        assert result == "Soil Mix"

    def test_en_dash_replaced_with_hyphen(self):
        """\u2013 (en dash) must be replaced with a plain hyphen."""
        result = app_module.sanitize_campaign_name("Premium \u2013 Best")
        assert "\u2013" not in result
        assert "-" in result

    def test_em_dash_replaced_with_hyphen(self):
        """\u2014 (em dash) must be replaced with a plain hyphen."""
        result = app_module.sanitize_campaign_name("Premium \u2014 Best")
        assert "\u2014" not in result
        assert "-" in result

    def test_smart_double_quotes_replaced(self):
        """\u201c/\u201d (smart double quotes) must be replaced with ASCII quotes."""
        result = app_module.sanitize_campaign_name("\u201cPremium\u201d Mix")
        assert "\u201c" not in result
        assert "\u201d" not in result

    def test_trademark_symbol_stripped(self):
        """\u2122 (trademark sign) must be removed."""
        result = app_module.sanitize_campaign_name("Soil\u2122 Mix")
        assert "\u2122" not in result

    def test_result_is_ascii_only(self):
        """Sanitized name must contain only ASCII characters."""
        raw = "Nature\u2019s Way Soil\u00ae Orchid &amp; African Violet \u2013 Premium"
        result = app_module.sanitize_campaign_name(raw)
        result.encode("ascii")  # must not raise

    def test_whitespace_collapsed(self):
        """Extra internal whitespace must be collapsed to a single space."""
        result = app_module.sanitize_campaign_name("Orchid   Mix")
        assert "  " not in result

    def test_empty_string(self):
        """Empty input must return empty string without error."""
        assert app_module.sanitize_campaign_name("") == ""

    def test_none_input(self):
        """None input must return empty string without error."""
        assert app_module.sanitize_campaign_name(None) == ""

    def test_full_product_title_from_error(self):
        """Full title from the bug report must produce an ASCII-safe campaign name."""
        # Simulate the raw title as stored in the spreadsheet (Unicode)
        title = (
            "Nature\u2019s Way Soil\u00ae Orchid &amp; African Violet Potting Mix "
            "\u2013 Premium Coco Coir, Worm Castings, Acti"
        )
        result = app_module.sanitize_campaign_name(title)
        result.encode("ascii")  # must not raise
        assert "&amp;" not in result
        assert "\u2019" not in result
        assert "\u00ae" not in result
        assert "\u2013" not in result

    def test_campaign_name_uses_sanitized_title(self):
        """create_live_campaign_for_product must use sanitized title in campaign name."""
        import json as json_module

        product = {
            "sku": "TEST-SKU",
            "asin": "B000TEST01",
            "title": "Nature\u2019s Way Soil\u00ae Orchid &amp; African Violet",
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
                    name = campaign.get("name", "")
                    name.encode("ascii")  # must not raise
                    assert "\u2019" not in name
                    assert "\u00ae" not in name
                    assert "&amp;" not in name


# ---------------------------------------------------------------------------
# Daily optimizer: campaign pause handling
# ---------------------------------------------------------------------------

class TestCampaignPauseOptimizer:
    def test_classify_campaigns_for_pause_flags_high_acos(self):
        rows = [
            {
                "campaignId": "123",
                "campaignName": "High ACOS Campaign",
                "campaignStatus": "ENABLED",
                "impressions": 420,
                "clicks": 17,
                "cost": 240.0,
                "sales7d": 60.0,
                "purchases7d": 2,
            },
            {
                "campaignId": "456",
                "campaignName": "Healthy Campaign",
                "campaignStatus": "ENABLED",
                "impressions": 420,
                "clicks": 17,
                "cost": 40.0,
                "sales7d": 200.0,
                "purchases7d": 4,
            },
        ]

        result = app_module.classify_campaigns_for_pause(rows, pause_acos_threshold=2.0)

        assert [item["campaign_id"] for item in result["to_pause"]] == ["123"]
        assert result["to_pause"][0]["reason"] == "ACOS 400.0% exceeds 200%"
        assert [item["campaign_id"] for item in result["keep"]] == ["456"]

    def test_classify_campaigns_for_pause_flags_no_sales_campaign(self):
        rows = [
            {
                "campaignId": "789",
                "campaignName": "No Sales Campaign",
                "campaignStatus": "ENABLED",
                "impressions": 800,
                "clicks": 25,
                "cost": 60.0,
                "sales7d": 0.0,
                "purchases7d": 0,
            }
        ]

        result = app_module.classify_campaigns_for_pause(
            rows,
            pause_acos_threshold=0.40,
            pause_no_sales_campaigns=True,
            min_clicks_for_no_sales_pause=10,
        )

        assert [item["campaign_id"] for item in result["to_pause"]] == ["789"]
        assert "no attributed sales" in result["to_pause"][0]["reason"]

    def test_winner_bid_for_daypart_uses_suggested_high_in_prime(self):
        rows = [
            {"suggested_bid": 1.1, "suggested_bid_high": 1.6},
            {"suggested_bid": 1.2, "suggested_bid_high": 1.5},
        ]

        prime_bid = app_module.winner_bid_for_daypart(
            rows,
            default_bid=0.9,
            prime_mode=True,
            prime_high_bid_multiplier=1.25,
            off_prime_bid_multiplier=0.35,
        )
        off_prime_bid = app_module.winner_bid_for_daypart(
            rows,
            default_bid=0.9,
            prime_mode=False,
            prime_high_bid_multiplier=1.25,
            off_prime_bid_multiplier=0.35,
        )

        assert prime_bid == 1.6
        assert off_prime_bid == 0.56

    def test_api_run_optimizer_pauses_high_acos_campaigns(self):
        search_rows = [
            {
                "campaignId": 111,
                "adGroupId": 222,
                "searchTerm": "compost",
                "clicks": 12,
                "cost": 12.0,
                "sales7d": 120.0,
                "purchases7d": 3,
            }
        ]
        campaign_rows = [
            {
                "campaignId": "999",
                "campaignName": "Stop Me",
                "campaignStatus": "ENABLED",
                "impressions": 900,
                "clicks": 21,
                "cost": 210.0,
                "sales7d": 70.0,
                "purchases7d": 2,
            }
        ]

        class FakeClient:
            def __init__(self):
                self.put_calls = []

            def request_sp_search_term_report(self, start_date, end_date):
                return {"reportId": "terms-report"}

            def request_sp_campaign_report(self, start_date, end_date):
                return {"reportId": "campaign-report"}

            def get_report_status(self, report_id):
                return {"status": "SUCCESS", "location": report_id}

            def download_binary(self, location):
                rows = search_rows if location == "terms-report" else campaign_rows
                payload = json_module.dumps(rows).encode("utf-8")
                import gzip
                return gzip.compress(payload)

            def post(self, endpoint, body):
                return {"ok": True}

            def put(self, endpoint, body):
                self.put_calls.append((endpoint, body))
                return {"campaigns": {"success": [{"campaign": {"campaignId": "999"}, "index": 0}], "error": []}}

        fake_client = FakeClient()

        with patch.object(app_module, "AmazonAdsClient", return_value=fake_client), \
             patch.object(app_module, "record_action"), \
             patch.object(app_module, "verify_internal_token"):
            response = app_module.api_run_optimizer(
                {
                    "apply_campaign_pauses_live": True,
                    "apply_negatives_live": False,
                    "apply_winners_live": False,
                    "apply_keyword_bid_updates_live": False,
                    "pause_acos_threshold": 2.0,
                },
                authorization="Bearer test-token",
            )

        assert response["campaigns_to_pause"] == 1
        assert len(response["campaign_pauses_applied"]) == 1
        assert fake_client.put_calls == [
            (
                app_module.ENDPOINTS["campaigns"],
                [{"campaignId": "999", "state": "PAUSED"}],
            )
        ]

    def test_compute_keyword_bid_target_dayparting(self):
        row = {
            "bid": 0.6,
            "suggestedBid": 0.8,
            "suggestedBidHigh": 1.3,
            "suggestedBidLow": 0.5,
        }

        prime_bid = app_module.compute_keyword_bid_target(
            row,
            prime_mode=True,
            prime_high_bid_multiplier=1.25,
            off_prime_bid_multiplier=0.35,
        )
        off_prime_bid = app_module.compute_keyword_bid_target(
            row,
            prime_mode=False,
            prime_high_bid_multiplier=1.25,
            off_prime_bid_multiplier=0.35,
        )

        assert prime_bid == 1.3
        assert off_prime_bid == 0.17

    def test_normalize_keyword_state_for_put_maps_archived_to_paused(self):
        assert app_module.normalize_keyword_state_for_put("ARCHIVED") == "PAUSED"

    def test_normalize_keyword_state_for_put_keeps_allowed_states(self):
        assert app_module.normalize_keyword_state_for_put("ENABLED") == "ENABLED"
        assert app_module.normalize_keyword_state_for_put("PAUSED") == "PAUSED"
        assert app_module.normalize_keyword_state_for_put("PROPOSED") == "PROPOSED"

    def test_normalize_keyword_state_for_put_defaults_unknown(self):
        assert app_module.normalize_keyword_state_for_put("UNKNOWN") == "ENABLED"
        assert app_module.normalize_keyword_state_for_put("") == "ENABLED"

    def test_api_run_optimizer_updates_keyword_bids(self):
        search_rows = [
            {
                "campaignId": 111,
                "adGroupId": 222,
                "searchTerm": "compost",
                "clicks": 12,
                "cost": 12.0,
                "sales7d": 120.0,
                "purchases7d": 3,
            }
        ]
        campaign_rows = [
            {
                "campaignId": "999",
                "campaignName": "Keep Me",
                "campaignStatus": "ENABLED",
                "impressions": 900,
                "clicks": 21,
                "cost": 70.0,
                "sales7d": 210.0,
                "purchases7d": 2,
            }
        ]

        class FakeClient:
            def __init__(self):
                self.put_calls = []

            def request_sp_search_term_report(self, start_date, end_date):
                return {"reportId": "terms-report"}

            def request_sp_campaign_report(self, start_date, end_date):
                return {"reportId": "campaign-report"}

            def get_report_status(self, report_id):
                return {"status": "SUCCESS", "location": report_id}

            def download_binary(self, location):
                rows = search_rows if location == "terms-report" else campaign_rows
                payload = json_module.dumps(rows).encode("utf-8")
                import gzip
                return gzip.compress(payload)

            def list_sp_keywords(self, max_results=1000):
                return [
                    {
                        "keywordId": "501",
                        "campaignId": "999",
                        "adGroupId": "888",
                        "state": "ENABLED",
                        "bid": 0.5,
                        "suggestedBid": 0.7,
                        "suggestedBidHigh": 1.1,
                        "suggestedBidLow": 0.4,
                    }
                ]

            def post(self, endpoint, body):
                return {"ok": True}

            def put(self, endpoint, body):
                self.put_calls.append((endpoint, body))
                return {"keywords": {"success": [{"keyword": {"keywordId": "501"}, "index": 0}], "error": []}}

        fake_client = FakeClient()

        with patch.object(app_module, "AmazonAdsClient", return_value=fake_client), \
             patch.object(app_module, "record_action"), \
             patch.object(app_module, "verify_internal_token"), \
             patch.object(app_module, "is_prime_time_now", return_value=True):
            response = app_module.api_run_optimizer(
                {
                    "apply_campaign_pauses_live": False,
                    "apply_negatives_live": False,
                    "apply_winners_live": False,
                    "apply_keyword_bid_updates_live": True,
                    "max_keyword_bid_updates": 10,
                },
                authorization="Bearer test-token",
            )

        assert response["keywords_scanned"] == 1
        assert response["keyword_bids_updated"] == 1
        assert any(call[0] == app_module.ENDPOINTS["keywords"] for call in fake_client.put_calls)

    def test_api_run_optimizer_skips_archived_keywords_in_put(self):
        """Archived keywords must never appear in PUT /sp/keywords payloads."""
        search_rows: list = []
        campaign_rows = [
            {
                "campaignId": "999",
                "campaignName": "Keep Me",
                "campaignStatus": "ENABLED",
                "impressions": 900,
                "clicks": 21,
                "cost": 70.0,
                "sales7d": 210.0,
                "purchases7d": 2,
            }
        ]

        import gzip
        import json as json_module2

        class FakeClient2:
            def __init__(self):
                self.put_calls = []

            def request_sp_search_term_report(self, start_date, end_date):
                return {"reportId": "terms-report"}

            def request_sp_campaign_report(self, start_date, end_date):
                return {"reportId": "campaign-report"}

            def get_report_status(self, report_id):
                return {"status": "SUCCESS", "location": report_id}

            def download_binary(self, location):
                rows = search_rows if location == "terms-report" else campaign_rows
                payload = json_module2.dumps(rows).encode("utf-8")
                return gzip.compress(payload)

            def list_sp_keywords(self, max_results=1000):
                return [
                    # Should be updated (ENABLED)
                    {
                        "keywordId": "501",
                        "campaignId": "999",
                        "adGroupId": "888",
                        "state": "ENABLED",
                        "bid": 0.5,
                        "suggestedBid": 0.7,
                        "suggestedBidHigh": 1.1,
                        "suggestedBidLow": 0.4,
                    },
                    # Should be skipped (ARCHIVED)
                    {
                        "keywordId": "502",
                        "campaignId": "999",
                        "adGroupId": "888",
                        "state": "ARCHIVED",
                        "bid": 0.5,
                        "suggestedBid": 0.7,
                        "suggestedBidHigh": 1.1,
                        "suggestedBidLow": 0.4,
                    },
                ]

            def post(self, endpoint, body):
                return {"ok": True}

            def put(self, endpoint, body):
                self.put_calls.append((endpoint, body))
                return {"keywords": {"success": [], "error": []}}

        fake_client = FakeClient2()

        with patch.object(app_module, "AmazonAdsClient", return_value=fake_client), \
             patch.object(app_module, "record_action"), \
             patch.object(app_module, "verify_internal_token"), \
             patch.object(app_module, "is_prime_time_now", return_value=True):
            app_module.api_run_optimizer(
                {
                    "apply_campaign_pauses_live": False,
                    "apply_negatives_live": False,
                    "apply_winners_live": False,
                    "apply_keyword_bid_updates_live": True,
                    "max_keyword_bid_updates": 10,
                },
                authorization="Bearer test-token",
            )

        # All PUT payloads must contain no ARCHIVED keyword IDs
        for _endpoint, body in fake_client.put_calls:
            kw_ids = [kw.get("keywordId") for kw in body]
            assert "502" not in kw_ids, "ARCHIVED keyword must not appear in PUT payload"


class TestProductKeywordAndLaunchFlow:
    def test_generate_keywords_prefers_research_phrases(self):
        product = {
            "keywords": "fertilizer, lawn",
            "research_keywords": "organic tomato fertilizer, best tomato fertilizer",
            "title": "Nature's Way Soil Organic Tomato Liquid Fertilizer",
            "category": "lawn",
        }

        keywords = app_module.generate_keywords(product)

        assert "organic tomato fertilizer" in keywords[:5]
        assert "best tomato fertilizer" in keywords[:8]
        assert len(keywords) <= 30

    def test_launch_campaigns_from_products_defaults_live_active(self):
        with patch.object(app_module, "api_bulk_create", return_value={"ok": True}) as mock_bulk:
            result = app_module.api_launch_campaigns_from_products({})

        assert result == {"ok": True}
        called_payload = mock_bulk.call_args[0][0]
        assert called_payload["launch_only_active"] is True
        assert called_payload["dry_run"] is False
        assert called_payload["limit"] == 50

    def test_launch_campaigns_from_products_idempotent_replay(self, tmp_path):
        app_module.IDEMPOTENCY_LOG_PATH = tmp_path / "idempotency.jsonl"

        with patch.object(app_module, "api_bulk_create", return_value={"ok": True}) as mock_bulk:
            first = app_module.api_launch_campaigns_from_products({}, x_idempotency_key="launch-123")
            second = app_module.api_launch_campaigns_from_products({}, x_idempotency_key="launch-123")

        assert mock_bulk.call_count == 1
        assert first["idempotency"]["replayed"] is False
        assert second["idempotency"]["replayed"] is True

    def test_launch_campaigns_from_products_idempotency_conflict(self, tmp_path):
        app_module.IDEMPOTENCY_LOG_PATH = tmp_path / "idempotency.jsonl"

        with patch.object(app_module, "api_bulk_create", return_value={"ok": True}):
            app_module.api_launch_campaigns_from_products({"limit": 10}, x_idempotency_key="launch-123")

            with pytest.raises(app_module.HTTPException) as exc:
                app_module.api_launch_campaigns_from_products({"limit": 20}, x_idempotency_key="launch-123")

        assert exc.value.status_code == 409
        assert "Idempotency key reuse" in str(exc.value.detail)

    def test_create_campaign_from_product_idempotent_replay(self, tmp_path):
        app_module.IDEMPOTENCY_LOG_PATH = tmp_path / "idempotency.jsonl"

        fake_result = {
            "message": "Live campaign created",
            "product_id": "P1",
            "sku": "SKU-1",
            "asin": "ASIN-1",
            "campaign_id": 123,
            "ad_group_id": 456,
        }

        with patch.object(app_module, "find_product", return_value={
            "product_id": "P1",
            "sku": "SKU-1",
            "asin": "ASIN-1",
            "title": "Test Product",
        }), patch.object(app_module, "create_live_campaign_for_product", return_value=fake_result) as mock_create, \
             patch.object(app_module, "record_action"):
            first = app_module.api_create_campaign({"sku": "SKU-1"}, x_idempotency_key="single-1")
            second = app_module.api_create_campaign({"sku": "SKU-1"}, x_idempotency_key="single-1")

        assert mock_create.call_count == 1
        assert first["idempotency"]["replayed"] is False
        assert second["idempotency"]["replayed"] is True

    def test_launch_campaigns_rejects_unsafe_idempotency_key(self):
        with pytest.raises(app_module.HTTPException) as exc:
            app_module.api_launch_campaigns_from_products({}, x_idempotency_key="unsafe key!")

        assert exc.value.status_code == 400
        assert "unsafe characters" in str(exc.value.detail)


class TestOptimizerAlerts:
    def test_maybe_emit_zero_action_alert_triggers_on_streak(self):
        zero_details = {
            "winners": 0,
            "negatives": 0,
            "campaigns_to_pause": 0,
            "rows_analyzed": 301,
            "campaign_rows_analyzed": 17,
        }

        recent_optimizer = [
            {"summary": "run 3", "details": zero_details},
            {"summary": "run 2", "details": zero_details},
            {"summary": "run 1", "details": zero_details},
        ]

        def fake_recent_actions(**kwargs):
            action_types = kwargs.get("action_types") or []
            if "ops_alert" in action_types:
                return []
            if "optimizer_run" in action_types:
                return recent_optimizer
            return []

        with patch.object(app_module, "recent_actions", side_effect=fake_recent_actions), \
             patch.object(app_module, "record_action") as mock_record_action:
            app_module.maybe_emit_zero_action_alert(zero_details)

        mock_record_action.assert_called_once()
        called_kwargs = mock_record_action.call_args.kwargs
        assert called_kwargs["action_type"] == "ops_alert"
        assert called_kwargs["status"] == "warning"

    def test_api_ops_status_exposes_streak_and_last_alert(self):
        snapshot = {
            "current_zero_action_streak": 4,
            "threshold": 3,
            "window_hours": 72,
            "cooldown_hours": 12,
            "last_zero_action_alert_timestamp": "2026-04-12T18:00:00Z",
            "last_zero_action_alert_summary": "Optimizer zero-action streak detected (4 runs)",
        }

        with patch.object(app_module, "get_zero_action_streak_snapshot", return_value=snapshot), \
             patch.object(app_module, "get_retry_425_count", return_value=7):
            response = app_module.api_ops_status()

        assert response["metrics"]["retry_425_last_24h"] == 7
        assert response["alerts"]["zero_action"]["current_zero_action_streak"] == 4
        assert response["alerts"]["zero_action"]["last_zero_action_alert_timestamp"] == "2026-04-12T18:00:00Z"

    def test_api_ops_status_via_testclient(self):
        client = TestClient(app_module.app)
        snapshot = {
            "current_zero_action_streak": 2,
            "threshold": 3,
            "window_hours": 72,
            "cooldown_hours": 12,
            "last_zero_action_alert_timestamp": None,
            "last_zero_action_alert_summary": None,
        }

        with patch.object(app_module, "get_zero_action_streak_snapshot", return_value=snapshot), \
             patch.object(app_module, "get_retry_425_count", return_value=3):
            response = client.get("/api/ops-status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["metrics"]["retry_425_last_24h"] == 3
        assert payload["alerts"]["zero_action"]["current_zero_action_streak"] == 2


class TestDashboardData:
    def test_dashboard_data_includes_retry_425_metric(self):
        with patch.object(app_module, "load_products", return_value=[]), \
             patch.object(app_module, "api_optimizer_status", return_value={"schedule": "daily", "lookback_days": 14}), \
             patch.object(app_module, "api_campaign_performance", return_value={"status": "ok", "count": 0, "campaigns": []}), \
             patch.object(app_module, "get_retry_425_count", return_value=7):
            payload = app_module.api_dashboard_data()

        assert payload["ops"]["retry_425_last_24h"] == 7


class TestPublicRoutes:
    def test_root_route_returns_html(self):
        client = TestClient(app_module.app)

        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Nature's Way Soil" in response.text

    def test_health_route_returns_ok(self):
        client = TestClient(app_module.app)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
