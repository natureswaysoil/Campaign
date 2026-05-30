"""Extended Cloud Run entrypoint.

This wraps server.py and overrides only the launch route so duplicate launches are
blocked. It also adds a harvest endpoint that promotes proven AUTO DISCOVERY
search terms into the matching MANUAL EXACT campaign.
"""
import datetime
import hmac
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, Header
from fastapi.responses import JSONResponse

import server as base
from server import app
from optimize_campaigns import AmazonAdsClient, DEFAULT_FALLBACK_BID, generate_keywords_for_product, load_products, normalized_product, parse_report_json_bytes, verify_internal_token
from budget_dayparting import budget_protection_status, choose_budget_protected_bid
from ppc_waste_rules import classify_search_terms, summarize_classification


def _remove_route(path: str, method: str) -> None:
    app.router.routes = [
        route for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and method.upper() in set(getattr(route, "methods", set()) or set())
        )
    ]


_remove_route("/api/create-campaign-from-product", "POST")


def _optional_dashboard_auth(authorization: Optional[str], x_daily_optimizer_token: Optional[str]) -> Optional[JSONResponse]:
    token = os.getenv("DAILY_OPTIMIZER_TOKEN", "")
    if not token:
        return None
    supplied = None
    if x_daily_optimizer_token:
        supplied = x_daily_optimizer_token.strip()
    elif authorization and authorization.startswith("Bearer "):
        supplied = authorization.replace("Bearer ", "", 1).strip()
    if supplied and not hmac.compare_digest(supplied, token):
        return JSONResponse({"error": True, "message": "Invalid token"}, status_code=403)
    return None


