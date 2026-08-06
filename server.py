"""Cloud Run entrypoint wrapper for the Amazon PPC Optimizer.

The full dashboard/API application lives in optimize_campaigns.py. This wrapper
keeps Cloud Run on a stable entrypoint, serves the dashboard as plain static
HTML, overrides product/campaign launch behavior, and patches search-term waste
rules plus dayparting so the optimizer protects budget before prime time.

Do not import app.py here; app.py is a smaller alternate app and does not expose
all dashboard endpoints such as /api/dashboard-data.
"""
import csv
import datetime
import hmac
import html as html_mod
import io
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, Header
from fastapi.responses import HTMLResponse, JSONResponse

import optimize_campaigns as optimizer_core
from optimize_campaigns import (
    AmazonAdsClient,
    DEFAULT_FALLBACK_BID,
    app,
    generate_keywords_for_product,
    load_products,
    normalized_product,
    parse_report_json_bytes,
    verify_internal_token,
)
from budget_dayparting import (
    budget_protection_status,
    choose_budget_protected_bid,
    choose_budget_protected_campaign_bid,
    get_budget_protection_mode,
)
from ppc_waste_rules import (
    COMPETITOR_OR_BRAND_PHRASES,
    WRONG_INTENT_PHRASES,
    apply_negatives_step_with_match_types,
    classify_search_terms,
    summarize_classification,
)

# Patch the live optimizer before any route handlers execute. Existing endpoints
# in optimize_campaigns.py resolve these globals at request time, so replacing
# them here changes /api/apply-negatives, /api/apply-winners,
# /api/apply-optimization, /api/retune-existing-bids, and product launch bids
# without duplicating those routes.
optimizer_core.classify_terms = classify_search_terms
optimizer_core.apply_negatives_step = apply_negatives_step_with_match_types
optimizer_core.choose_bid = choose_budget_protected_bid
optimizer_core.choose_campaign_applied_bid = choose_budget_protected_campaign_bid
optimizer_core.get_bid_mode = get_budget_protection_mode

BASE_DIR = Path(__file__).parent.absolute()
DASHBOARD_PATH = BASE_DIR / "templates" / "dashboard.html"
GENERIC_EXACT_BLOCKLIST = {
    "compost", "soil", "fertilizer", "plant", "plants", "garden", "lawn",
    "organic", "natural", "liquid", "outdoor", "indoor", "premium",
}


def _is_route(route, path: str, method: Optional[str] = None) -> bool:
    if getattr(route, "path", None) != path:
        return False
    if not method:
        return True
    return method.upper() in set(getattr(route, "methods", set()) or set())


# Remove the original root dashboard route, fallback-only product route, and old
# single-campaign launcher. All other API routes from optimize_campaigns.py remain.
app.router.routes = [
    route for route in app.router.routes
    if not (
        _is_route(route, "/", "GET")
        or _is_route(route, "/api/products", "GET")
        or _is_route(route, "/api/create-campaign-from-product", "POST")
    )
]


def _first_bid_context(client: AmazonAdsClient) -> Tuple[Optional[str], Optional[str]]:
    """Find any live campaign/ad group context Amazon can use for bid recs."""
    try:
        for campaign in client.list_campaigns()[:25]:
            campaign_id = str(campaign.get("campaignId") or "")
            if not campaign_id:
                continue
            try:
                keywords = client.list_keywords(campaign_id)
            except Exception:
                continue
            for keyword in keywords[:50]:
                ad_group_id = str(keyword.get("adGroupId") or "")
                if ad_group_id:
                    return campaign_id, ad_group_id
    except Exception:
        return None, None
    return None, None


def _primary_keyword(raw_row: Dict[str, Any], product: Dict[str, Any]) -> str:
    try:
        keywords = generate_keywords_for_product(raw_row, limit=1)
        if keywords:
            return keywords[0]
    except Exception:
        pass

    title = str(product.get("title") or raw_row.get("Title") or "fertilizer").lower()
    for phrase in (
        "dog urine", "fruit tree fertilizer", "liquid kelp", "humic acid",
        "bone meal", "pasture fertilizer", "lawn fertilizer", "compost",
    ):
        if phrase in title:
            return phrase
    return "fertilizer"


