"""
Campaign Optimizer - Enhanced with Amazon Suggested Bids + Peak/Off-Peak Logic
"""

import csv
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


# =============================
# PERFORMANCE TIERS
# =============================
class PerformanceTier(Enum):
    TOP_PERFORMER = "top_performer"
    PROFITABLE = "profitable"
    MARGINAL = "marginal"
    UNPROFITABLE = "unprofitable"
    ZERO_PERFORMANCE = "zero_performance"


# =============================
# DATA STRUCTURES
# =============================
@dataclass
class CampaignMetrics:
    campaign_id: str
    campaign_name: str
    status: str
    impressions: int
    clicks: int
    cost: float
    sales: float
    purchases: int
    acos: float
    roas: float
    current_budget: float
    ctr: float = 0.0
    cpc: float = 0.0

    # NEW: Amazon suggested bids
    suggested_bid_low: float = 0.0
    suggested_bid_high: float = 0.0


@dataclass
class OptimizationAction:
    campaign_id: str
    campaign_name: str
    tier: PerformanceTier
    current_budget: float
    recommended_budget: float
    action: str
    reason: str
    priority: int

    # NEW: Bid logic output
    amazonSuggestedBidLow: float = 0.0
    amazonSuggestedBidHigh: float = 0.0
    currentBidMode: str = "UNKNOWN"
    currentAppliedBid: float = 0.0


# =============================
# OPTIMIZER
# =============================
class CampaignOptimizer:

    # PERFORMANCE THRESHOLDS
    TOP_PERFORMER_ROAS_MIN = 3.0
    TOP_PERFORMER_ACOS_MAX = 0.35

    PROFITABLE_ROAS_MIN = 1.5
    PROFITABLE_ACOS_MAX = 0.65

    MARGINAL_ROAS_MIN = 0.5
    MARGINAL_ACOS_MAX = 1.5

    # BUDGET SCALING (FIXED - was too aggressive)
    TOP_PERFORMER_SCALE = 1.3
    PROFITABLE_SCALE = 1.15
    MARGINAL_SCALE = 0.85

    TEST_BUDGET = 5.0

    MIN_IMPRESSIONS = 100
    MIN_CLICKS = 5

    # =============================
    # TIME-BASED BID SETTINGS
    # =============================
    USE_AMAZON_SUGGESTED_BIDS = True

    PEAK_HOURS_START = 18   # 6 PM
    PEAK_HOURS_END = 23     # 11 PM

    OFF_PEAK_START = 0
    OFF_PEAK_END = 6

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        if config:
            for key, value in config.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    # =============================
    # TIME DETECTION
    # =============================
    def get_bid_mode(self) -> str:
        hour = datetime.now().hour

        if self.PEAK_HOURS_START <= hour <= self.PEAK_HOURS_END:
            return "PEAK"

        if self.OFF_PEAK_START <= hour <= self.OFF_PEAK_END:
            return "OFF_PEAK"

        return "NORMAL"

    # =============================
    # BID SELECTION
    # =============================
    def select_bid(self, metrics: CampaignMetrics) -> (float, str):
        mode = self.get_bid_mode()

        if not self.USE_AMAZON_SUGGESTED_BIDS:
            return metrics.cpc or 0.5, "STATIC"

        if mode == "PEAK":
            return metrics.suggested_bid_high or metrics.cpc or 0.75, "PEAK"

        if mode == "OFF_PEAK":
            return metrics.suggested_bid_low or metrics.cpc or 0.3, "OFF_PEAK"

        # normal
        avg = (metrics.suggested_bid_low + metrics.suggested_bid_high) / 2
        return avg or metrics.cpc or 0.5, "NORMAL"

    # =============================
    # CLASSIFICATION
    # =============================
    def classify_campaign(self, metrics: CampaignMetrics) -> PerformanceTier:

        if metrics.impressions < self.MIN_IMPRESSIONS:
            return PerformanceTier.ZERO_PERFORMANCE

        if metrics.clicks >= 20 and metrics.purchases == 0:
            return PerformanceTier.UNPROFITABLE

        if metrics.roas >= self.TOP_PERFORMER_ROAS_MIN and metrics.acos <= self.TOP_PERFORMER_ACOS_MAX:
            return PerformanceTier.TOP_PERFORMER

        if metrics.roas >= self.PROFITABLE_ROAS_MIN and metrics.acos <= self.PROFITABLE_ACOS_MAX:
            return PerformanceTier.PROFITABLE

        if metrics.roas >= self.MARGINAL_ROAS_MIN:
            return PerformanceTier.MARGINAL

        return PerformanceTier.UNPROFITABLE

    # =============================
    # BUDGET CALC
    # =============================
    def calculate_recommended_budget(self, metrics, tier):

        current = metrics.current_budget

        if tier == PerformanceTier.TOP_PERFORMER:
            return round(current * self.TOP_PERFORMER_SCALE, 2)

        if tier == PerformanceTier.PROFITABLE:
            return round(current * self.PROFITABLE_SCALE, 2)

        if tier == PerformanceTier.MARGINAL:
            return round(current * self.MARGINAL_SCALE, 2)

        if tier == PerformanceTier.ZERO_PERFORMANCE:
            return self.TEST_BUDGET

        return 0.0

    # =============================
    # ACTION GENERATOR
    # =============================
    def generate_optimization_action(self, metrics: CampaignMetrics):

        tier = self.classify_campaign(metrics)
        recommended_budget = self.calculate_recommended_budget(metrics, tier)

        bid_value, bid_mode = self.select_bid(metrics)

        if tier == PerformanceTier.TOP_PERFORMER:
            action = "scale_up"
            priority = 1
            reason = "High ROAS + efficient spend"

        elif tier == PerformanceTier.PROFITABLE:
            action = "scale_up"
            priority = 2
            reason = "Consistent profit"

        elif tier == PerformanceTier.MARGINAL:
            action = "optimize"
            priority = 3
            reason = "Needs refinement"

        elif tier == PerformanceTier.UNPROFITABLE:
            action = "pause"
            priority = 1
            reason = "Spending without return"

        else:
            action = "test"
            priority = 4
            reason = "Low data"

        return OptimizationAction(
            campaign_id=metrics.campaign_id,
            campaign_name=metrics.campaign_name,
            tier=tier,
            current_budget=metrics.current_budget,
            recommended_budget=recommended_budget,
            action=action,
            reason=reason,
            priority=priority,

            # NEW OUTPUT
            amazonSuggestedBidLow=metrics.suggested_bid_low,
            amazonSuggestedBidHigh=metrics.suggested_bid_high,
            currentBidMode=bid_mode,
            currentAppliedBid=round(bid_value, 2),
        )

    # =============================
    def optimize_campaign_set(self, campaigns):
        actions = [self.generate_optimization_action(c) for c in campaigns]
        return sorted(actions, key=lambda x: x.priority)


