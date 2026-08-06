"""Cloud Run entrypoint with live dayparting bid controls.

Imports server.py first so all dashboard, campaign-launch, search-term, and optimizer
routes are registered, then adds the live bid-retune route that server.py expected.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional

from fastapi import Body, Header
from fastapi.responses import JSONResponse

import server
from server import app  # noqa: F401 - importing server registers the main app/routes
from optimize_campaigns import AmazonAdsClient, verify_internal_token
from budget_dayparting import budget_protection_status, get_budget_protection_mode


BASELINE_BIDS_FILE = Path(os.getenv("DAYPARTING_BASELINE_BIDS_FILE", "/tmp/dayparting_baseline_bids.json"))
MIN_BID = float(os.getenv("MIN_DAYPART_BID", "0.10"))
MAX_BID = float(os.getenv("MAX_DAYPART_BID", "2.50"))
PROTECT_MULTIPLIER = float(os.getenv("PROTECT_BID_MULTIPLIER", "0.35"))
TAPER_MULTIPLIER = float(os.getenv("TAPER_BID_MULTIPLIER", "0.45"))
PRIME_MULTIPLIER = float(os.getenv("PRIME_BID_MULTIPLIER", "1.15"))
ACOS_CEILING = float(os.getenv("ACOS_CIRCUIT_BREAKER_CEILING", "0.38"))
ACOS_MIN_SPEND = float(os.getenv("ACOS_CIRCUIT_BREAKER_MIN_SPEND", "20.0"))
ACOS_REDUCE_MULTIPLIER = float(os.getenv("ACOS_REDUCE_MULTIPLIER", "0.75"))
ACOS_SEVERE_MULTIPLIER = float(os.getenv("ACOS_SEVERE_MULTIPLIER", "0.50"))
ACOS_ZERO_SALES_MULTIPLIER = float(os.getenv("ACOS_ZERO_SALES_MULTIPLIER", "0.35"))


def _clamp_bid(value: float) -> float:
    return round(max(MIN_BID, min(MAX_BID, float(value))), 2)


def _acos_protected_bid(
    daypart_bid: float,
    suggested_bid: float,
    metrics: Dict[str, Any],
) -> tuple[float, bool, Optional[str]]:
    """Cap inefficient campaigns below Amazon's suggestion; never raise them."""
    spend = float(metrics.get("spend") or 0.0)
    sales = float(metrics.get("sales") or 0.0)
    if spend < ACOS_MIN_SPEND:
        return daypart_bid, False, None
    if sales <= 0:
        return min(daypart_bid, _clamp_bid(suggested_bid * ACOS_ZERO_SALES_MULTIPLIER)), True, "zero_sales"
    acos = spend / sales
    if acos <= ACOS_CEILING:
        return daypart_bid, False, None
    multiplier = ACOS_SEVERE_MULTIPLIER if acos >= (ACOS_CEILING * 2.0) else ACOS_REDUCE_MULTIPLIER
    return min(daypart_bid, _clamp_bid(suggested_bid * multiplier)), True, "acos_above_ceiling"

def _load_baseline_bids() -> Dict[str, float]:
    try:
        if BASELINE_BIDS_FILE.exists():
            data = json.loads(BASELINE_BIDS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items() if v not in (None, "")}
    except Exception:
        pass
    return {}


def _save_baseline_bids(data: Dict[str, float]) -> None:
    try:
        BASELINE_BIDS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        # Non-fatal: Cloud Run /tmp is best-effort. The route still returns its preview.
        pass


def _target_bid_from_baseline(base_bid: float, mode: str) -> float:
    if mode == "PROTECT":
        return _clamp_bid(base_bid * PROTECT_MULTIPLIER)
    if mode == "TAPER":
        return _clamp_bid(base_bid * TAPER_MULTIPLIER)
    return _clamp_bid(base_bid * PRIME_MULTIPLIER)


def _amazon_update_outcome(response: Dict[str, Any], submitted: int) -> tuple[int, int]:
    """Return per-item success/error counts from Amazon Ads v3 batch responses."""
    if submitted <= 0:
        return 0, 0
    body = response.get("adGroups", response) if isinstance(response, dict) else {}
    if not isinstance(body, dict):
        return 0, submitted
    successes = body.get("success")
    errors = body.get("error") or body.get("errors")
    success_count = len(successes) if isinstance(successes, list) else None
    error_count = len(errors) if isinstance(errors, list) else 0
    if success_count is None:
        success_count = max(0, submitted - error_count) if errors is not None else 0
    return min(submitted, success_count), min(submitted, error_count)