def _sanitize_name(name: str) -> str:
    name = html_mod.unescape(name or "")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", name).strip()


def _parse_positive_float(raw: Any, default: float, min_value: float) -> float:
    if raw in (None, ""):
        return round(float(default), 2)
    try:
        value = float(str(raw).strip())
    except Exception:
        raise ValueError(f"Invalid numeric value: {raw}")
    if value < min_value:
        raise ValueError(f"Value must be >= {min_value}")
    return round(value, 2)


def _extract_id(resp: Dict[str, Any], batch_key: str, item_key: str, id_key: str) -> Optional[str]:
    inner = resp.get(batch_key, {}) if isinstance(resp, dict) else {}
    if isinstance(inner, dict):
        success = inner.get("success", [])
        if success:
            return str((success[0].get(item_key) or success[0]).get(id_key) or "")
    if isinstance(inner, list) and inner:
        return str(inner[0].get(id_key) or "")
    return str(resp.get(id_key) or "") if isinstance(resp, dict) else None


def _optional_dashboard_auth(authorization: Optional[str], x_daily_optimizer_token: Optional[str]) -> Optional[JSONResponse]:
    """Match the old launch route: enforce token only when a token is configured."""
    token = os.getenv("DAILY_OPTIMIZER_TOKEN", "")
    if not token:
        return None
    supplied = None
    if x_daily_optimizer_token:
        supplied = x_daily_optimizer_token.strip()
    elif authorization and authorization.startswith("Bearer "):
        supplied = authorization.replace("Bearer ", "", 1).strip()
    if not supplied or not hmac.compare_digest(supplied, token):
        return JSONResponse({"error": True, "message": "Invalid token"}, status_code=403)
    return None


def _create_campaign(client: AmazonAdsClient, name: str, targeting_type: str, daily_budget: float, start_date: str) -> str:
    resp = client.post("/sp/campaigns", {
        "campaigns": [{
            "name": name,
            "targetingType": targeting_type,
            "state": "ENABLED",
            "budget": {"budget": round(daily_budget, 2), "budgetType": "DAILY"},
            "startDate": start_date,
        }]
    }, content_type="application/vnd.spcampaign.v3+json", accept="application/vnd.spcampaign.v3+json")
    campaign_id = _extract_id(resp, "campaigns", "campaign", "campaignId")
    if not campaign_id:
        raise RuntimeError(f"Campaign creation failed: {resp}")
    return campaign_id


def _create_ad_group(client: AmazonAdsClient, campaign_id: str, name: str, default_bid: float) -> str:
    resp = client.post("/sp/adGroups", {
        "adGroups": [{
            "name": name,
            "campaignId": str(campaign_id),
            "state": "ENABLED",
            "defaultBid": round(default_bid, 2),
        }]
    }, content_type="application/vnd.spadgroup.v3+json", accept="application/vnd.spadgroup.v3+json")
    ad_group_id = _extract_id(resp, "adGroups", "adGroup", "adGroupId")
    if not ad_group_id:
        raise RuntimeError(f"Ad group creation failed: {resp}")
    return ad_group_id


def _create_product_ad(client: AmazonAdsClient, campaign_id: str, ad_group_id: str, sku: str, asin: str) -> None:
    product_ad = {"campaignId": str(campaign_id), "adGroupId": str(ad_group_id), "state": "ENABLED"}
    if sku:
        product_ad["sku"] = sku
    if asin:
        product_ad["asin"] = asin
    client.post("/sp/productAds", {"productAds": [product_ad]},
                content_type="application/vnd.spproductad.v3+json",
                accept="application/vnd.spproductad.v3+json")


def _normalize_keyword(keyword: str) -> str:
    kw = str(keyword or "").strip().lower()
    kw = re.sub(r"[^a-z0-9'&\s-]", " ", kw)
    return re.sub(r"\s+", " ", kw).strip()


