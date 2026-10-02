"""Daily sales-growth decision engine for Amazon Sponsored Products.

Classifies each product as SCALE, HOLD, FIX_LISTING, PROMOTE, or RESTOCK.
Only SCALE may change budgets automatically. All other actions are recommendations.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse

from amazon_results import batch_outcome
from automation_store import GCSState, StateBusy
from optimize_campaigns import AmazonAdsClient, load_products, normalized_product
from safety import live_requested
from scheduled_harvest import eastern_day
import extended_server

SCALE_MIN_ORDERS = int(os.getenv("GROWTH_SCALE_MIN_ORDERS", "3"))
SCALE_MIN_SALES = float(os.getenv("GROWTH_SCALE_MIN_SALES", "60"))
SCALE_MIN_CVR = float(os.getenv("GROWTH_SCALE_MIN_CVR", "0.10"))
SCALE_MAX_ACOS = float(os.getenv("GROWTH_SCALE_MAX_ACOS", "0.35"))
PROMOTE_MIN_CLICKS = int(os.getenv("GROWTH_PROMOTE_MIN_CLICKS", "6"))
PROMOTE_MAX_ACOS = float(os.getenv("GROWTH_PROMOTE_MAX_ACOS", "0.50"))
FIX_MIN_CLICKS = int(os.getenv("GROWTH_FIX_MIN_CLICKS", "15"))
FIX_MAX_CVR = float(os.getenv("GROWTH_FIX_MAX_CVR", "0.05"))
LOW_INVENTORY_UNITS = int(os.getenv("GROWTH_LOW_INVENTORY_UNITS", "10"))
PROFIT_RESERVE = float(os.getenv("GROWTH_PROFIT_RESERVE", "0.10"))
DEFAULT_GROSS_MARGIN = float(os.getenv("GROWTH_DEFAULT_GROSS_MARGIN", "0.40"))
SCALE_MULTIPLIER = float(os.getenv("GROWTH_SCALE_MULTIPLIER", "1.15"))
MAX_BUDGET_STEP = float(os.getenv("GROWTH_MAX_BUDGET_STEP", "10"))
MAX_DAILY_BUDGET = float(os.getenv("GROWTH_MAX_DAILY_BUDGET", "50"))
MAX_AUTO_SCALES_PER_DAY = int(os.getenv("GROWTH_MAX_AUTO_SCALES_PER_DAY", "3"))


def _money(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        return default


def _optional_money(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(
            str(value).replace("$", "").replace(",", "").replace("%", "").strip()
        )
    except Exception:
        return None


def _first(row: Dict[str, Any], *keys: str) -> str:
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            value = lower.get(key.lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def product_economics(raw: Dict[str, Any]) -> Dict[str, Any]:
    price_raw = _first(raw, "Price", "Amazon_Price", "Amazon Price", "Sale Price", "Selling Price")
    cogs_raw = _first(raw, "COGS", "Product_Cost", "Product Cost", "Cost_of_Goods", "Cost of Goods")
    fees_raw = _first(raw, "Amazon_Fees", "Amazon Fees", "FBA_Fees", "FBA Fees")
    shipping_raw = _first(raw, "Shipping_Cost", "Shipping Cost", "Fulfillment_Cost", "Fulfillment Cost")
    margin_raw = _first(raw, "Gross_Margin", "Gross Margin", "Product_Margin", "Product Margin")

    price_value = _optional_money(price_raw)
    cogs_value = _optional_money(cogs_raw)
    fees_value = _optional_money(fees_raw)
    shipping_value = _optional_money(shipping_raw)
    explicit_margin_value = _optional_money(margin_raw)

    price = price_value or 0.0
    cogs = cogs_value or 0.0
    amazon_fees = fees_value or 0.0
    shipping = shipping_value or 0.0
    explicit_margin = explicit_margin_value if explicit_margin_value is not None else -1.0
    if explicit_margin > 1:
        explicit_margin /= 100.0

    complete_costs = all(
        value is not None
        for value in (price_value, cogs_value, fees_value, shipping_value)
    )
    known_costs = cogs + amazon_fees + shipping
    if complete_costs and price > 0:
        gross_margin = max(0.0, min(0.95, (price - known_costs) / price))
        source = "product_costs"
    elif explicit_margin >= 0:
        gross_margin = max(0.0, min(0.95, explicit_margin))
        source = "product_margin"
    else:
        gross_margin = DEFAULT_GROSS_MARGIN
        source = "default_margin"

    break_even_acos = gross_margin
    profitable_acos = max(0.05, min(SCALE_MAX_ACOS, break_even_acos - PROFIT_RESERVE))
    return {
        "price": round(price, 2),
        "cogs": round(cogs, 2),
        "amazon_fees": round(amazon_fees, 2),
        "shipping": round(shipping, 2),
        "gross_margin": round(gross_margin, 4),
        "break_even_acos": round(break_even_acos, 4),
        "profitable_acos": round(profitable_acos, 4),
        "economics_source": source,
    }


def _inventory_units(raw: Dict[str, Any]) -> Optional[int]:
    value = _first(raw, "Inventory", "Inventory_Units", "Inventory Units", "Units_Available", "Units Available")
    if not value:
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def classify_product(
    product: Dict[str, Any],
    metrics: Dict[str, Any],
    economics: Dict[str, Any],
    inventory_units: Optional[int] = None,
) -> Dict[str, Any]:
    spend = float(metrics.get("spend") or 0)
    sales = float(metrics.get("sales") or 0)
    clicks = int(metrics.get("clicks") or 0)
    orders = int(metrics.get("orders") or 0)
    impressions = int(metrics.get("impressions") or 0)
    acos = (spend / sales) if sales > 0 else None
    cvr = (orders / clicks) if clicks else 0.0
    estimated_profit_after_ads = sales * float(economics["gross_margin"]) - spend

    if inventory_units is not None and inventory_units <= LOW_INVENTORY_UNITS:
        decision = "RESTOCK"
        reason = f"inventory_at_or_below_{LOW_INVENTORY_UNITS}"
    elif clicks >= FIX_MIN_CLICKS and (orders == 0 or cvr < FIX_MAX_CVR):
        decision = "FIX_LISTING"
        reason = "traffic_not_converting"
    elif (
        orders >= SCALE_MIN_ORDERS
        and sales >= SCALE_MIN_SALES
        and acos is not None
        and acos <= float(economics["profitable_acos"])
        and cvr >= SCALE_MIN_CVR
        and estimated_profit_after_ads > 0
    ):
        decision = "SCALE"
        reason = "proven_profitable_demand"
    elif (
        clicks >= PROMOTE_MIN_CLICKS
        and orders >= 1
        and acos is not None
        and acos <= PROMOTE_MAX_ACOS
        and estimated_profit_after_ads > 0
    ):
        decision = "PROMOTE"
        reason = "promising_but_not_scale_ready"
    else:
        decision = "HOLD"
        reason = "insufficient_evidence_or_margin"

    return {
        "product_id": product.get("product_id"),
        "title": product.get("title"),
        "sku": product.get("sku"),
        "asin": product.get("asin"),
        "decision": decision,
        "reason": reason,
        "metrics": {
            "spend": round(spend, 2),
            "sales": round(sales, 2),
            "clicks": clicks,
            "orders": orders,
            "impressions": impressions,
            "acos": round(acos, 4) if acos is not None else None,
            "conversion_rate": round(cvr, 4),
            "estimated_profit_after_ads": round(estimated_profit_after_ads, 2),
        },
        "economics": economics,
        "inventory_units": inventory_units,
    }


def _campaign_budget(campaign: Dict[str, Any]) -> float:
    budget = campaign.get("budget")
    if isinstance(budget, dict):
        return _money(budget.get("budget"), 0.0)
    return _money(campaign.get("dailyBudget") or campaign.get("daily_budget"), 0.0)


def _set_budget_verified(client: AmazonAdsClient, campaign: Dict[str, Any], new_budget: float) -> Dict[str, Any]:
    campaign_id = str(campaign.get("campaignId") or "")
    payload = {
        "campaigns": [{
            "campaignId": campaign_id,
            "budget": {"budget": round(new_budget, 2), "budgetType": "DAILY"},
        }]
    }
    response = client.put(
        "/sp/campaigns",
        payload,
        content_type="application/vnd.spcampaign.v3+json",
        accept="application/vnd.spcampaign.v3+json",
    )
    outcome = batch_outcome(response, "campaigns", 1)
    if not outcome.get("success"):
        raise RuntimeError(f"Amazon did not acknowledge budget update: {outcome}")

    refreshed = next(
        (c for c in client.list_campaigns() if str(c.get("campaignId") or "") == campaign_id),
        None,
    )
    if refreshed is None:
        raise RuntimeError("Budget update could not be verified because campaign was not returned")
    actual = _campaign_budget(refreshed)
    if abs(actual - round(new_budget, 2)) > 0.01:
        raise RuntimeError(f"Budget verification failed: expected {new_budget:.2f}, got {actual:.2f}")
    return {
        "campaign_id": campaign_id,
        "old_budget": round(_campaign_budget(campaign), 2),
        "new_budget": round(actual, 2),
    }


class GrowthMetricsNotReady(RuntimeError):
    pass


def evaluate_products(client: AmazonAdsClient, require_fresh: bool = False) -> List[Dict[str, Any]]:
    core = extended_server.base.optimizer_core
    _, per_campaign = core._get_cached_dashboard_summary()
    per_campaign = per_campaign or {}
    cache = core._dash_summary_cache
    metrics_fresh = (
        bool(per_campaign)
        and 0 <= time.time() - float(cache.get("ts") or 0) < core._DASH_SUMMARY_TTL
    )
    if require_fresh and not metrics_fresh:
        raise GrowthMetricsNotReady("fresh campaign metrics are not available yet")
    all_campaigns = client.list_campaigns()

    results: List[Dict[str, Any]] = []
    for raw in load_products():
        product = normalized_product(raw)
        title = extended_server._safe_title(product)
        prefix = title + " |"
        related = [
            c for c in all_campaigns
            if str(c.get("state") or "").upper() != "ARCHIVED"
            and str(c.get("name") or "").startswith(prefix)
        ]
        metrics = {"spend": 0.0, "sales": 0.0, "clicks": 0, "orders": 0, "impressions": 0}
        for campaign in related:
            row = per_campaign.get(str(campaign.get("campaignId") or "")) or {}
            for key in metrics:
                metrics[key] += float(row.get(key) or 0)
        metrics["clicks"] = int(metrics["clicks"])
        metrics["orders"] = int(metrics["orders"])
        metrics["impressions"] = int(metrics["impressions"])

        item = classify_product(
            product,
            metrics,
            product_economics(raw),
            _inventory_units(raw),
        )
        item["campaigns"] = related
        results.append(item)
    return results


def run_growth(
    client: AmazonAdsClient,
    state: Dict[str, Any],
    live: bool,
    persist_state=None,
) -> Dict[str, Any]:
    evaluated = evaluate_products(client, require_fresh=live)
    recommendations = state.setdefault("recommendations", {})
    scaled = state.setdefault("scaled", {})
    scale_results: List[Dict[str, Any]] = []
    today = eastern_day().isoformat()
    daily_counts = state.setdefault("daily_scale_counts", {})
    reservations = state.setdefault("scale_reservations", {})
    auto_count = int(daily_counts.get(today) or 0)

    for item in evaluated:
        key = str(item.get("product_id") or item.get("sku") or item.get("asin") or item.get("title"))
        decision = item["decision"]

        if decision != "SCALE":
            recommendations[key] = {**item, "queued_at": recommendations.get(key, {}).get("queued_at", time.time())}
            continue

        reservation_key = f"{today}:{key}"
        if reservation_key in reservations:
            recommendations[key] = {**item, "reason": "scale_reserved_or_completed_today"}
            continue

        if auto_count >= MAX_AUTO_SCALES_PER_DAY:
            recommendations[key] = {**item, "reason": "daily_auto_scale_limit"}
            continue

        campaigns = [
            c for c in item.get("campaigns", [])
            if str(c.get("state") or "").upper() == "ENABLED"
            and "MANUAL EXACT" in str(c.get("name") or "").upper()
        ]
        if not campaigns:
            recommendations[key] = {**item, "reason": "no_enabled_manual_exact_campaign"}
            continue

        campaign = campaigns[0]
        old_budget = _campaign_budget(campaign)
        if old_budget <= 0:
            recommendations[key] = {**item, "reason": "campaign_budget_unavailable"}
            continue
        new_budget = min(MAX_DAILY_BUDGET, old_budget + MAX_BUDGET_STEP, old_budget * SCALE_MULTIPLIER)
        new_budget = round(new_budget, 2)
        if new_budget <= old_budget:
            recommendations[key] = {**item, "reason": "budget_already_at_cap"}
            continue

        if not live:
            scale_results.append({"product_id": key, "preview": True, "old_budget": old_budget, "new_budget": new_budget})
            continue

        reservations[reservation_key] = {
            "product_id": key,
            "campaign_id": str(campaign.get("campaignId") or ""),
            "old_budget": round(old_budget, 2),
            "new_budget": new_budget,
            "reserved_at": time.time(),
        }
        auto_count += 1
        daily_counts[today] = auto_count
        if persist_state:
            persist_state(state)

        try:
            change = _set_budget_verified(client, campaign, new_budget)
        except Exception as exc:
            recommendations[key] = {
                **item,
                "reason": "auto_scale_failed_or_unverified",
                "scale_error": str(exc),
                "reservation": reservations[reservation_key],
            }
            if persist_state:
                persist_state(state)
            continue

        reservations[reservation_key]["status"] = "verified"
        reservations[reservation_key]["verified_at"] = time.time()
        scaled[key] = {
            "product": item,
            "change": change,
            "scaled_at": time.time(),
            "source": "automatic",
        }
        recommendations.pop(key, None)
        scale_results.append({"product_id": key, **change})
        if persist_state:
            persist_state(state)

    return {
        "success": True,
        "live": live,
        "evaluated": len(evaluated),
        "counts": {decision: sum(1 for x in evaluated if x["decision"] == decision)
                   for decision in ("SCALE", "HOLD", "FIX_LISTING", "PROMOTE", "RESTOCK")},
        "auto_scales": scale_results,
        "auto_scales_today": auto_count,
        "recommendations": list(recommendations.values()),
        "products": [{k: v for k, v in x.items() if k != "campaigns"} for x in evaluated],
        "rules": {
            "scale_min_orders": SCALE_MIN_ORDERS,
            "scale_min_sales": SCALE_MIN_SALES,
            "scale_min_conversion_rate": SCALE_MIN_CVR,
            "scale_max_acos": SCALE_MAX_ACOS,
            "profit_reserve": PROFIT_RESERVE,
            "scale_multiplier": SCALE_MULTIPLIER,
            "max_budget_step": MAX_BUDGET_STEP,
            "max_daily_budget": MAX_DAILY_BUDGET,
            "max_auto_scales_per_day": MAX_AUTO_SCALES_PER_DAY,
        },
    }


def growth_tick(payload: Dict[str, Any]) -> JSONResponse:
    if not live_requested(payload):
        return JSONResponse({
            "success": True,
            "dry_run": True,
            "message": "Sales growth preview; no budgets changed",
        })
    try:
        store = GCSState("sales-growth-workflow")
        with store.locked() as state:
            today = eastern_day().isoformat()
            if state.get("completed_day") == today:
                return JSONResponse({
                    "success": True,
                    "status": "already_completed",
                    "last_result": state.get("last_result"),
                })
            result = run_growth(
                AmazonAdsClient(), state, live=True, persist_state=store.save
            )
            state["last_result"] = result
            state["last_run_at"] = time.time()
            state["completed_day"] = today
            store.save(state)
            return JSONResponse(result)
    except GrowthMetricsNotReady as exc:
        return JSONResponse({
            "success": True,
            "status": "waiting_for_fresh_metrics",
            "message": str(exc),
        }, status_code=202)
    except StateBusy as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=503)


def get_growth_state() -> Dict[str, Any]:
    state = GCSState("sales-growth-workflow").read()
    return {
        "success": True,
        "recommendations": list((state.get("recommendations") or {}).values()),
        "scaled": list((state.get("scaled") or {}).values()),
        "last_result": state.get("last_result"),
    }
