"""Tests for read-only bid readiness diagnostics."""
from scripts.audit_bid_readiness import audit
from profit_intelligence import Policy


class FakeAmazon:
    def list_campaigns(self):
        return [{"campaignId": "a"}, {"campaignId": "b"}]

    def list_product_ads(self, cid):
        if cid == "a":
            return [{"asin": "B012345678", "sku": "KNOWN", "state": "ENABLED"}]
        return [{"asin": "B000000001", "sku": "UNKNOWN", "state": "ENABLED"}]


def test_audit_missing_mapping_and_disabled_exploration():
    result = audit(FakeAmazon(), {
        "B012345678": {"asin": "B012345678", "sku": "KNOWN",
                        "price": 40, "cogs": 10, "fees": 5, "shipping": 10,
                        "updated_at": 100000, "market_at": 100000, "inventory": 100,
                        "inventory_at": 100000, "daily_units": 1, "velocity_at": 100000}
    }, Policy(daily_limit=75), now=100000)
    known = result["campaigns"][0]["products"][0]
    unknown = result["campaigns"][1]["products"][0]
    assert known["exploration_opt_in"] is False
    assert known["readiness"] == "insufficient_mature_conversion_data"
    assert unknown["verified_mapping"] is False
    assert unknown["readiness"] == "missing_verified_asin_economics"


def test_audit_is_read_only_and_never_guesses_other_skus():
    amazon = FakeAmazon()
    result = audit(amazon, {"B111111111": {"asin": "B111111111", "sku": "DIFFERENT"}},
                   Policy(daily_limit=75), now=100000)
    assert all(not x["verified_mapping"]
               for campaign in result["campaigns"] for x in campaign["products"])
