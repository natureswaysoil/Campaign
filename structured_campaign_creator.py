"""Live structured campaign creation for Amazon Sponsored Products.

This module takes a campaign_engine plan and pushes the full campaign structure:
AUTO, EXACT core, EXACT long-tail, PHRASE research, BROAD discovery, COMPETITOR,
and optional PRODUCT targeting shell.
"""
from __future__ import annotations

from typing import Any, Dict, List

from campaign_engine import build_campaign_plan


def _extract_first_id(payload: Any, batch_key: str, item_key: str) -> int:
    if isinstance(payload, dict):
        inner = payload.get(batch_key)
        if isinstance(inner, dict) and inner.get("success"):
            item = inner["success"][0].get(item_key, inner["success"][0])
            for key in ("campaignId", "adGroupId", "adId", "keywordId", "id"):
                if item.get(key):
                    return int(item[key])
        for key in ("campaignId", "adGroupId", "adId", "keywordId", "id"):
            if payload.get(key):
                return int(payload[key])
    raise RuntimeError(f"Could not extract ID from {batch_key} response: {payload}")


def _keyword_rows(campaign: Dict[str, Any], campaign_id: int, ad_group_id: int) -> List[Dict[str, Any]]:
    match_type = str(campaign.get("match_type") or "exact").upper()
    if match_type == "PRODUCT" or match_type == "AUTO":
        return []
    if match_type not in {"EXACT", "PHRASE", "BROAD"}:
        match_type = "EXACT"
    bid = round(float(campaign.get("default_bid") or 0.55), 2)
    return [
        {
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "keywordText": kw,
            "matchType": match_type,
            "state": "ENABLED",
            "bid": bid,
        }
        for kw in campaign.get("keywords", [])
    ]


def _negative_rows(campaign: Dict[str, Any], campaign_id: int) -> List[Dict[str, Any]]:
    negatives = campaign.get("negatives") or {}
    rows: List[Dict[str, Any]] = []
    for term in negatives.get("negative_exact", []):
        rows.append({
            "campaignId": str(campaign_id),
            "keywordText": term,
            "matchType": "NEGATIVE_EXACT",
            "state": "ENABLED",
        })
    for term in negatives.get("negative_phrase", []):
        rows.append({
            "campaignId": str(campaign_id),
            "keywordText": term,
            "matchType": "NEGATIVE_PHRASE",
            "state": "ENABLED",
        })
    return rows


def create_structured_campaigns_for_product(row: Dict[str, Any], amazon_client: Any, start_date: str) -> Dict[str, Any]:
    """Create all campaigns from a sheet/product row using an existing AmazonAdsClient.

    amazon_client must expose post(endpoint, body), where body can be a list and the
    client wraps it for Amazon Ads v3.
    """
    plan = build_campaign_plan(row)
    asin = plan.get("asin")
    if not asin:
        raise ValueError("ASIN is required to create product ads")

    created: List[Dict[str, Any]] = []

    for campaign in plan["campaigns"]:
        campaign_type = campaign["campaign_type"]
        targeting_type = "AUTO" if campaign_type == "AUTO_Discovery" else "MANUAL"

        campaign_payload = [{
            "name": campaign["campaign_name"][:128],
            "targetingType": targeting_type,
            "state": "ENABLED",
            "budget": {
                "budget": round(float(campaign["daily_budget"]), 2),
                "budgetType": "DAILY",
            },
            "startDate": start_date,
        }]
        campaign_resp = amazon_client.post("/sp/campaigns", campaign_payload)
        campaign_id = _extract_first_id(campaign_resp, "campaigns", "campaign")

        ad_group_payload = [{
            "name": f"{campaign_type} Main Ad Group"[:128],
            "campaignId": str(campaign_id),
            "state": "ENABLED",
            "defaultBid": round(float(campaign["default_bid"]), 2),
        }]
        ad_group_resp = amazon_client.post("/sp/adGroups", ad_group_payload)
        ad_group_id = _extract_first_id(ad_group_resp, "adGroups", "adGroup")

        product_ad_payload = [{
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "asin": asin,
            "state": "ENABLED",
        }]
        amazon_client.post("/sp/productAds", product_ad_payload)

        keyword_count = 0
        keyword_payload = _keyword_rows(campaign, campaign_id, ad_group_id)
        if keyword_payload:
            amazon_client.post("/sp/keywords", keyword_payload)
            keyword_count = len(keyword_payload)

        negative_count = 0
        negative_payload = _negative_rows(campaign, campaign_id)
        if negative_payload:
            amazon_client.post("/sp/campaignNegativeKeywords", negative_payload)
            negative_count = len(negative_payload)

        created.append({
            "campaign_type": campaign_type,
            "campaign_name": campaign["campaign_name"],
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "targeting_type": targeting_type,
            "daily_budget": campaign["daily_budget"],
            "default_bid": campaign["default_bid"],
            "keyword_count": keyword_count,
            "negative_count": negative_count,
        })

    return {
        "message": "Structured campaigns created successfully",
        "product_name": plan["product_name"],
        "asin": plan["asin"],
        "sku": plan["sku"],
        "campaign_count": len(created),
        "created": created,
    }
