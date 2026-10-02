import time
from datetime import date
from unittest.mock import MagicMock
import pytest
import sales_growth_engine as growth


def test_scale_requires_real_profit_after_ads():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 20, "sales": 100, "clicks": 20, "orders": 4, "impressions": 1000},
        {
            "gross_margin": 0.40,
            "break_even_acos": 0.40,
            "profitable_acos": 0.30,
            "economics_source": "product_costs",
        },
    )
    assert item["decision"] == "SCALE"
    assert item["metrics"]["estimated_profit_after_ads"] == 20.0


def test_listing_problem_beats_more_ad_spend():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 25, "sales": 0, "clicks": 20, "orders": 0, "impressions": 1500},
        {
            "gross_margin": 0.40,
            "break_even_acos": 0.40,
            "profitable_acos": 0.30,
            "economics_source": "default_margin",
        },
    )
    assert item["decision"] == "FIX_LISTING"


def test_low_inventory_blocks_scaling():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 15, "sales": 100, "clicks": 20, "orders": 5, "impressions": 1000},
        {
            "gross_margin": 0.40,
            "break_even_acos": 0.40,
            "profitable_acos": 0.30,
            "economics_source": "product_costs",
        },
        inventory_units=5,
    )
    assert item["decision"] == "RESTOCK"


def test_borderline_winner_is_recommendation_not_scale():
    item = growth.classify_product(
        {"product_id": "p1", "title": "Liquid Kelp", "sku": "s1", "asin": "B012345678"},
        {"spend": 15, "sales": 50, "clicks": 10, "orders": 1, "impressions": 700},
        {
            "gross_margin": 0.45,
            "break_even_acos": 0.45,
            "profitable_acos": 0.35,
            "economics_source": "product_costs",
        },
    )
    assert item["decision"] == "PROMOTE"


def test_product_economics_uses_real_costs_when_available():
    econ = growth.product_economics({
        "Selling Price": "39.99",
        "COGS": "10.00",
        "Amazon Fees": "6.00",
        "Shipping Cost": "8.00",
    })
    assert econ["economics_source"] == "product_costs"
    assert econ["break_even_acos"] > 0
    assert econ["profitable_acos"] < econ["break_even_acos"]



def test_partial_costs_do_not_override_explicit_margin():
    econ = growth.product_economics({
        "Selling Price": "100",
        "COGS": "50",
        "Amazon Fees": "",
        "Shipping Cost": "",
        "Gross Margin": "20%",
    })
    assert econ["economics_source"] == "product_margin"
    assert econ["gross_margin"] == 0.20


def test_campaign_matching_uses_exact_safe_title_prefix(monkeypatch):
    client = MagicMock()
    client.list_campaigns.return_value = [
        {"campaignId": "kelp", "name": "Kelp | MANUAL EXACT | 2026-10-01", "state": "ENABLED"},
        {"campaignId": "liquid", "name": "Liquid Kelp | MANUAL EXACT | 2026-10-01", "state": "ENABLED"},
    ]
    monkeypatch.setattr(growth, "load_products", lambda: [{"Title": "Kelp", "Product_ID": "p1"}])
    monkeypatch.setattr(
        growth.extended_server.base.optimizer_core,
        "_get_cached_dashboard_summary",
        lambda: ({}, {"kelp": {"sales": 10}, "liquid": {"sales": 100}}),
    )
    monkeypatch.setattr(
        growth.extended_server.base.optimizer_core,
        "_dash_summary_cache",
        {"ts": time.time(), "summary": {}, "per_campaign": {}, "refreshing": False},
    )
    rows = growth.evaluate_products(client)
    assert len(rows) == 1
    assert [c["campaignId"] for c in rows[0]["campaigns"]] == ["kelp"]


def test_live_growth_waits_for_fresh_metrics(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        growth.extended_server.base.optimizer_core,
        "_get_cached_dashboard_summary",
        lambda: ({}, {}),
    )
    monkeypatch.setattr(
        growth.extended_server.base.optimizer_core,
        "_dash_summary_cache",
        {"ts": 0.0, "summary": None, "per_campaign": {}, "refreshing": True},
    )
    with pytest.raises(growth.GrowthMetricsNotReady):
        growth.evaluate_products(client, require_fresh=True)


def test_scale_reservation_is_saved_before_budget_write(monkeypatch):
    item = {
        "product_id": "p1",
        "title": "Liquid Kelp",
        "decision": "SCALE",
        "campaigns": [{
            "campaignId": "c1",
            "name": "Liquid Kelp | MANUAL EXACT | 2026-10-01",
            "state": "ENABLED",
            "budget": {"budget": 10, "budgetType": "DAILY"},
        }],
    }
    monkeypatch.setattr(growth, "evaluate_products", lambda client, require_fresh=False: [item])
    monkeypatch.setattr(growth, "eastern_day", lambda: date(2026, 10, 2))
    events = []
    def persist(state):
        events.append(("persist", dict(state["daily_scale_counts"])))
    def write(client, campaign, budget):
        events.append(("write", budget))
        return {"campaign_id": "c1", "old_budget": 10.0, "new_budget": budget}
    monkeypatch.setattr(growth, "_set_budget_verified", write)
    state = {}
    result = growth.run_growth(MagicMock(), state, True, persist)
    assert events[0][0] == "persist"
    assert events[1][0] == "write"
    assert result["auto_scales_today"] == 1
    assert state["daily_scale_counts"]["2026-10-02"] == 1


def test_existing_scale_reservation_prevents_repeat(monkeypatch):
    item = {
        "product_id": "p1",
        "title": "Liquid Kelp",
        "decision": "SCALE",
        "campaigns": [{
            "campaignId": "c1",
            "name": "Liquid Kelp | MANUAL EXACT | 2026-10-01",
            "state": "ENABLED",
            "budget": {"budget": 10, "budgetType": "DAILY"},
        }],
    }
    monkeypatch.setattr(growth, "evaluate_products", lambda client, require_fresh=False: [item])
    monkeypatch.setattr(growth, "eastern_day", lambda: date(2026, 10, 2))
    state = {
        "daily_scale_counts": {"2026-10-02": 1},
        "scale_reservations": {"2026-10-02:p1": {"status": "reserved"}},
    }
    write = MagicMock()
    monkeypatch.setattr(growth, "_set_budget_verified", write)
    result = growth.run_growth(MagicMock(), state, True, lambda current: None)
    write.assert_not_called()
    assert result["auto_scales_today"] == 1
    assert state["recommendations"]["p1"]["reason"] == "scale_reserved_or_completed_today"
