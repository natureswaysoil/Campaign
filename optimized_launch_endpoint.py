"""Guarded optimized Sponsored Products launch endpoint.

This module provides a real launch flow that is preview-first and guarded by
`confirm: true`. Import and include it from optimize_campaigns.py after the app,
AmazonAdsClient, token verifier, and helper functions are available.
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

from optimized_launch_preview import build_optimized_launch_preview


def extract_amazon_created_id(resp: Dict[str, Any], batch_key: str, item_key: str, id_key: str) -> Optional[str]:
    """Safely extract IDs from Amazon Ads v3 batch responses."""
    inner = resp.get(batch_key, {})

    if isinstance(inner, dict):
        success = inner.get("success", [])
        if success:
            first = success[0]
            item = first.get(item_key) or first
            return str(item.get(id_key)) if item.get(id_key) else None

    if isinstance(inner, list) and inner:
        return str(inner[0].get(id_key)) if inner[0].get(id_key) else None

    if resp.get(id_key):
        return str(resp.get(id_key))

    return None


def sanitize_campaign_name(name: str) -> str:
    """Keep Amazon campaign names safe and reasonably short."""
    name = re.sub(r"[^a-zA-Z0-9\s\-_/]", " ", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120]


def register_optimized_launch_routes(
    app,
    *,
    AmazonAdsClient,
    MEDIA_TYPES: Dict[str, str],
    DEFAULT_FALLBACK_BID: float,
    verify_internal_token,
    get_bid_mode,
    today_iso_date,
    utc_now_iso,
    save_optimizer_history,
    logger,
):
    """Attach /api/launch-optimized to the FastAPI app."""

    @app.post("/api/launch-optimized")
    def launch_optimized(
        payload: Dict[str, Any],
        authorization: Optional[str] = Header(None),
        x_daily_optimizer_token: Optional[str] = Header(None),
    ) -> JSONResponse:
        """Real optimized Sponsored Products launch endpoint.

        Safety rules:
        - Requires DAILY_OPTIMIZER_TOKEN
        - Defaults to preview-only unless payload.confirm == true
        - Builds preview first
        - Creates campaigns/ad groups/product ads/keywords only after confirmation
        """
        verify_internal_token(authorization, x_daily_optimizer_token)

        confirm = bool(payload.get("confirm", False))

        product = payload.get("product") or {}
        if not isinstance(product, dict):
            raise HTTPException(status_code=400, detail="product must be an object")

        asin = str(product.get("asin") or product.get("ASIN") or "").strip()
        sku = str(product.get("sku") or product.get("sellerSku") or product.get("SKU") or "").strip()
        title = str(product.get("title") or product.get("name") or product.get("productName") or "Untitled Product").strip()

        if not asin:
            raise HTTPException(status_code=400, detail="product.asin is required")

        manual_keywords = payload.get("keywords") or payload.get("manual_keywords") or []
        if isinstance(manual_keywords, str):
            manual_keywords = [k.strip() for k in manual_keywords.split(",") if k.strip()]
        if not isinstance(manual_keywords, list):
            raise HTTPException(status_code=400, detail="keywords must be list or comma-separated string")

        try:
            budget = float(payload.get("budget", 10.0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="budget must be a number")

        preview = build_optimized_launch_preview(
            product=product,
            manual_keywords=[str(k) for k in manual_keywords],
            budget=budget,
            fallback_bid=DEFAULT_FALLBACK_BID,
            bid_mode=get_bid_mode(),
        )

        if not confirm:
            preview["message"] = "Preview only. To create campaigns, call this endpoint again with confirm: true."
            preview["confirm_required"] = True
            return JSONResponse(preview)

        client = AmazonAdsClient()
        launch_results: List[Dict[str, Any]] = []

        try:
            today = today_iso_date()

            for campaign_plan in preview.get("campaigns", []):
                campaign_type = campaign_plan.get("type")
                campaign_name = sanitize_campaign_name(campaign_plan.get("name") or f"NWS - {title} - Optimized")
                daily_budget = round(float(campaign_plan.get("daily_budget") or 3.0), 2)

                campaign_resp = client.post(
                    "/sp/campaigns",
                    {
                        "campaigns": [
                            {
                                "name": campaign_name,
                                "targetingType": "AUTO" if campaign_type == "auto_discovery" else "MANUAL",
                                "state": "ENABLED",
                                "dynamicBidding": {"strategy": "LEGACY_FOR_SALES"},
                                "startDate": today,
                                "budget": {"budgetType": "DAILY", "budget": daily_budget},
                            }
                        ]
                    },
                    content_type=MEDIA_TYPES["campaigns_v3"],
                    accept=MEDIA_TYPES["campaigns_v3"],
                )

                campaign_id = extract_amazon_created_id(campaign_resp, "campaigns", "campaign", "campaignId")
                if not campaign_id:
                    launch_results.append({
                        "campaign_name": campaign_name,
                        "type": campaign_type,
                        "error": "Campaign created response did not include campaignId",
                        "response": campaign_resp,
                    })
                    continue

                ad_group_name = sanitize_campaign_name(f"{campaign_name} - Ad Group")
                default_bid = round(float(campaign_plan.get("starting_bid") or DEFAULT_FALLBACK_BID), 2)

                if campaign_type in ("manual_exact_phrase", "manual_broad_research"):
                    keyword_list = campaign_plan.get("keywords") or []
                    if keyword_list:
                        first_kw = keyword_list[0]
                        default_bid = round(float(
                            first_kw.get("phrase_bid")
                            or first_kw.get("broad_bid")
                            or first_kw.get("exact_bid")
                            or DEFAULT_FALLBACK_BID
                        ), 2)

                ad_group_resp = client.post(
                    "/sp/adGroups",
                    {
                        "adGroups": [
                            {
                                "campaignId": str(campaign_id),
                                "name": ad_group_name,
                                "state": "ENABLED",
                                "defaultBid": default_bid,
                            }
                        ]
                    },
                    content_type="application/vnd.spadgroup.v3+json",
                    accept="application/vnd.spadgroup.v3+json",
                )

                ad_group_id = extract_amazon_created_id(ad_group_resp, "adGroups", "adGroup", "adGroupId")
                if not ad_group_id:
                    launch_results.append({
                        "campaign_name": campaign_name,
                        "campaign_id": campaign_id,
                        "type": campaign_type,
                        "error": "Ad group created response did not include adGroupId",
                        "response": ad_group_resp,
                    })
                    continue

                product_ad_body = {
                    "productAds": [
                        {
                            "campaignId": str(campaign_id),
                            "adGroupId": str(ad_group_id),
                            "state": "ENABLED",
                            "asin": asin,
                        }
                    ]
                }
                if sku:
                    product_ad_body["productAds"][0]["sku"] = sku

                product_ad_resp = client.post(
                    "/sp/productAds",
                    product_ad_body,
                    content_type="application/vnd.spproductad.v3+json",
                    accept="application/vnd.spproductad.v3+json",
                )

                keyword_resp = None
                keyword_rows: List[Dict[str, Any]] = []

                if campaign_type == "manual_exact_phrase":
                    for item in campaign_plan.get("keywords") or []:
                        kw = item.get("keyword")
                        if not kw:
                            continue
                        exact_bid = round(float(item.get("exact_bid") or DEFAULT_FALLBACK_BID), 2)
                        phrase_bid = round(float(item.get("phrase_bid") or DEFAULT_FALLBACK_BID), 2)
                        keyword_rows.append({
                            "campaignId": str(campaign_id),
                            "adGroupId": str(ad_group_id),
                            "keywordText": kw,
                            "matchType": "EXACT",
                            "state": "ENABLED",
                            "bid": exact_bid,
                        })
                        keyword_rows.append({
                            "campaignId": str(campaign_id),
                            "adGroupId": str(ad_group_id),
                            "keywordText": kw,
                            "matchType": "PHRASE",
                            "state": "ENABLED",
                            "bid": phrase_bid,
                        })

                elif campaign_type == "manual_broad_research":
                    for item in campaign_plan.get("keywords") or []:
                        kw = item.get("keyword")
                        if not kw:
                            continue
                        broad_bid = round(float(item.get("broad_bid") or DEFAULT_FALLBACK_BID), 2)
                        keyword_rows.append({
                            "campaignId": str(campaign_id),
                            "adGroupId": str(ad_group_id),
                            "keywordText": kw,
                            "matchType": "BROAD",
                            "state": "ENABLED",
                            "bid": broad_bid,
                        })

                if keyword_rows:
                    keyword_resp = client.create_keywords(keyword_rows)

                launch_results.append({
                    "campaign_name": campaign_name,
                    "campaign_id": campaign_id,
                    "ad_group_id": ad_group_id,
                    "type": campaign_type,
                    "daily_budget": daily_budget,
                    "default_bid": default_bid,
                    "product_ad_response": product_ad_resp,
                    "keyword_count": len(keyword_rows),
                    "keyword_response": keyword_resp,
                })

            save_optimizer_history({
                "type": "optimized_campaign_launch",
                "created_at": utc_now_iso(),
                "product": {"title": title, "asin": asin, "sku": sku},
                "budget": budget,
                "results": launch_results,
            })

            return JSONResponse({
                "success": True,
                "message": "Optimized campaigns launched.",
                "product": {"title": title, "asin": asin, "sku": sku},
                "results": launch_results,
                "preview_used": preview,
            })

        except Exception as e:
            logger.exception("Optimized campaign launch failed")
            return JSONResponse({
                "success": False,
                "error": True,
                "message": str(e),
                "preview_used": preview,
                "partial_results": launch_results,
            }, status_code=500)

    return launch_optimized
