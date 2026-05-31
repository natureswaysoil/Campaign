"""Profit-aware + Growth-oriented scaling logic for Amazon PPC."""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ScalingDecision:
    entity_type: str
    entity_id: str
    action: str
    current_value: Optional[float]
    new_value: Optional[float]
    reason: str
    confidence: str
    dry_run: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _num(row: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(str(value).replace("$", "").replace(",", "").strip())
            except Exception:
                pass
    return default


def calculate_acos(spend: float, sales: float) -> Optional[float]:
    if sales <= 0:
        return None
    return spend / sales


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def decide_keyword_bid_scaling(
    keyword: Dict[str, Any],
    *,
    target_acos: float = 0.35,
    min_bid: float = 0.25,
    max_bid: float = 2.50,          # Increased ceiling
    conversion_rate_threshold: float = 0.12,
    dry_run: bool = True,
) -> ScalingDecision:
    keyword_id = str(keyword.get("keywordId") or keyword.get("keyword_id") or keyword.get("id") or keyword.get("keywordText") or "unknown")
    
    current_bid = _num(keyword, "bid", "currentBid", "current_bid", default=0.0)
    spend = _num(keyword, "spend", "cost", default=0.0)
    sales = _num(keyword, "sales", "sales7d", "sales14d", default=0.0)
    clicks = int(_num(keyword, "clicks", default=0.0))
    orders = int(_num(keyword, "orders", "purchases7d", "purchases14d", default=0.0))
    acos = calculate_acos(spend, sales)
    
    # Calculate conversion rate if possible
    conv_rate = (orders / clicks) if clicks > 0 else 0.0

    if current_bid <= 0:
        return ScalingDecision("keyword", keyword_id, "hold", current_bid, current_bid, "No keyword bid available.", "low", dry_run)

    # === AGGRESSIVE WINNER SCALING ===
    if orders >= 3 and acos is not None and acos <= target_acos * 0.90:
        multiplier = 1.30 if conv_rate >= conversion_rate_threshold else 1.22
        new_bid = round(clamp(current_bid * multiplier, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "increase_bid", current_bid, new_bid,
                             f"STRONG WINNER: {orders} orders, ACOS {acos:.1%}, CR {conv_rate:.1%} → Aggressive scale", "high", dry_run)

    if orders >= 2 and acos is not None and acos <= target_acos * 0.85:
        new_bid = round(clamp(current_bid * 1.20, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "increase_bid", current_bid, new_bid,
                             f"Good winner: {orders} orders at ACOS {acos:.1%}", "high", dry_run)

    if orders >= 1 and acos is not None and acos <= target_acos * 1.05:   # Allow scaling even near target if volume is good
        new_bid = round(clamp(current_bid * 1.12, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "increase_bid", current_bid, new_bid,
                             f"Volume ramp: {orders} orders near target ACOS", "medium", dry_run)

    # Downscaling logic (kept conservative)
    if clicks >= 12 and orders == 0:
        new_bid = round(clamp(current_bid * 0.72, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "decrease_bid", current_bid, new_bid, f"{clicks} clicks, zero orders", "high", dry_run)

    if acos is not None and acos > target_acos * 1.30:
        new_bid = round(clamp(current_bid * 0.78, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "decrease_bid", current_bid, new_bid, f"High ACOS {acos:.1%}", "medium", dry_run)

    return ScalingDecision("keyword", keyword_id, "hold", current_bid, current_bid, "Not enough signal for change.", "medium", dry_run)
