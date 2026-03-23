"""
Campaign Optimizer - Implements automatic budget adjustments based on performance tiers.
"""

import csv
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger(__name__)


class PerformanceTier(Enum):
    """Campaign performance tier classification"""
    TOP_PERFORMER = "top_performer"
    PROFITABLE = "profitable"
    MARGINAL = "marginal"
    UNPROFITABLE = "unprofitable"
    ZERO_PERFORMANCE = "zero_performance"


@dataclass
class CampaignMetrics:
    """Campaign performance metrics"""
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


@dataclass
class OptimizationAction:
    """Recommended optimization action"""
    campaign_id: str
    campaign_name: str
    tier: PerformanceTier
    current_budget: float
    recommended_budget: float
    action: str
    reason: str
    priority: int


class CampaignOptimizer:
    """Optimizes Amazon PPC campaigns based on performance data."""
    
    TOP_PERFORMER_ROAS_MIN = 3.0
    TOP_PERFORMER_ACOS_MAX = 0.35
    PROFITABLE_ROAS_MIN = 1.0
    PROFITABLE_ACOS_MAX = 1.0
    MARGINAL_ROAS_MIN = 0.5
    MARGINAL_ACOS_MAX = 2.0
    
    TOP_PERFORMER_SCALE = 3.0
    PROFITABLE_SCALE = 2.0
    MARGINAL_SCALE = 0.7
    TEST_BUDGET = 5.0
    
    MIN_IMPRESSIONS = 100
    MIN_CLICKS = 5
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        if config:
            for key, value in config.items():
                if hasattr(self, key):
                    setattr(self, key, value)
    
    def classify_campaign(self, metrics: CampaignMetrics) -> PerformanceTier:
        if metrics.impressions < self.MIN_IMPRESSIONS or metrics.purchases == 0:
            return PerformanceTier.ZERO_PERFORMANCE
        if metrics.roas >= self.TOP_PERFORMER_ROAS_MIN and metrics.acos <= self.TOP_PERFORMER_ACOS_MAX:
            return PerformanceTier.TOP_PERFORMER
        if metrics.roas >= self.PROFITABLE_ROAS_MIN and metrics.acos <= self.PROFITABLE_ACOS_MAX:
            return PerformanceTier.PROFITABLE
        if metrics.roas >= self.MARGINAL_ROAS_MIN and metrics.acos <= self.MARGINAL_ACOS_MAX:
            return PerformanceTier.MARGINAL
        return PerformanceTier.UNPROFITABLE
    
    def calculate_recommended_budget(self, metrics: CampaignMetrics, tier: PerformanceTier) -> float:
        current = metrics.current_budget
        if tier == PerformanceTier.TOP_PERFORMER:
            return round(current * self.TOP_PERFORMER_SCALE, 2)
        elif tier == PerformanceTier.PROFITABLE:
            return round(current * self.PROFITABLE_SCALE, 2)
        elif tier == PerformanceTier.MARGINAL:
            return round(current * self.MARGINAL_SCALE, 2)
        elif tier == PerformanceTier.ZERO_PERFORMANCE:
            return self.TEST_BUDGET
        else:
            return 0.0
    
    def generate_optimization_action(self, metrics: CampaignMetrics) -> OptimizationAction:
        tier = self.classify_campaign(metrics)
        recommended_budget = self.calculate_recommended_budget(metrics, tier)
        
        if tier == PerformanceTier.TOP_PERFORMER:
            action = "scale_up"
            reason = f"High ROAS ({metrics.roas:.2f}) and low ACOS ({metrics.acos:.1%}). Scale aggressively."
            priority = 1
        elif tier == PerformanceTier.PROFITABLE:
            action = "scale_up"
            reason = f"Profitable with ROAS {metrics.roas:.2f}. Increase budget to capture more sales."
            priority = 2
        elif tier == PerformanceTier.MARGINAL:
            action = "scale_down"
            reason = f"Marginal performance (ACOS {metrics.acos:.1%}). Reduce budget and optimize."
            priority = 3
        elif tier == PerformanceTier.UNPROFITABLE:
            action = "pause"
            reason = f"Unprofitable: ACOS {metrics.acos:.1%}. Pause and review targeting."
            priority = 1
        else:
            if metrics.impressions == 0:
                action = "test"
                reason = "No impressions. Verify product availability and increase bids."
                priority = 4
            else:
                action = "monitor"
                reason = f"{metrics.impressions} impressions but no sales. Monitor or test at low budget."
                priority = 5
        
        return OptimizationAction(
            campaign_id=metrics.campaign_id,
            campaign_name=metrics.campaign_name,
            tier=tier,
            current_budget=metrics.current_budget,
            recommended_budget=recommended_budget,
            action=action,
            reason=reason,
            priority=priority
        )
    
    def optimize_campaign_set(self, campaigns: List[CampaignMetrics]) -> List[OptimizationAction]:
        actions = []
        for campaign in campaigns:
            action = self.generate_optimization_action(campaign)
            actions.append(action)
        actions.sort(key=lambda x: (x.priority, -abs(x.recommended_budget - x.current_budget)))
        return actions
    
    def generate_budget_reallocation_summary(self, actions: List[OptimizationAction]) -> Dict[str, Any]:
        current_total = sum(a.current_budget for a in actions)
        recommended_total = sum(a.recommended_budget for a in actions)
        
        by_tier = {}
        for action in actions:
            tier_name = action.tier.value
            if tier_name not in by_tier:
                by_tier[tier_name] = {
                    "count": 0,
                    "current_budget": 0.0,
                    "recommended_budget": 0.0,
                }
            by_tier[tier_name]["count"] += 1
            by_tier[tier_name]["current_budget"] += action.current_budget
            by_tier[tier_name]["recommended_budget"] += action.recommended_budget
        
        paused_campaigns = [a for a in actions if a.action == "pause"]
        scaled_up_campaigns = [a for a in actions if a.action == "scale_up"]
        
        return {
            "total_campaigns": len(actions),
            "current_total_budget": round(current_total, 2),
            "recommended_total_budget": round(recommended_total, 2),
            "budget_change": round(recommended_total - current_total, 2),
            "budget_change_pct": round((recommended_total - current_total) / current_total * 100, 1) if current_total > 0 else 0,
            "by_tier": by_tier,
            "campaigns_to_pause": len(paused_campaigns),
            "campaigns_to_scale": len(scaled_up_campaigns),
            "freed_budget_from_paused": round(sum(a.current_budget for a in paused_campaigns), 2),
            "additional_budget_needed": round(sum(max(0, a.recommended_budget - a.current_budget) for a in scaled_up_campaigns), 2),
        }


