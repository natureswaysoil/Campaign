"""Nature's Way Soil Amazon PPC waste-control rules.

This module is intentionally dependency-free so it can be imported by the Cloud Run
FastAPI app and also used in local/offline tests with exported Amazon Search Term
CSV rows.

Goals:
- Catch obvious wrong-intent searches before they burn 20 clicks.
- Separate true winners from broad expensive terms.
- Keep broad root terms like "compost" out of automatic negatives and automatic
  winner promotion; flag them for bid reduction or review instead.
- Protect core buyer-intent product phrases from automatic negatives.
- Support NEGATIVE_EXACT and NEGATIVE_PHRASE when applying negatives.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional


WINNER_MIN_CLICKS = int(os.getenv("WINNER_MIN_CLICKS", "8"))
WINNER_MIN_ORDERS = int(os.getenv("WINNER_MIN_ORDERS", "2"))
WINNER_MAX_ACOS = float(os.getenv("WINNER_MAX_ACOS", "0.35"))

# Lower than the old 20-click rule; Amazon can waste real money before 20 clicks.
NEGATIVE_MIN_CLICKS = int(os.getenv("NEGATIVE_MIN_CLICKS", "12"))
NEGATIVE_MIN_SPEND_NO_SALE = float(os.getenv("NEGATIVE_MIN_SPEND_NO_SALE", "8.00"))
HIGH_ACOS_NEGATIVE = float(os.getenv("HIGH_ACOS_NEGATIVE", "1.00"))
HIGH_ACOS_MIN_SPEND = float(os.getenv("HIGH_ACOS_MIN_SPEND", "10.00"))
BID_DOWN_ACOS = float(os.getenv("BID_DOWN_ACOS", "0.60"))
BID_DOWN_MIN_SPEND = float(os.getenv("BID_DOWN_MIN_SPEND", "15.00"))

# Terms that are commonly wrong intent for bagged living compost / soil products.
# These should usually be NEGATIVE_PHRASE because the whole phrase is the problem.
WRONG_INTENT_PHRASES = tuple(
    phrase.lower()
    for phrase in (
        "compost bin",
        "compost bins",
        "in ground compost",
        "inground compost",
        "kitchen compost",
        "countertop compost",
        "compost tumbler",
        "worm bin",
        "live worms",
        "compost worms live",
        "composting worms live",
        "red wigglers live",
        "cow manure",
        "black cow",
        "manure compost",
        "cotton burr compost",
        "mushroom compost",
        "ericaceous compost",
        "bokashi bucket",
        "compost toilet",
    )
)

# Brand/competitor searches where the shopper is usually looking for a specific
# brand, not Nature's Way Soil. Keep these phrase-level, not broad single words.
COMPETITOR_OR_BRAND_PHRASES = tuple(
    phrase.lower()
    for phrase in (
        "coast of maine",
        "black gold",
        "back to roots",
        "biosol",
        "black kow",
        "charlie's compost",
        "charlies compost",
        "r&m compost",
        "r and m compost",
    )
)

# Root terms can sell, but they are too broad to auto-promote as exact winners or
# auto-negate. They should be managed with bid reduction, product-specific manual
# campaigns, or human review.
BROAD_ROOT_TERMS = {"compost", "soil", "fertilizer", "lawn fertilizer", "garden soil", "potting soil"}

# These are core buyer phrases for Nature's Way Soil products. If they perform
# badly, lower bids or review them manually; do not automatically add them as
# negative keywords because that can shut off the main demand for a product.
PROTECTED_BUYER_PHRASES = tuple(
    phrase.lower()
    for phrase in (
        "dog urine",
        "dog pee",
        "urine neutralizer",
        "lawn neutralizer",
        "grass neutralizer",
        "dog urine neutralizer",
        "dog pee grass",
        "dog urine grass",
        "liquid bone meal",
        "bone meal",
        "liquid kelp",
        "kelp fertilizer",
        "humic acid",
        "fulvic acid",
        "liquid biochar",
        "biochar",
        "pasture fertilizer",
        "hay fertilizer",
        "lawn recovery",
        "soil recovery",
        "fruit tree fertilizer",
        "bloom fertilizer",
    )
)


def normalize_term(value: Any) -> str:
    term = str(value or "").strip().lower()
    term = term.replace("’", "'")
    term = re.sub(r"[^a-z0-9'&\s-]", " ", term)
    return re.sub(r"\s+", " ", term).strip()


def num(row: Dict[str, Any], keys: Iterable[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            try:
                return float(str(row[key]).replace("$", "").replace(",", "").strip())
            except Exception:
                continue
    return default


def text(row: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return default


def _contains_any(term: str, phrases: Iterable[str]) -> Optional[str]:
    padded = f" {term} "
    for phrase in phrases:
        if f" {phrase} " in padded or term == phrase:
            return phrase
    return None


def _base_result(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_term = text(row, ["Customer Search Term", "searchTerm", "Search Term", "search term"])
    term = normalize_term(raw_term)
    if not term:
        return None

    clicks = int(num(row, ["Clicks", "clicks"], 0))
    cost = num(row, ["Spend", "Cost", "cost", "spend"], 0.0)
    sales = num(row, ["7 Day Total Sales", "14 Day Total Sales", "sales7d", "sales14d", "sales"], 0.0)
    orders = int(num(row, ["7 Day Total Orders (#)", "14 Day Total Orders (#)", "orders", "purchases7d", "purchases14d"], 0))
    acos = (cost / sales) if sales > 0 else None

    return {
        "term": term,
        "raw_term": raw_term,
        "campaign_id": int(num(row, ["Campaign Id", "campaignId", "campaign_id"], 0)),
        "ad_group_id": int(num(row, ["Ad Group Id", "adGroupId", "ad_group_id"], 0)),
        "clicks": clicks,
        "orders": orders,
        "cost": round(cost, 2),
        "sales": round(sales, 2),
        "acos": round(acos, 4) if acos is not None else None,
    }


def classify_search_terms(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Classify search-term rows into winners, negatives, bid-downs, and hold.

    Existing optimizer code consumes ``winners`` and ``negatives``. ``bid_down`` is
    included for dashboard/preview output so James can see spend leaks without
    automatically blocking important buyer terms.
    """
    winners: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []
    bid_down: List[Dict[str, Any]] = []
    hold: List[Dict[str, Any]] = []

    for row in rows:
        result = _base_result(row)
        if not result:
            continue

        term = result["term"]
        clicks = int(result["clicks"])
        orders = int(result["orders"])
        cost = float(result["cost"])
        sales = float(result["sales"])
        acos = result["acos"]

        wrong_intent = _contains_any(term, WRONG_INTENT_PHRASES)
        competitor = _contains_any(term, COMPETITOR_OR_BRAND_PHRASES)
        protected_buyer_phrase = _contains_any(term, PROTECTED_BUYER_PHRASES)

        if wrong_intent or competitor:
            negatives.append({
                **result,
                "reason": "wrong_intent" if wrong_intent else "competitor_or_brand_intent",
                "matched_phrase": wrong_intent or competitor,
                "negative_match_type": "NEGATIVE_PHRASE",
            })
            continue

        # Broad roots and core buyer phrases should not be auto-negated or
        # auto-promoted. If expensive, flag for bid reduction; otherwise hold.
        if term in BROAD_ROOT_TERMS or protected_buyer_phrase:
            if cost >= BID_DOWN_MIN_SPEND or (sales > 0 and acos is not None and acos >= BID_DOWN_ACOS):
                bid_down.append({
                    **result,
                    "reason": "protected_core_or_broad_term_review_bid_down",
                    "matched_phrase": protected_buyer_phrase,
                    "recommended_bid_multiplier": 0.50 if (acos or 0) >= 0.75 or orders == 0 else 0.65,
                })
            else:
                hold.append({
                    **result,
                    "reason": "protected_core_or_broad_term_hold",
                    "matched_phrase": protected_buyer_phrase,
                })
            continue

        if (
            orders >= WINNER_MIN_ORDERS
            and clicks >= WINNER_MIN_CLICKS
            and sales > 0
            and (acos is None or acos <= WINNER_MAX_ACOS)
        ):
            winners.append({**result, "reason": "winner"})
            continue

        # No-order spend leak: negative sooner than the old 20-click rule, but
        # only after protected buyer terms have been removed from auto-negatives.
        if orders == 0 and (clicks >= NEGATIVE_MIN_CLICKS or cost >= NEGATIVE_MIN_SPEND_NO_SALE):
            negatives.append({
                **result,
                "reason": "no_sales_spend_leak",
                "negative_match_type": "NEGATIVE_EXACT",
            })
            continue

        # If it technically sold but lost money badly, stop exact term leakage.
        if sales > 0 and acos is not None and acos >= HIGH_ACOS_NEGATIVE and cost >= HIGH_ACOS_MIN_SPEND and orders <= 1:
            negatives.append({
                **result,
                "reason": "high_acos_exact_leak",
                "negative_match_type": "NEGATIVE_EXACT",
            })
            continue

        if sales > 0 and acos is not None and acos >= BID_DOWN_ACOS and cost >= BID_DOWN_MIN_SPEND:
            bid_down.append({
                **result,
                "reason": "high_acos_lower_bid",
                "recommended_bid_multiplier": 0.65 if acos < 0.85 else 0.50,
            })
            continue

        hold.append({**result, "reason": "hold"})

    return {"winners": winners, "negatives": negatives, "bid_down": bid_down, "hold": hold}


