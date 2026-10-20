"""Read-only diagnostic: campaign ASIN mapping, economic readiness, exploration.

Does not mutate Amazon bids, profit-data, or policy. No unit cost data is printed.
"""
import json
import time
from automation_store import GCSState
from optimize_campaigns import AmazonAdsClient
from profit_intelligence import Policy, cpc_ceiling, mature_rows


def audit(client, products, policy=None, metrics=None, now=None):
    now = time.time() if now is None else now
    metrics = metrics or []
    results = []
    for campaign in client.list_campaigns():
        cid = str(campaign["campaignId"])
        rows = [row for row in client.list_product_ads(cid)
                if row.get("state") != "ARCHIVED"]
        items = []
        for ad in rows:
            asin, sku = str(ad.get("asin") or ""), str(ad.get("sku") or "")
            # A SKU match is a clue, not verification of a different ASIN.
            product = products.get(asin) if asin else None
            exact = bool(product and product.get("asin") == asin
                         and product.get("sku") == sku)
            matches = [p for p in products.values() if sku and p.get("sku") == sku]
            entry = {"asin": asin or None, "sku": sku or None,
                     "verified_mapping": exact,
                     "sku_conflict": bool(matches and not exact)}
            if not exact:
                product = None
            if not product:
                entry["readiness"] = "missing_verified_asin_economics"
            elif policy is None:
                entry["readiness"] = "missing_profit_policy"
            else:
                entry["exploration_opt_in"] = (
                    product["asin"] in policy.product_exploration
                    or policy.exploration_cvr > 0
                )
                try:
                    eligible = mature_rows({"metrics": metrics}, policy, now,
                                           grain="campaign", campaign_id=cid)
                    ceiling = cpc_ceiling(product, eligible, policy, now)
                    entry["ceiling_source"] = ceiling["source"]
                    entry["max_cpc"] = ceiling["max_cpc"]
                    entry["readiness"] = ("eligible" if ceiling["max_cpc"] >= policy.min_cpc
                                          else "no_safe_bid_allowance")
                except ValueError as exc:
                    # Restrict output to known non-secret diagnostic categories.
                    msg = str(exc)
                    permitted = {"insufficient_mature_conversion_data",
                                 "stale_economics", "stale_price_or_fees",
                                 "missing_or_stale_inventory",
                                 "missing_or_stale_sales_velocity"}
                    entry["readiness"] = msg if msg in permitted else "economic_validation_failed"
            items.append(entry)
        if items:
            results.append({"campaign_id": cid, "products": items})
    return {"read_only": True, "campaigns": results}


if __name__ == "__main__":
    client = AmazonAdsClient()
    state = GCSState("profit-data").read()
    raw = GCSState("profit-policy").read().get("policy")
    policy = Policy.model_validate(raw) if raw else None
    print(json.dumps(audit(client, state.get("products", {}), policy,
                           state.get("metrics", [])), indent=2))
