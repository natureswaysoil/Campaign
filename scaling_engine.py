"""Profit-aware scaling logic for Amazon PPC optimization.

Rules are intentionally conservative by default:
- Scale budgets only when sales exist and ACOS is safely under target.
- Cut bids/budgets when ACOS is high or spend is wasted.
- Keep decisions explainable so the dashboard can show exactly why a change was made.
"""
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


def decide_budget_scaling(
    campaign: Dict[str, Any],
    *,
    target_acos: float = 0.35,
    min_orders_to_scale: int = 2,
    min_clicks_to_judge: int = 15,
    max_budget_increase_pct: float = 0.20,
    max_budget_decrease_pct: float = 0.20,
    dry_run: bool = True,
) -> ScalingDecision:
    campaign_id = str(campaign.get("campaignId") or campaign.get("campaign_id") or campaign.get("id") or "unknown")
    current_budget = _num(campaign, "budget", "dailyBudget", "daily_budget", default=0.0)
    spend = _num(campaign, "spend", "cost", default=0.0)
    sales = _num(campaign, "sales", "sales7d", "sales14d", default=0.0)
    clicks = int(_num(campaign, "clicks", default=0.0))
    orders = int(_num(campaign, "orders", "purchases7d", "purchases14d", default=0.0))
    acos = calculate_acos(spend, sales)

    if current_budget <= 0:
        return ScalingDecision("campaign", campaign_id, "hold", current_budget, current_budget, "No campaign budget available.", "low", dry_run)

    if orders >= min_orders_to_scale and acos is not None and acos <= target_acos * 0.75:
        new_budget = round(current_budget * (1 + max_budget_increase_pct), 2)
        return ScalingDecision("campaign", campaign_id, "increase_budget", current_budget, new_budget, f"ACOS {acos:.1%} is well below target {target_acos:.1%} with {orders} orders.", "high", dry_run)

    if orders >= 1 and acos is not None and acos <= target_acos:
        new_budget = round(current_budget * 1.10, 2)
        return ScalingDecision("campaign", campaign_id, "increase_budget", current_budget, new_budget, f"ACOS {acos:.1%} is below target {target_acos:.1%}; modest budget scale.", "medium", dry_run)

    if clicks >= min_clicks_to_judge and orders == 0:
        new_budget = round(current_budget * (1 - max_budget_decrease_pct), 2)
        return ScalingDecision("campaign", campaign_id, "decrease_budget", current_budget, new_budget, f"{clicks} clicks with zero orders; reduce budget exposure.", "high", dry_run)

    if acos is not None and acos > target_acos * 1.25:
        new_budget = round(current_budget * (1 - max_budget_decrease_pct), 2)
        return ScalingDecision("campaign", campaign_id, "decrease_budget", current_budget, new_budget, f"ACOS {acos:.1%} is above target {target_acos:.1%}; reduce budget.", "medium", dry_run)

    return ScalingDecision("campaign", campaign_id, "hold", current_budget, current_budget, "Performance does not justify budget change yet.", "medium", dry_run)


def decide_keyword_bid_scaling(
    keyword: Dict[str, Any],
    *,
    target_acos: float = 0.35,
    min_bid: float = 0.25,
    max_bid: float = 1.75,
    dry_run: bool = True,
) -> ScalingDecision:
    keyword_id = str(keyword.get("keywordId") or keyword.get("keyword_id") or keyword.get("id") or keyword.get("keywordText") or "unknown")
    current_bid = _num(keyword, "bid", "currentBid", "current_bid", default=0.0)
    spend = _num(keyword, "spend", "cost", default=0.0)
    sales = _num(keyword, "sales", "sales7d", "sales14d", default=0.0)
    clicks = int(_num(keyword, "clicks", default=0.0))
    orders = int(_num(keyword, "orders", "purchases7d", "purchases14d", default=0.0))
    acos = calculate_acos(spend, sales)

    if current_bid <= 0:
        return ScalingDecision("keyword", keyword_id, "hold", current_bid, current_bid, "No keyword bid available.", "low", dry_run)

    if orders >= 2 and acos is not None and acos <= target_acos * 0.70:
        new_bid = round(clamp(current_bid * 1.15, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "increase_bid", current_bid, new_bid, f"Keyword is a winner: {orders} orders and ACOS {acos:.1%}.", "high", dry_run)

    if orders >= 1 and acos is not None and acos <= target_acos:
        new_bid = round(clamp(current_bid * 1.08, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "increase_bid", current_bid, new_bid, f"Keyword is profitable below target ACOS {target_acos:.1%}.", "medium", dry_run)

    if clicks >= 15 and orders == 0:
        new_bid = round(clamp(current_bid * 0.75, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "decrease_bid", current_bid, new_bid, f"{clicks} clicks with zero orders; reduce bid.", "high", dry_run)

    if acos is not None and acos > target_acos * 1.25:
        new_bid = round(clamp(current_bid * 0.80, min_bid, max_bid), 2)
        return ScalingDecision("keyword", keyword_id, "decrease_bid", current_bid, new_bid, f"ACOS {acos:.1%} is above target {target_acos:.1%}.", "medium", dry_run)

    return ScalingDecision("keyword", keyword_id, "hold", current_bid, current_bid, "Not enough signal for bid change.", "medium", dry_run)


def build_scaling_plan(
    campaigns: List[Dict[str, Any]],
    keywords: List[Dict[str, Any]],
    *,
    target_acos: float = 0.35,
    dry_run: bool = True,
) -> Dict[str, Any]:
    campaign_decisions = [decide_budget_scaling(c, target_acos=target_acos, dry_run=dry_run).to_dict() for c in campaigns]
    keyword_decisions = [decide_keyword_bid_scaling(k, target_acos=target_acos, dry_run=dry_run).to_dict() for k in keywords]
    all_decisions = campaign_decisions + keyword_decisions
    return {
        "dry_run": dry_run,
        "target_acos": target_acos,
        "summary": {
            "increase_budget": sum(1 for d in all_decisions if d["action"] == "increase_budget"),
            "decrease_budget": sum(1 for d in all_decisions if d["action"] == "decrease_budget"),
            "increase_bid": sum(1 for d in all_decisions if d["action"] == "increase_bid"),
            "decrease_bid": sum(1 for d in all_decisions if d["action"] == "decrease_bid"),
            "hold": sum(1 for d in all_decisions if d["action"] == "hold"),
        },
        "campaign_decisions": campaign_decisions,
        "keyword_decisions": keyword_decisions,
    }
