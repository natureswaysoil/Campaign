"""Preview-first optimized Sponsored Products launch planner.

This module is intentionally side-effect free: it builds the campaign, keyword,
and bid plan that should be reviewed before any Amazon Ads objects are created.
"""

import re
from typing import Any, Dict, List, Optional

STOP_WORDS = {
    "and", "or", "with", "for", "the", "a", "an", "to", "of", "in", "on",
    "by", "from", "up", "more", "all", "new", "premium", "organic", "natural",
    "natures", "nature", "way", "soil", "llc", "oz", "ounce", "gallon",
    "gallons", "lb", "lbs", "pound", "pounds", "concentrate", "liquid",
}

PRODUCT_KEYWORD_THEMES = [
    ("dog", ["dog urine neutralizer", "dog urine lawn repair", "pet urine grass repair", "yellow spot lawn repair"]),
    ("urine", ["dog urine neutralizer", "dog urine lawn repair", "pet urine grass repair", "yellow spot lawn repair"]),
    ("compost", ["living compost", "compost for garden", "worm castings compost", "biochar compost", "raised bed compost"]),
    ("worm", ["worm castings", "worm castings for plants", "worm castings compost"]),
    ("biochar", ["liquid biochar", "biochar soil amendment", "biochar for plants", "activated biochar"]),
    ("humic", ["humic acid for lawn", "humic acid fertilizer", "humic fulvic acid", "soil conditioner liquid"]),
    ("fulvic", ["fulvic acid for plants", "humic fulvic acid", "liquid soil conditioner"]),
    ("kelp", ["liquid kelp fertilizer", "seaweed fertilizer", "kelp fertilizer for plants", "kelp for garden"]),
    ("bone", ["liquid bone meal", "bone meal fertilizer", "phosphorus fertilizer", "calcium fertilizer for plants"]),
    ("pasture", ["pasture fertilizer", "hay fertilizer", "liquid pasture fertilizer", "lawn and pasture fertilizer"]),
    ("hay", ["hay fertilizer", "pasture fertilizer", "liquid hay fertilizer"]),
    ("lawn", ["liquid lawn fertilizer", "lawn fertilizer concentrate", "grass fertilizer liquid"]),
    ("fruit", ["fruit tree fertilizer", "apple tree fertilizer", "peach tree fertilizer", "citrus tree fertilizer"]),
    ("tree", ["fruit tree fertilizer", "tree fertilizer", "root growth fertilizer for trees"]),
    ("hydroponic", ["hydroponic fertilizer", "hydroponic nutrients", "liquid plant food hydroponics"]),
]


def clean_keyword(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9\s\-]", " ", value or "").lower()
    value = re.sub(r"\s+", " ", value).strip()
    return value[:80]