def parse_campaign_csv(csv_content: str) -> List[CampaignMetrics]:
    campaigns = []
    reader = csv.DictReader(csv_content.splitlines())
    
    for row in reader:
        try:
            def safe_float(value: str, default: float = 0.0) -> float:
                if not value or value == "0":
                    return default
                cleaned = value.replace("%", "").replace("$", "").replace(",", "").strip()
                if cleaned.startswith("<"):
                    return 0.0
                try:
                    return float(cleaned)
                except ValueError:
                    return default
            
            def safe_int(value: str, default: int = 0) -> int:
                try:
                    return int(safe_float(value, default))
                except ValueError:
                    return default
            
            campaign_name = row.get("Campaign name", "")
            campaign_id = campaign_name
            
            metrics = CampaignMetrics(
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                status=row.get("Status", "UNKNOWN"),
                impressions=safe_int(row.get("Impressions", "0")),
                clicks=safe_int(row.get("Clicks", "0")),
                cost=safe_float(row.get("Total cost", "0")),
                sales=safe_float(row.get("Sales", "0")),
                purchases=safe_int(row.get("Purchases", "0")),
                acos=safe_float(row.get("ACOS", "0")),
                roas=safe_float(row.get("ROAS", "0")),
                current_budget=safe_float(row.get("Campaign budget amount", "25")),
                ctr=safe_float(row.get("CTR", "0")),
                cpc=safe_float(row.get("CPC", "0"))
            )
            
            campaigns.append(metrics)
            
        except Exception as e:
            logger.warning(f"Error parsing campaign row: {e}")
            continue
    
    return campaigns


def format_optimization_report(actions: List[OptimizationAction], summary: Dict[str, Any]) -> str:
    report = []
    report.append("=" * 80)
    report.append("CAMPAIGN OPTIMIZATION REPORT")
    report.append("=" * 80)
    report.append("")
    
    report.append("BUDGET REALLOCATION SUMMARY")
    report.append("-" * 80)
    report.append(f"Total Campaigns: {summary['total_campaigns']}")
    report.append(f"Current Total Budget: ${summary['current_total_budget']:.2f}/day")
    report.append(f"Recommended Budget: ${summary['recommended_total_budget']:.2f}/day")
    report.append(f"Change: ${summary['budget_change']:.2f} ({summary['budget_change_pct']:+.1f}%)")
    report.append("")
    report.append(f"Campaigns to Pause: {summary['campaigns_to_pause']}")
    report.append(f"Budget Freed: ${summary['freed_budget_from_paused']:.2f}")
    report.append(f"Campaigns to Scale: {summary['campaigns_to_scale']}")
    report.append(f"Additional Budget Needed: ${summary['additional_budget_needed']:.2f}")
    report.append("")
    
    report.append("PERFORMANCE BY TIER")
    report.append("-" * 80)
    for tier_name, data in summary['by_tier'].items():
        report.append(f"{tier_name.upper()}: {data['count']} campaigns")
        report.append(f"  Current: ${data['current_budget']:.2f} → Recommended: ${data['recommended_budget']:.2f}")
    report.append("")
    
    critical_actions = [a for a in actions if a.priority == 1]
    if critical_actions:
        report.append("PRIORITY 1 ACTIONS (IMMEDIATE)")
        report.append("-" * 80)
        for action in critical_actions:
            report.append(f"\nCampaign: {action.campaign_name}")
            report.append(f"Action: {action.action.upper()}")
            report.append(f"Budget: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}")
            report.append(f"Reason: {action.reason}")
        report.append("")
    
    report.append("ALL OPTIMIZATION ACTIONS")
    report.append("-" * 80)
    
    for priority in sorted(set(a.priority for a in actions)):
        priority_actions = [a for a in actions if a.priority == priority]
        report.append(f"\nPriority {priority}:")
        for action in priority_actions:
            report.append(f"  • {action.campaign_name}")
            report.append(f"    {action.action.upper()}: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}")
            report.append(f"    {action.reason}")
    
    report.append("")
    report.append("=" * 80)
    
    return "\n".join(report)
