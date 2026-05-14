"""Cloud Run entrypoint wrapper for the Amazon PPC Optimizer.

The full dashboard/API application lives in optimize_campaigns.py. This wrapper
keeps Cloud Run on a stable entrypoint, serves the dashboard as plain static
HTML, and overrides the product list endpoint so launch bids try Amazon's live
suggested bid range before falling back to the old price-tier bid.

Do not import app.py here; app.py is a smaller alternate app and does not expose
all dashboard endpoints such as /api/dashboard-data.
"""
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi.responses import HTMLResponse, JSONResponse

from optimize_campaigns import (
    AmazonAdsClient,
    DEFAULT_FALLBACK_BID,
    app,
    choose_bid,
    generate_keywords_for_product,
    get_bid_mode,
    load_products,
    normalized_product,
)

BASE_DIR = Path(__file__).parent.absolute()
DASHBOARD_PATH = BASE_DIR / "templates" / "dashboard.html"


def _is_get_route(route, path: str) -> bool:
    return (
        getattr(route, "path", None) == path
        and "GET" in set(getattr(route, "methods", set()) or set())
    )


# Remove the original root dashboard route and the original fallback-only product
# route. All other API routes from optimize_campaigns.py remain available.
app.router.routes = [
    route for route in app.router.routes
    if not (_is_get_route(route, "/") or _is_get_route(route, "/api/products"))
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
    for phrase in ("dog urine", "fruit tree fertilizer", "liquid kelp", "humic acid", "bone meal", "pasture fertilizer", "lawn fertilizer"):
        if phrase in title:
            return phrase
    return "fertilizer"


def _enrich_product_bid(
    client: Optional[AmazonAdsClient],
    campaign_id: Optional[str],
    ad_group_id: Optional[str],
    raw_row: Dict[str, Any],
    product: Dict[str, Any],
) -> Dict[str, Any]:
    fallback_bid = float(product.get("suggested_bid") or DEFAULT_FALLBACK_BID)
    keyword = _primary_keyword(raw_row, product)
    mode = get_bid_mode()

    product["bid_mode"] = mode
    product["bid_keyword"] = keyword
    product["bid_source"] = "fallback_price_tier"
    product["amazon_bid_low"] = None
    product["amazon_bid_high"] = None
    product["amazon_bid_suggested"] = None

    if not client or not campaign_id or not ad_group_id:
        low, high, applied = choose_bid({}, fallback_bid)
        product["suggested_bid"] = applied
        product["bid_source_note"] = "Amazon bid context unavailable; used fallback with time-of-day multiplier."
        return product

    try:
        rec = client.get_bid_recommendation(
            campaign_id=campaign_id,
            ad_group_id=ad_group_id,
            keyword=keyword,
            match_type="PHRASE",
        )
        low, high, applied = choose_bid(rec, fallback_bid)
        if low > 0 and high > 0:
            product["suggested_bid"] = applied
            product["amazon_bid_low"] = low
            product["amazon_bid_high"] = high
            product["amazon_bid_suggested"] = rec.get("suggested")
            product["bid_source"] = "amazon_suggested"
            product["bid_source_note"] = "Amazon suggested range used, then adjusted for PEAK/OFF_PEAK/NORMAL mode."
        else:
            product["suggested_bid"] = applied
            product["bid_source_note"] = "Amazon did not return low/high bid range; used fallback with time-of-day multiplier."
    except Exception as exc:
        low, high, applied = choose_bid({}, fallback_bid)
        product["suggested_bid"] = applied
        product["bid_source_note"] = f"Amazon bid recommendation failed; used fallback. {type(exc).__name__}"

    return product


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_static():
    try:
        return HTMLResponse(
            DASHBOARD_PATH.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f"""
            <h2>Amazon PPC Optimizer Dashboard</h2>
            <p>Service is running.</p>
            <p style=\"color: red; margin: 20px 0;\">
                <strong>Dashboard File Error:</strong> {type(exc).__name__}<br>
                {exc}
            </p>
            <p>Base directory: {BASE_DIR}</p>
            <p>Dashboard path: {DASHBOARD_PATH}</p>
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

        enriched = [
            _enrich_product_bid(client, campaign_id, ad_group_id, raw_row, product)
            for raw_row, product in zip(rows, products)
        ]

        return JSONResponse({
            "count": len(enriched),
            "bid_mode": get_bid_mode(),
            "bid_context_available": bool(client and campaign_id and ad_group_id),
            "products": enriched,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