def _product_from_key(key: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    key = key.lower().strip()
    for row in load_products():
        if row.get("Product_ID", "").lower() == key or row.get("SKU", "").lower() == key:
            return normalized_product(row), row
    return None, {}


def _safe_title(product: Dict[str, Any]) -> str:
    return base._sanitize_name(str(product.get("title") or "Product"))[:70]


def _find_existing_launch_campaigns(client: AmazonAdsClient, safe_title: str) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    prefix = f"{safe_title} | "
    for campaign in client.list_campaigns():
        name = str(campaign.get("name") or "")
        if not name.startswith(prefix):
            continue
        if "| AUTO DISCOVERY |" in name and "AUTO_DISCOVERY" not in found:
            found["AUTO_DISCOVERY"] = campaign
        if "| MANUAL EXACT |" in name and "MANUAL_EXACT" not in found:
            found["MANUAL_EXACT"] = campaign
    return found


def _list_ad_groups(client: AmazonAdsClient, campaign_id: str) -> List[Dict[str, Any]]:
    data = client.post(
        "/sp/adGroups/list",
        {
            "maxResults": 100,
            "filters": {
                "campaignIdFilter": {"include": [str(campaign_id)]},
                "stateFilter": {"include": ["ENABLED"]},
            },
        },
        content_type="application/vnd.spadgroup.v3+json",
        accept="application/vnd.spadgroup.v3+json",
    )
    return data.get("adGroups", []) if isinstance(data, dict) else []


def _first_ad_group_id(client: AmazonAdsClient, campaign_id: str) -> Optional[str]:
    for ad_group in _list_ad_groups(client, campaign_id):
        if ad_group.get("adGroupId"):
            return str(ad_group["adGroupId"])
    return None


def _search_term_rows(client: AmazonAdsClient, lookback_days: int) -> Tuple[List[Dict[str, Any]], str, str, str]:
    start_date = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    end_date = datetime.date.today().isoformat()
    report_id = client.request_report({
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["searchTerm"],
            "columns": ["campaignId", "adGroupId", "searchTerm", "clicks", "cost", "sales7d", "purchases7d"],
            "reportTypeId": "spSearchTerm",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    })
    report_url = client.wait_for_report(report_id)
    return parse_report_json_bytes(client.download_binary(report_url)), report_id, start_date, end_date


@app.post("/api/create-campaign-from-product")
def api_create_campaign_with_duplicate_protection(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Launch using server.py logic, but block duplicate product launches first."""
    auth_error = _optional_dashboard_auth(authorization, x_daily_optimizer_token)
    if auth_error:
        return auth_error
    try:
        key = (payload.get("product_id") or payload.get("sku") or "").lower().strip()
        if not key:
            return JSONResponse({"error": True, "message": "product_id or sku required"}, status_code=400)
        product, _ = _product_from_key(key)
        if not product:
            return JSONResponse({"error": True, "message": "Product not found"}, status_code=404)

        client = AmazonAdsClient()
        safe_title = _safe_title(product)
        existing = _find_existing_launch_campaigns(client, safe_title)
        force_relaunch = bool(payload.get("force_relaunch", False))
        if existing and not force_relaunch:
            return JSONResponse({
                "success": True,
                "duplicate_launch_prevented": True,
                "message": "Matching launch campaigns already exist. No new campaigns were created. Use force_relaunch=true only when you intentionally want duplicates.",
                "product": product.get("title"),
                "existing_campaigns": {
                    campaign_type: {
                        "campaign_id": str(campaign.get("campaignId") or ""),
                        "name": campaign.get("name"),
                        "state": campaign.get("state"),
                    }
                    for campaign_type, campaign in existing.items()
                },
            })

        # Reuse the tested launcher in server.py after duplicate protection passes.
        return base.api_create_recommended_campaigns(payload, authorization, x_daily_optimizer_token)
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/harvest-discovery-winners")
def api_harvest_discovery_winners(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Promote proven AUTO DISCOVERY search terms into MANUAL EXACT."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        key = (payload.get("product_id") or payload.get("sku") or "").lower().strip()
        if not key:
            return JSONResponse({"error": True, "message": "product_id or sku required"}, status_code=400)
        product, _ = _product_from_key(key)
        if not product:
            return JSONResponse({"error": True, "message": "Product not found"}, status_code=404)

        lookback_days = max(1, min(60, int(payload.get("lookback_days", 14))))
        max_terms = max(1, min(100, int(payload.get("max_terms", 25))))
        apply_live = bool(payload.get("apply_live", True))
        fallback_bid = float(payload.get("winner_bid", product.get("suggested_bid") or DEFAULT_FALLBACK_BID))
        _, _, protected_bid = choose_budget_protected_bid({}, fallback_bid)
        exact_bid = round(max(0.10, protected_bid * 1.15), 2)

        client = AmazonAdsClient()
        existing = _find_existing_launch_campaigns(client, _safe_title(product))
        discovery_campaign = existing.get("AUTO_DISCOVERY")
        exact_campaign = existing.get("MANUAL_EXACT")
        if not discovery_campaign or not exact_campaign:
            return JSONResponse({
                "error": True,
                "message": "Could not find both AUTO DISCOVERY and MANUAL EXACT campaigns for this product.",
                "found_campaigns": list(existing.keys()),
            }, status_code=404)

        discovery_campaign_id = str(discovery_campaign.get("campaignId") or "")
        exact_campaign_id = str(exact_campaign.get("campaignId") or "")
        exact_ad_group_id = _first_ad_group_id(client, exact_campaign_id)
        if not exact_ad_group_id:
            return JSONResponse({"error": True, "message": "MANUAL EXACT campaign has no enabled ad group."}, status_code=404)

        rows, report_id, start_date, end_date = _search_term_rows(client, lookback_days)
        discovery_rows = [row for row in rows if str(row.get("campaignId") or "") == discovery_campaign_id]
        classified = classify_search_terms(discovery_rows)
        winners = sorted(
            classified.get("winners", []),
            key=lambda item: (float(item.get("sales") or 0), -float(item.get("acos") or 9)),
            reverse=True,
        )

        existing_exact_terms = {
            base._normalize_keyword(keyword.get("keywordText"))
            for keyword in client.list_keywords(exact_campaign_id)
            if str(keyword.get("matchType") or "").upper() == "EXACT"
        }
        selected_terms: List[str] = []
        skipped_existing: List[str] = []
        for item in winners:
            term = base._normalize_keyword(item.get("term"))
            if not term:
                continue
            if term in existing_exact_terms:
                skipped_existing.append(term)
                continue
            selected_terms.append(term)
            existing_exact_terms.add(term)
            if len(selected_terms) >= max_terms:
                break

        keyword_rows = base._exact_keyword_rows(selected_terms, exact_campaign_id, exact_ad_group_id, exact_bid)
        created = 0
        if apply_live and keyword_rows:
            client.create_keywords(keyword_rows)
            created = len(keyword_rows)

        return JSONResponse({
            "success": True,
            "apply_live": apply_live,
            "product": product.get("title"),
            "report_id": report_id,
            "date_range": {"start": start_date, "end": end_date},
            "budget_protection": budget_protection_status(),
            "discovery_campaign_id": discovery_campaign_id,
            "exact_campaign_id": exact_campaign_id,
            "exact_ad_group_id": exact_ad_group_id,
            "rows_analyzed": len(discovery_rows),
            "winners_found": len(winners),
            "terms_selected": len(selected_terms),
            "keywords_created": created,
            "applied_bid": exact_bid,
            "terms_harvested": selected_terms,
            "skipped_existing_sample": skipped_existing[:25],
            "summary": summarize_classification(classified),
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
