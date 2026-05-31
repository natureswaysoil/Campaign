"""Scaling Engine - Aggressive winner scaling + cash protection"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any

def money(value, default=0.0):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except:
        return default

@dataclass
class BidDecision:
    action: str
    new_bid: float
    reason: str

class ScalingEngine:
    def __init__(self, target_acos: float = 0.35, max_bid: float = 3.50, min_bid: float = 0.30):
        self.target_acos = target_acos
        self.max_bid = max_bid
        self.min_bid = min_bid

    def decide_bid(self, row: Dict[str, Any], current_bid: float) -> BidDecision:
        spend = money(row.get("spend") or row.get("cost"), 0)
        orders = int(row.get("orders", 0) or row.get("purchases7d", 0))
        acos = money(row.get("acos"), 999)
        clicks = int(row.get("clicks", 0))
        conv_rate = (orders / clicks) if clicks > 0 else 0.0

        # CASH PROTECTION (your $8k balance)
        if spend > 120 and orders < 2:
            new_bid = round(max(current_bid * 0.60, self.min_bid), 2)
            return BidDecision("decrease", new_bid, "CASH PROTECTION - high spend, low orders")

        # AGGRESSIVE GROWTH
        if orders >= 3 and acos <= self.target_acos * 0.90:
            multiplier = 1.32 if conv_rate >= 0.13 else 1.24
            new_bid = round(min(current_bid * multiplier, self.max_bid), 2)
        elif orders >= 2 and acos <= self.target_acos * 0.95:
            new_bid = round(min(current_bid * 1.22, self.max_bid), 2)
        elif orders >= 1 and acos <= self.target_acos * 1.08:
            new_bid = round(min(current_bid * 1.15, self.max_bid), 2)
        elif acos > self.target_acos * 1.45 or (clicks > 15 and orders == 0):
            new_bid = round(max(current_bid * 0.75, self.min_bid), 2)
        else:
            new_bid = current_bid

        action = "increase" if new_bid > current_bid else "decrease" if new_bid < current_bid else "hold"
        return BidDecision(action, new_bid, f"Orders:{orders} ACOS:{acos:.1%} CR:{conv_rate:.1%}")