@app.post("/api/retune-existing-bids")
def api_retune_existing_bids(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Apply dayparting to active ad group default bids.

    - Before 10am ET: sets ad group bids to about 35% of their saved baseline.
    - 10am-8:59pm ET: restores/raises to prime-time pressure.
    - After 9pm ET: tapers to about 45% of baseline.

    The first live run saves the current ad group bids as the baseline so repeated
    scheduler runs do not keep compounding bids lower and lower.
    """
    verify_internal_token(authorization, x_daily_optimizer_token)
    apply_live = bool(payload.get("apply_live", True))
    reset_baseline = bool(payload.get("reset_baseline", False))
    max_results = int(payload.get("max_results", 100))
    max_results = max(1, min(max_results, 100))

    try:
        client = AmazonAdsClient()
        status = budget_protection_status()
        mode = get_budget_protection_mode()

        response = client.post(
            "/sp/adGroups/list",
            {"maxResults": max_results, "filters": {"stateFilter": {"include": ["ENABLED"]}}},
            content_type="application/vnd.spadgroup.v3+json",
            accept="application/vnd.spadgroup.v3+json",
        )
        ad_groups = response.get("adGroups", []) if isinstance(response, dict) else []
        baseline = {} if reset_baseline else _load_baseline_bids()

        _, campaign_metrics = server.optimizer_core._get_cached_dashboard_summary()
        campaign_metrics = campaign_metrics or {}
        if apply_live and not campaign_metrics:
            return JSONResponse({
                "error": True,
                "message": "Live retuning blocked: ACOS campaign metrics cache is unavailable.",
                "retryable": True,
            }, status_code=503)

        recommendations: Dict[str, Dict[str, Any]] = {}
        recommendation_errors = 0

        def fetch_recommendation(ad_group: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            ad_group_id = str(ad_group.get("adGroupId") or "")
            campaign_id = str(ad_group.get("campaignId") or "")
            return ad_group_id, client.get_ad_group_bid_recommendation(ad_group_id, campaign_id)

        with ThreadPoolExecutor(max_workers=min(5, max(1, len(ad_groups)))) as executor:
            futures = [executor.submit(fetch_recommendation, row) for row in ad_groups]
            for future in as_completed(futures):
                try:
                    ad_group_id, recommendation = future.result()
                    recommendations[ad_group_id] = recommendation or {}
                except Exception:
                    recommendation_errors += 1

        preview = []
        updates = []
        for ad_group in ad_groups:
            ad_group_id = str(ad_group.get("adGroupId") or "")
            if not ad_group_id:
                continue
            current_bid = float(ad_group.get("defaultBid") or payload.get("fallback_bid", 0.75))
            if reset_baseline or ad_group_id not in baseline:
                baseline[ad_group_id] = current_bid
            recommendation = recommendations.get(ad_group_id, {})
            suggested_bid = float(recommendation.get("suggested") or 0.0)
            base_bid = suggested_bid or float(baseline.get(ad_group_id) or current_bid)
            bid_source = "amazon_suggested_bid" if suggested_bid else "baseline_fallback"
            daypart_bid = _target_bid_from_baseline(base_bid, mode)
            campaign_id = str(ad_group.get("campaignId") or "")
            metrics = campaign_metrics.get(campaign_id, {})
            new_bid, circuit_breaker_active, adjustment_reason = _acos_protected_bid(
                daypart_bid, suggested_bid, metrics
            ) if suggested_bid else (daypart_bid, False, None)
            spend = float(metrics.get("spend") or 0.0)
            sales = float(metrics.get("sales") or 0.0)
            acos = (spend / sales) if sales > 0 else None
            row = {
                "adGroupId": ad_group_id,
                "campaignId": campaign_id,
                "currentBid": round(current_bid, 2),
                "baselineBid": round(base_bid, 2),
                "bidSource": bid_source,
                "amazonSuggestedBid": round(suggested_bid, 2) if suggested_bid else None,
                "daypartBid": daypart_bid,
                "newBid": new_bid,
                "mode": mode,
                "spend": round(spend, 2),
                "sales": round(sales, 2),
                "acos": round(acos, 4) if acos is not None else None,
                "acosCircuitBreaker": circuit_breaker_active,
                "adjustmentReason": adjustment_reason,
            }
            preview.append(row)
            if bid_source == "amazon_suggested_bid" and abs(new_bid - current_bid) >= 0.01:
                update_row = {"adGroupId": ad_group_id, "defaultBid": new_bid}
                if campaign_id:
                    update_row["campaignId"] = campaign_id
                updates.append(update_row)

        _save_baseline_bids(baseline)

        api_response: Dict[str, Any] = {}
        if apply_live and updates:
            api_response = client.put(
                "/sp/adGroups",
                {"adGroups": updates},
                content_type="application/vnd.spadgroup.v3+json",
                accept="application/vnd.spadgroup.v3+json",
            )

        applied_count, update_error_count = _amazon_update_outcome(api_response, len(updates)) if apply_live else (0, 0)
        return JSONResponse({
            "success": True,
            "dry_run": not apply_live,
            "mode": mode,
            "budget_protection": status,
            "ad_groups_seen": len(ad_groups),
            "updates_needed": len(updates),
            "updates_applied": applied_count,
            "update_errors": update_error_count,
            "acos_circuit_breakers": sum(1 for row in preview if row.get("acosCircuitBreaker")),
            "acos_ceiling": ACOS_CEILING,
            "acos_min_spend": ACOS_MIN_SPEND,
            "baseline_count": len(baseline),
            "metrics_available": bool(campaign_metrics),
            "recommendation_errors": recommendation_errors,
            "reset_baseline": reset_baseline,
            "preview": preview[:25],
            "amazon_response": api_response,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