def unique(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        item = clean_keyword(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def title_phrases(title: str) -> List[str]:
    words = [w for w in clean_keyword(title).split() if w not in STOP_WORDS and len(w) > 2]
    phrases: List[str] = []
    for size in (2, 3, 4):
        for i in range(0, max(0, len(words) - size + 1)):
            phrases.append(" ".join(words[i:i + size]))
    return phrases


def generate_launch_keywords(product: Dict[str, Any], manual_keywords: Optional[List[str]] = None, max_keywords: int = 18) -> List[str]:
    title = str(product.get("title") or product.get("name") or product.get("productName") or "")
    seed_text = clean_keyword(" ".join([title, str(product.get("sku") or "")]))

    candidates: List[str] = []
    candidates.extend(manual_keywords or [])
    candidates.extend(title_phrases(title))

    for trigger, kws in PRODUCT_KEYWORD_THEMES:
        if trigger in seed_text:
            candidates.extend(kws)

    # Keep phrases that look buyer-intent rather than one-word fragments.
    filtered = [kw for kw in unique(candidates) if len(kw.split()) >= 2]
    return filtered[:max_keywords]


def clamp_bid(value: float, min_bid: float = 0.25, max_bid: float = 1.50) -> float:
    return round(max(min_bid, min(max_bid, float(value))), 2)


def build_bid_plan(keyword: str, rec: Optional[Dict[str, Any]], fallback_bid: float, mode: str) -> Dict[str, Any]:
    rec = rec or {}
    low = float(rec.get("low") or 0)
    high = float(rec.get("high") or 0)
    suggested = float(rec.get("suggested") or 0)

    if suggested > 0:
        base = suggested
    elif low > 0 and high > 0:
        base = (low + high) / 2
    else:
        base = fallback_bid

    if mode == "PEAK" and high > 0:
        base = min(base * 1.05, high)
    elif mode == "OFF_PEAK" and low > 0:
        base = max(low, base * 0.90)

    base = clamp_bid(base)
    return {
        "keyword": keyword,
        "amazon_suggested_low": round(low, 2) if low else None,
        "amazon_suggested_mid": round(suggested, 2) if suggested else None,
        "amazon_suggested_high": round(high, 2) if high else None,
        "phrase_bid": base,
        "exact_bid": clamp_bid(base * 1.15),
        "broad_bid": clamp_bid(base * 0.80),
        "bid_mode": mode,
        "source": "amazon_recommendation" if (low or high or suggested) else "fallback",
    }


def build_optimized_launch_preview(
    product: Dict[str, Any],
    manual_keywords: Optional[List[str]] = None,
    budget: float = 10.0,
    fallback_bid: float = 0.75,
    bid_mode: str = "NORMAL",
    bid_recommendations: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    title = str(product.get("title") or product.get("name") or product.get("productName") or "Untitled Product")
    asin = str(product.get("asin") or product.get("ASIN") or "")
    sku = str(product.get("sku") or product.get("sellerSku") or product.get("SKU") or "")
    safe_budget = round(max(3.0, min(50.0, float(budget or 10.0))), 2)
    safe_fallback = clamp_bid(fallback_bid)

    keywords = generate_launch_keywords(product, manual_keywords)
    bid_recommendations = bid_recommendations or {}
    bid_plan = [
        build_bid_plan(kw, bid_recommendations.get(kw), safe_fallback, bid_mode)
        for kw in keywords
    ]

    manual_core_keywords = bid_plan[:12]
    broad_research_keywords = bid_plan[:10]

    return {
        "dry_run": True,
        "message": "Preview only. No Amazon Ads campaigns, ad groups, ads, or keywords were created.",
        "product": {"title": title, "asin": asin, "sku": sku},
        "settings": {
            "daily_budget_total": safe_budget,
            "fallback_bid": safe_fallback,
            "bid_mode": bid_mode,
            "max_bid_guardrail": 1.50,
            "min_bid_guardrail": 0.25,
        },
        "campaigns": [
            {
                "name": f"NWS - {title[:55]} - Auto - Discovery",
                "type": "auto_discovery",
                "daily_budget": round(max(3.0, safe_budget * 0.30), 2),
                "starting_bid": clamp_bid(safe_fallback * 0.75),
                "purpose": "Find converting customer search terms safely.",
            },
            {
                "name": f"NWS - {title[:55]} - Manual - Core",
                "type": "manual_exact_phrase",
                "daily_budget": round(max(3.0, safe_budget * 0.50), 2),
                "keywords": [
                    {"keyword": b["keyword"], "exact_bid": b["exact_bid"], "phrase_bid": b["phrase_bid"], "source": b["source"]}
                    for b in manual_core_keywords
                ],
                "purpose": "Launch strongest buyer-intent terms as Exact and Phrase.",
            },
            {
                "name": f"NWS - {title[:55]} - Broad - Research",
                "type": "manual_broad_research",
                "daily_budget": round(max(3.0, safe_budget * 0.20), 2),
                "keywords": [
                    {"keyword": b["keyword"], "broad_bid": b["broad_bid"], "source": b["source"]}
                    for b in broad_research_keywords
                ],
                "purpose": "Explore related searches at lower bids.",
            },
        ],
        "bid_plan": bid_plan,
        "review_checklist": [
            "Remove keywords that do not describe the product exactly.",
            "Lower bids for heavy/low-margin products such as bagged compost.",
            "Keep the Auto Discovery budget small until converting search terms appear.",
            "Use negative keywords after 20+ clicks with zero orders.",
        ],
    }