def _select_exact_keywords(keywords: List[str], max_keywords: int) -> List[str]:
    """Keep exact launch keywords focused on buyer-intent phrases."""
    selected: List[str] = []
    seen = set()
    for keyword in keywords:
        kw = _normalize_keyword(keyword)
        if not kw or kw in seen:
            continue
        seen.add(kw)
        # Do not launch exact campaigns on single generic words like compost/soil.
        if kw in GENERIC_EXACT_BLOCKLIST:
            continue
        # Prefer phrase keywords; one-word terms only survive if they are distinctive.
        if len(kw.split()) == 1 and len(kw) < 7:
            continue
        selected.append(kw)
        if len(selected) >= max_keywords:
            break
    return selected


def _exact_keyword_rows(keywords: List[str], campaign_id: str, ad_group_id: str, bid: float) -> List[Dict[str, Any]]:
    return [{
        "campaignId": str(campaign_id),
        "adGroupId": str(ad_group_id),
        "keywordText": keyword,
        "matchType": "EXACT",
        "state": "ENABLED",
        "bid": round(float(bid), 2),
    } for keyword in keywords]


def _seed_negative_rows(campaign_id: str, limit: int = 35) -> List[Dict[str, Any]]:
    """Seed launch campaigns with obvious wrong-intent negatives on day one."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    seed_terms = list(WRONG_INTENT_PHRASES) + list(COMPETITOR_OR_BRAND_PHRASES)
    for term in seed_terms:
        kw = _normalize_keyword(term)
        if not kw or kw in seen:
            continue
        seen.add(kw)
        rows.append({
            "campaignId": str(campaign_id),
            "keywordText": kw,
            "matchType": "NEGATIVE_PHRASE",
            "state": "ENABLED",
        })
        if len(rows) >= limit:
            break
    return rows


def _apply_launch_seed_negatives(client: AmazonAdsClient, campaign_ids: List[str]) -> Dict[str, Any]:
    applied: List[Dict[str, Any]] = []
    for campaign_id in campaign_ids:
        rows = _seed_negative_rows(campaign_id)
        if not rows:
            continue
        client.create_negative_keywords(rows)
        applied.append({
            "campaign_id": campaign_id,
            "count": len(rows),
            "terms_sample": [row["keywordText"] for row in rows[:10]],
        })
    return {
        "campaigns_seeded": len(applied),
        "negative_rows_created": sum(item["count"] for item in applied),
        "details": applied,
    }


def _enrich_product_bid(
    client: Optional[AmazonAdsClient],
    campaign_id: Optional[str],
    ad_group_id: Optional[str],
    raw_row: Dict[str, Any],
    product: Dict[str, Any],
) -> Dict[str, Any]:
    fallback_bid = float(product.get("suggested_bid") or DEFAULT_FALLBACK_BID)
    keyword = _primary_keyword(raw_row, product)
    status = budget_protection_status()

    product["bid_mode"] = status["bid_mode"]
    product["bid_keyword"] = keyword
    product["bid_source"] = "fallback_price_tier"
    product["amazon_bid_low"] = None
    product["amazon_bid_high"] = None
    product["amazon_bid_suggested"] = None
    product["budget_protection"] = status

    if not client or not campaign_id or not ad_group_id:
        low, high, applied = choose_budget_protected_bid({}, fallback_bid)
        product["suggested_bid"] = applied
        product["bid_source_note"] = "Amazon bid context unavailable; used budget-protected fallback bid."
        return product

    try:
        rec = client.get_bid_recommendation(campaign_id=campaign_id, ad_group_id=ad_group_id, keyword=keyword, match_type="PHRASE")
        low, high, applied = choose_budget_protected_bid(rec, fallback_bid)
        if low > 0 and high > 0:
            product["suggested_bid"] = applied
            product["amazon_bid_low"] = low
            product["amazon_bid_high"] = high
            product["amazon_bid_suggested"] = rec.get("suggested")
            product["bid_source"] = "amazon_suggested_budget_protected"
            product["bid_source_note"] = "Amazon suggested range used, then protected until PRIME time."
        else:
            product["suggested_bid"] = applied
            product["bid_source_note"] = "Amazon did not return low/high bid range; used budget-protected fallback bid."
    except Exception as exc:
        low, high, applied = choose_budget_protected_bid({}, fallback_bid)
        product["suggested_bid"] = applied
        product["bid_source_note"] = f"Amazon bid recommendation failed; used budget-protected fallback. {type(exc).__name__}"

    return product


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_static():
    try:
        return HTMLResponse(
            DASHBOARD_PATH.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"},
        )
    except Exception as exc:
        return HTMLResponse(
            f"""
            <h2>Amazon PPC Optimizer Dashboard</h2>
            <p>Service is running.</p>
            <p style=\"color: red; margin: 20px 0;\">
                <strong>Dashboard File Error:</strong> {type(exc).__name__}<br>{exc}
            </p>
            <p>Base directory: {BASE_DIR}</p><p>Dashboard path: {DASHBOARD_PATH}</p>
            """,
            status_code=500,
        )


@app.get("/api/products")
def api_products_with_live_bids():
    try:
        rows = load_products()
        products = [normalized_product(row) for row in rows]
        client = None
        campaign_id = None
        ad_group_id = None
        try:
            client = AmazonAdsClient()
            campaign_id, ad_group_id = _first_bid_context(client)
        except Exception:
            client = None
        enriched = [_enrich_product_bid(client, campaign_id, ad_group_id, raw_row, product) for raw_row, product in zip(rows, products)]
        return JSONResponse({
            "count": len(enriched),
            "bid_mode": get_budget_protection_mode(),
            "budget_protection": budget_protection_status(),
            "bid_context_available": bool(client and campaign_id and ad_group_id),
            "products": enriched,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/create-campaign-from-product")
def api_create_recommended_campaigns(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Launch the recommended two-campaign structure: AUTO DISCOVERY + MANUAL EXACT.

    The old launcher created one manual campaign with exact/phrase/broad mixed in a
    single ad group. This version keeps discovery spend separate from exact spend,
    seeds wrong-intent negatives immediately, and limits exact keywords to the
    strongest buyer-intent phrases.
    """
    auth_error = _optional_dashboard_auth(authorization, x_daily_optimizer_token)
    if auth_error:
        return auth_error
    try:
        key = (payload.get("product_id") or payload.get("sku") or "").lower().strip()
        if not key:
            return JSONResponse({"error": True, "message": "product_id or sku required"}, status_code=400)

        product = None
        product_row: Dict[str, Any] = {}
        for row in load_products():
            if row.get("Product_ID", "").lower() == key or row.get("SKU", "").lower() == key:
                product = normalized_product(row)
                product_row = row
                break
        if not product:
            return JSONResponse({"error": True, "message": "Product not found"}, status_code=404)

        sku = str(product.get("sku") or "").strip()
        asin = str(product.get("asin") or "").strip()
        if not sku and not asin:
            return JSONResponse({"error": True, "message": "Product must include at least one of SKU or ASIN"}, status_code=400)

        total_budget = _parse_positive_float(payload.get("daily_budget", payload.get("budget")), float(product["suggested_budget"]), 2.0)
        base_bid = _parse_positive_float(payload.get("starting_bid", payload.get("bid")), float(product["suggested_bid"]), 0.02)
        discovery_budget_pct = float(payload.get("discovery_budget_pct", 0.30))
        discovery_budget_pct = max(0.10, min(0.50, discovery_budget_pct))
        discovery_budget = round(max(1.0, total_budget * discovery_budget_pct), 2)
        exact_budget = round(max(1.0, total_budget - discovery_budget), 2)
        max_exact_keywords = int(payload.get("max_exact_keywords", 40))
        max_exact_keywords = max(5, min(80, max_exact_keywords))

        # Protect discovery bids more aggressively. Exact gets the higher-quality budget.
        _, _, protected_bid = choose_budget_protected_bid({}, base_bid)
        discovery_bid = round(max(0.10, protected_bid * 0.70), 2)
        exact_bid = round(max(0.10, protected_bid * 1.15), 2)

        raw_keywords = generate_keywords_for_product(product_row)
        exact_keywords = _select_exact_keywords(raw_keywords, max_exact_keywords)
        client = AmazonAdsClient()
        start_date = datetime.date.today().isoformat()
        safe_title = _sanitize_name(str(product.get("title") or "Product"))[:70]

        # 1) Amazon-recommended discovery: automatic targeting campaign.
        discovery_campaign_id = _create_campaign(
            client,
            f"{safe_title} | AUTO DISCOVERY | {start_date}",
            "AUTO",
            discovery_budget,
            start_date,
        )
        discovery_ad_group_id = _create_ad_group(client, discovery_campaign_id, "Auto Discovery", discovery_bid)
        _create_product_ad(client, discovery_campaign_id, discovery_ad_group_id, sku, asin)

        # 2) Controlled harvesting campaign: exact-only manual campaign.
        exact_campaign_id = _create_campaign(
            client,
            f"{safe_title} | MANUAL EXACT | {start_date}",
            "MANUAL",
            exact_budget,
            start_date,
        )
        exact_ad_group_id = _create_ad_group(client, exact_campaign_id, "Exact Winners", exact_bid)
        _create_product_ad(client, exact_campaign_id, exact_ad_group_id, sku, asin)

        exact_rows = _exact_keyword_rows(exact_keywords, exact_campaign_id, exact_ad_group_id, exact_bid)
        exact_keywords_created = 0
        if exact_rows:
            client.create_keywords(exact_rows)
            exact_keywords_created = len(exact_rows)

        # Seed both campaigns with obvious wrong-intent negatives from day one.
        launch_negatives = _apply_launch_seed_negatives(client, [discovery_campaign_id, exact_campaign_id])

        return JSONResponse({
            "success": True,
            "structure": "recommended_auto_discovery_plus_manual_exact",
            "product": product["title"],
            "sku": sku,
            "asin": asin,
            "total_daily_budget": total_budget,
            "budget_protection": budget_protection_status(),
            "launch_negatives": launch_negatives,
            "keyword_filtering": {
                "generated_keywords_count": len(raw_keywords),
                "exact_keywords_selected": len(exact_keywords),
                "max_exact_keywords": max_exact_keywords,
                "blocked_generic_single_terms": sorted(GENERIC_EXACT_BLOCKLIST),
            },
            "campaigns_created": [
                {
                    "campaign_type": "AUTO_DISCOVERY",
                    "campaign_id": discovery_campaign_id,
                    "ad_group_id": discovery_ad_group_id,
                    "daily_budget": discovery_budget,
                    "default_bid": discovery_bid,
                    "seed_negative_rows_created": launch_negatives["details"][0]["count"] if launch_negatives["details"] else 0,
                    "purpose": "Find converting search terms cheaply without mixing discovery spend into exact winners.",
                },
                {
                    "campaign_type": "MANUAL_EXACT",
                    "campaign_id": exact_campaign_id,
                    "ad_group_id": exact_ad_group_id,
                    "daily_budget": exact_budget,
                    "default_bid": exact_bid,
                    "keywords_count": len(exact_keywords),
                    "keyword_rows_created": exact_keywords_created,
                    "seed_negative_rows_created": launch_negatives["details"][1]["count"] if len(launch_negatives["details"]) > 1 else 0,
                    "purpose": "Control spend on the most relevant buyer-intent exact keywords.",
                },
            ],
        })
    except ValueError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.get("/api/dayparting-status")