def negative_keyword_rows_with_match_types(classified: Dict[str, Any], campaign_id: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    for item in classified.get("negatives", []):
        if int(item.get("campaign_id") or 0) != int(campaign_id):
            continue
        term = normalize_term(item.get("matched_phrase") or item.get("term"))
        if not term or term in seen:
            continue
        seen.add(term)
        rows.append({
            "campaignId": str(campaign_id),
            "keywordText": term,
            "matchType": item.get("negative_match_type") or "NEGATIVE_EXACT",
            "state": "ENABLED",
        })
    return rows


def apply_negatives_step_with_match_types(client: Any, classified: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Drop-in replacement for optimize_campaigns.apply_negatives_step."""
    negatives_applied: List[Dict[str, Any]] = []
    if not classified.get("negatives"):
        return negatives_applied

    campaigns = sorted({int(item.get("campaign_id") or 0) for item in classified["negatives"] if item.get("campaign_id")})
    for campaign_id in campaigns:
        rows = negative_keyword_rows_with_match_types(classified, campaign_id)
        if not rows:
            continue
        client.create_negative_keywords(rows)
        negatives_applied.append({
            "campaign_id": campaign_id,
            "count": len(rows),
            "terms_sample": [row["keywordText"] for row in rows[:10]],
            "match_types_sample": [row["matchType"] for row in rows[:10]],
        })
    return negatives_applied


def summarize_classification(classified: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    total_spend = 0.0
    total_sales = 0.0
    out: Dict[str, Any] = {}
    for bucket in ("winners", "negatives", "bid_down", "hold"):
        rows = classified.get(bucket, []) or []
        spend = round(sum(float(r.get("cost") or 0) for r in rows), 2)
        sales = round(sum(float(r.get("sales") or 0) for r in rows), 2)
        total_spend += spend
        total_sales += sales
        out[bucket] = {
            "count": len(rows),
            "spend": spend,
            "sales": sales,
            "acos": round(spend / sales, 4) if sales else None,
            "top_terms": [r.get("term") for r in sorted(rows, key=lambda r: float(r.get("cost") or 0), reverse=True)[:10]],
        }
    out["total"] = {
        "spend": round(total_spend, 2),
        "sales": round(total_sales, 2),
        "acos": round(total_spend / total_sales, 4) if total_sales else None,
    }
    return out