# =============================
# CSV PARSER (UPDATED)
# =============================
def parse_campaign_csv(csv_content: str):

    campaigns = []
    reader = csv.DictReader(csv_content.splitlines())

    for row in reader:
        try:
            def f(x):
                return float(str(x).replace("$","").replace(",","") or 0)

            def i(x):
                return int(float(x or 0))

            campaigns.append(
                CampaignMetrics(
                    campaign_id=row.get("Campaign name",""),
                    campaign_name=row.get("Campaign name",""),
                    status=row.get("Status",""),
                    impressions=i(row.get("Impressions")),
                    clicks=i(row.get("Clicks")),
                    cost=f(row.get("Total cost")),
                    sales=f(row.get("Sales")),
                    purchases=i(row.get("Purchases")),
                    acos=f(row.get("ACOS")),
                    roas=f(row.get("ROAS")),
                    current_budget=f(row.get("Campaign budget amount",25)),

                    # NEW: OPTIONAL FIELDS
                    suggested_bid_low=f(row.get("Suggested bid (low)", 0)),
                    suggested_bid_high=f(row.get("Suggested bid (high)", 0)),
                )
            )
        except Exception as e:
            logger.warning(f"Parse error: {e}")

    return campaigns


# =============================
# REPORT (UNCHANGED)
# =============================
def format_optimization_report(actions, summary):
    return f"Processed {len(actions)} campaigns"