def dayparting_status() -> Dict[str, Any]:
    """Show whether the optimizer is currently protecting budget or competing hard."""
    return budget_protection_status()


@app.post("/api/search-term-waste-preview")
def search_term_waste_preview(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Preview the stronger rules before applying negatives live.

    Input options:
    - rows: list of Amazon search-term report rows
    - csv_text: pasted/exported Amazon search-term CSV content
    - report_url: Amazon report download URL already generated by the Ads API
    """
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        rows = payload.get("rows")
        if not rows and payload.get("csv_text"):
            rows = list(csv.DictReader(io.StringIO(str(payload["csv_text"]))))
        if not rows and payload.get("report_url"):
            client = AmazonAdsClient()
            rows = parse_report_json_bytes(client.download_binary(str(payload["report_url"])))
        if not rows:
            return JSONResponse({"error": True, "message": "Provide rows, csv_text, or report_url."}, status_code=400)

        classified = classify_search_terms(rows)
        summary = summarize_classification(classified)
        return JSONResponse({
            "success": True,
            "summary": summary,
            "winners": classified.get("winners", [])[:50],
            "negatives": classified.get("negatives", [])[:100],
            "bid_down": classified.get("bid_down", [])[:100],
            "hold_sample": classified.get("hold", [])[:50],
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
