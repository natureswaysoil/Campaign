"""Final Cloud Run entrypoint.

Imports the extended server and adds the missing campaign pause/resume endpoint
used by the dashboard Pause and Resume buttons.
"""
from typing import Any, Dict, Optional

from fastapi import Body, Header
from fastapi.responses import JSONResponse

import server_with_bids  # noqa: F401 - registers live bid controls
import extended_server  # noqa: F401 - registers routes and dashboard patch
from extended_server import app
from optimize_campaigns import AmazonAdsClient, load_products, verify_internal_token
from ppc_agent import AmazonPpcAgent


@app.post("/api/update-campaign-state")
def api_update_campaign_state(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Pause or resume one Sponsored Products campaign from the dashboard."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        campaign_id = str(payload.get("campaign_id") or payload.get("campaignId") or "").strip()
        state = str(payload.get("state") or "").strip().upper()

        if not campaign_id:
            return JSONResponse({"error": True, "message": "campaign_id required"}, status_code=400)
        if state not in {"ENABLED", "PAUSED"}:
            return JSONResponse({"error": True, "message": "state must be ENABLED or PAUSED"}, status_code=400)

        client = AmazonAdsClient()
        result = client.put(
            "/sp/campaigns",
            {"campaigns": [{"campaignId": campaign_id, "state": state}]},
            content_type="application/vnd.spcampaign.v3+json",
            accept="application/vnd.spcampaign.v3+json",
        )
        return JSONResponse({
            "success": True,
            "campaign_id": campaign_id,
            "state": state,
            "amazon_response": result,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)

@app.post("/api/harvest-all-discovery")
def api_harvest_all_discovery(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Harvest all product campaign pairs from one shared Amazon search-term report."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    apply_live = bool(payload.get("apply_live", False))
    lookback_days = max(1, min(60, int(payload.get("lookback_days", 14))))
    max_terms = max(1, min(100, int(payload.get("max_terms_per_product", 10))))
    max_products = max(1, min(100, int(payload.get("max_products", 25))))
    client = AmazonAdsClient()
    try:
        rows, report_id, start_date, end_date = extended_server._search_term_rows(client, lookback_days)
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=504)

    results = []
    seen = set()
    for raw_product in load_products():
        key = str(raw_product.get("Product_ID") or raw_product.get("SKU") or "").strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        product = extended_server.normalized_product(raw_product)
        existing = extended_server._find_existing_launch_campaigns(client, extended_server._safe_title(product))
        discovery = existing.get("AUTO_DISCOVERY")
        exact = existing.get("MANUAL_EXACT")
        if not discovery or not exact:
            results.append({"product_id": key, "skipped": True, "reason": "campaign_pair_not_found"})
        else:
            discovery_id = str(discovery.get("campaignId") or "")
            exact_id = str(exact.get("campaignId") or "")
            exact_ad_group_id = extended_server._first_ad_group_id(client, exact_id)
            if not exact_ad_group_id:
                results.append({"product_id": key, "skipped": True, "reason": "enabled_exact_ad_group_not_found"})
            else:
                classified = extended_server.classify_search_terms([
                    row for row in rows if str(row.get("campaignId") or "") == discovery_id
                ])
                winners = sorted(
                    classified.get("winners", []),
                    key=lambda item: (float(item.get("sales") or 0), -float(item.get("acos") or 9)),
                    reverse=True,
                )
                existing_terms = {
                    extended_server.base._normalize_keyword(keyword.get("keywordText"))
                    for keyword in client.list_keywords(exact_id)
                    if str(keyword.get("matchType") or "").upper() == "EXACT"
                }
                selected = []
                for item in winners:
                    term = extended_server.base._normalize_keyword(item.get("term"))
                    if term and term not in existing_terms:
                        selected.append(term)
                        existing_terms.add(term)
                    if len(selected) >= max_terms:
                        break
                fallback_bid = float(payload.get("winner_bid", product.get("suggested_bid") or 0.75))
                _, _, protected_bid = extended_server.choose_budget_protected_bid({}, fallback_bid)
                exact_bid = round(max(0.10, protected_bid * 1.15), 2)
                keyword_rows = extended_server.base._exact_keyword_rows(selected, exact_id, exact_ad_group_id, exact_bid)
                created = 0
                if apply_live and keyword_rows:
                    client.create_keywords(keyword_rows)
                    created = len(keyword_rows)
                results.append({
                    "product_id": key,
                    "success": True,
                    "discovery_campaign_id": discovery_id,
                    "exact_campaign_id": exact_id,
                    "rows_analyzed": sum(1 for row in rows if str(row.get("campaignId") or "") == discovery_id),
                    "winners_found": len(winners),
                    "terms_selected": len(selected),
                    "keywords_created": created,
                    "terms_harvested": selected,
                })
        if len(results) >= max_products:
            break
    return JSONResponse({
        "success": True,
        "apply_live": apply_live,
        "report_id": report_id,
        "date_range": {"start": start_date, "end": end_date},
        "report_rows": len(rows),
        "products_checked": len(results),
        "products_with_campaign_pairs": sum(1 for item in results if item.get("success")),
        "terms_selected": sum(int(item.get("terms_selected") or 0) for item in results),
        "keywords_created": sum(int(item.get("keywords_created") or 0) for item in results),
        "results": results,
    })
@app.post("/api/refresh-dashboard-cache")
def api_refresh_dashboard_cache(
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    core = extended_server.base.optimizer_core
    summary, campaigns = core._get_cached_dashboard_summary()
    refreshing = bool(core._dash_summary_cache.get("refreshing"))
    return JSONResponse({
        "success": True,
        "refreshing": refreshing,
        "cache_ready": summary is not None,
        "summary": summary,
        "campaigns_cached": len(campaigns or {}),
        "refreshed_at_epoch": core._dash_summary_cache.get("ts"),
    }, status_code=202 if refreshing else 200)


@app.post("/api/acos-circuit-breaker-v2")
def api_acos_circuit_breaker_v2(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Audit 14-day campaign ACOS; live mutation stays disabled in this stack."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    apply_live = bool(payload.get("apply_live", False))
    if apply_live:
        return JSONResponse({
            "error": True,
            "message": "Live ACOS bid flooring is not enabled; use dry-run review.",
        }, status_code=409)
    min_spend = float(payload.get("min_spend", 20.0))
    ceiling = float(payload.get("acos_ceiling", 0.38))
    core = extended_server.base.optimizer_core
    summary, campaigns = core._get_cached_dashboard_summary()
    candidates = []
    for campaign_id, metrics in (campaigns or {}).items():
        spend = float(metrics.get("spend") or 0)
        sales = float(metrics.get("sales") or 0)
        acos = (spend / sales) if sales > 0 else None
        if spend >= min_spend and (acos is None or acos > ceiling):
            candidates.append({
                "campaign_id": campaign_id,
                "spend": round(spend, 2),
                "sales": round(sales, 2),
                "acos": round(acos, 4) if acos is not None else None,
                "ceiling": ceiling,
                "reason": "zero_sales" if sales <= 0 else "acos_above_ceiling",
            })
    return JSONResponse({
        "success": True,
        "apply_live": False,
        "cache_ready": summary is not None,
        "campaigns_checked": len(campaigns or {}),
        "candidates_count": len(candidates),
        "candidates": candidates,
    })

@app.get("/api/agent/ppc/status")
def api_ppc_agent_status() -> Dict[str, Any]:
    return {
        "agent": "amazon-ppc-optimizer",
        "ready": True,
        "default_mode": "DRY_RUN",
        "actions": ["refresh_dashboard", "audit_acos", "retune_bids", "harvest_keywords", "launch_campaign"],
        "live_launch_guard": "allow_campaign_launch=true and product_id required",
    }


@app.post("/api/agent/ppc/run")
def api_run_ppc_agent(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)

    def call_refresh(_: Dict[str, Any]) -> JSONResponse:
        return api_refresh_dashboard_cache(authorization, x_daily_optimizer_token)

    tools = {
        "refresh_dashboard": call_refresh,
        "audit_acos": lambda body: api_acos_circuit_breaker_v2(body, authorization, x_daily_optimizer_token),
        "retune_bids": lambda body: server_with_bids.api_retune_existing_bids(body, authorization, x_daily_optimizer_token),
        "harvest_keywords": lambda body: api_harvest_all_discovery(body, authorization, x_daily_optimizer_token),
        "launch_campaign": lambda body: extended_server.api_create_campaign_with_duplicate_protection(body, authorization, x_daily_optimizer_token),
    }
    return JSONResponse(AmazonPpcAgent(tools).run(payload))
