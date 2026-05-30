"""
Campaign Optimizer - Amazon Ads API connected
Uses:
- campaign CSV metrics for classification
- Amazon Ads API for bid recommendations and bid updates
- peak/off-peak bid application

IMPORTANT:
Set AMAZON_ADS_BID_RECOMMENDATIONS_ENDPOINT in env to the bid recommendation
path from your current Amazon Ads API docs/account.
Example placeholder only:
    /sp/targets/bidRecommendations

Do not assume the placeholder path is correct for your account/version
without verifying in Amazon Ads developer docs.
"""

import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class PerformanceTier(Enum):
    TOP_PERFORMER = "top_performer"
    PROFITABLE = "profitable"
    MARGINAL = "marginal"
    UNPROFITABLE = "unprofitable"
    ZERO_PERFORMANCE = "zero_performance"


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
    suggested_bid_low: float = 0.0
    suggested_bid_high: float = 0.0
    keyword_id: str = ""
    keyword_text: str = ""
    ad_group_id: str = ""


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
    amazonSuggestedBidLow: float = 0.0
    amazonSuggestedBidHigh: float = 0.0
    currentBidMode: str = "UNKNOWN"
    currentAppliedBid: float = 0.0
    keyword_id: str = ""
    keyword_text: str = ""
    ad_group_id: str = ""


class AmazonAdsClient:
    """
    Minimal Amazon Ads API client using OAuth refresh token flow.

    Required env vars:
    - AMAZON_ADS_CLIENT_ID
    - AMAZON_ADS_CLIENT_SECRET
    - AMAZON_ADS_REFRESH_TOKEN
    - AMAZON_ADS_PROFILE_ID

    Optional:
    - AMAZON_ADS_REGION = na|eu|fe
    - AMAZON_ADS_BID_RECOMMENDATIONS_ENDPOINT
    """

    TOKEN_URL = "https://api.amazon.com/auth/o2/token"
    BASE_URLS = {
        "na": "https://advertising-api.amazon.com",
        "eu": "https://advertising-api-eu.amazon.com",
        "fe": "https://advertising-api-fe.amazon.com",
    }

    def __init__(self) -> None:
        self.client_id = os.getenv("AMAZON_ADS_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("AMAZON_ADS_CLIENT_SECRET", "").strip()
        self.refresh_token = os.getenv("AMAZON_ADS_REFRESH_TOKEN", "").strip()
        self.profile_id = os.getenv("AMAZON_ADS_PROFILE_ID", "").strip()
        self.region = os.getenv("AMAZON_ADS_REGION", "na").strip().lower()

        if not all([self.client_id, self.client_secret, self.refresh_token, self.profile_id]):
            raise RuntimeError("Missing one or more Amazon Ads API credentials in environment.")

        if self.region not in self.BASE_URLS:
            raise RuntimeError("AMAZON_ADS_REGION must be one of: na, eu, fe")

        self.base_url = self.BASE_URLS[self.region]
        self.access_token = self._refresh_access_token()
        self.session = requests.Session()

    def _refresh_access_token(self) -> str:
        resp = requests.post(
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Amazon OAuth token response did not include access_token.")
        return token

    def headers(self, content_type: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Content-Type": content_type,
            "Accept": content_type,
        }

    def post(self, endpoint: str, body: Any, content_type: str = "application/json") -> Any:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.post(url, headers=self.headers(content_type), json=body, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"POST {endpoint} failed: {resp.status_code} {resp.text[:500]}")
        return resp.json() if resp.text.strip() else {}

    def put(self, endpoint: str, body: Any, content_type: str = "application/json") -> Any:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.put(url, headers=self.headers(content_type), json=body, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"PUT {endpoint} failed: {resp.status_code} {resp.text[:500]}")
        return resp.json() if resp.text.strip() else {}

    def get(self, endpoint: str, content_type: str = "application/json") -> Any:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.get(url, headers=self.headers(content_type), timeout=60)
        if not resp.ok:
            raise RuntimeError(f"GET {endpoint} failed: {resp.status_code} {resp.text[:500]}")
        return resp.json() if resp.text.strip() else {}

    def get_bid_recommendation(
        self,
        campaign_id: str,
        ad_group_id: str,
        keyword_text: str,
        match_type: str = "EXACT",
    ) -> Tuple[float, float]:
        """
        Calls a configurable Amazon Ads bid recommendation endpoint.

        You must set:
        AMAZON_ADS_BID_RECOMMENDATIONS_ENDPOINT

        because the exact path/version can vary and I could not confirm a current
        public endpoint path from Amazon’s public docs.
        """
        endpoint = os.getenv("AMAZON_ADS_BID_RECOMMENDATIONS_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError(
                "Missing AMAZON_ADS_BID_RECOMMENDATIONS_ENDPOINT. "
                "Set it from your current Amazon Ads developer docs."
            )

        payload = {
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "keywordText": keyword_text,
            "matchType": match_type,
        }

        data = self.post(endpoint, payload)

        # Flexible parsing because response shape may vary by endpoint/version.
        suggested = (
            data.get("suggestedBid")
            or data.get("suggested")
            or data.get("recommendedBid")
            or 0
        )
        low = (
            data.get("rangeStart")
            or data.get("suggestedBidLow")
            or data.get("min")
            or suggested
            or 0
        )
        high = (
            data.get("rangeEnd")
            or data.get("suggestedBidHigh")
            or data.get("max")
            or suggested
            or 0
        )

        return float(low or 0), float(high or 0)

    def update_keyword_bid(
        self,
        keyword_id: str,
        campaign_id: str,
        ad_group_id: str,
        bid: float,
        state: str = "ENABLED",
    ) -> Any:
        """
        Updates a Sponsored Products keyword bid.
        This uses the v3 keywords endpoint pattern already consistent with the
        rest of your existing codebase.
        """
        endpoint = "/sp/keywords"
        content_type = "application/vnd.spkeyword.v3+json"
        payload = {
            "keywords": [
                {
                    "keywordId": str(keyword_id),
                    "campaignId": str(campaign_id),
                    "adGroupId": str(ad_group_id),
                    "bid": round(float(bid), 2),
                    "state": state,
                }
            ]
        }
        return self.put(endpoint, payload, content_type=content_type)


class CampaignOptimizer:
    TOP_PERFORMER_ROAS_MIN = 4.0
    TOP_PERFORMER_ACOS_MAX = 0.25

    PROFITABLE_ROAS_MIN = 3.0
    PROFITABLE_ACOS_MAX = 0.33

    MARGINAL_ROAS_MIN = 2.0
    MARGINAL_ACOS_MAX = 0.50

    TOP_PERFORMER_SCALE = 1.20
    PROFITABLE_SCALE = 1.10
    MARGINAL_SCALE = 0.75
    TEST_BUDGET = 3.0

    MIN_IMPRESSIONS = 100
    MIN_CLICKS = 5

    USE_AMAZON_SUGGESTED_BIDS = True
    APPLY_LIVE_BID_UPDATES = False

    PEAK_HOURS_START = 18
    PEAK_HOURS_END = 23
    OFF_PEAK_START = 0
    OFF_PEAK_END = 6

    NORMAL_MODE_STRATEGY = "MIDPOINT"  # MIDPOINT | LOW | HIGH

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        if config:
            for key, value in config.items():
                if hasattr(self, key):
                    setattr(self, key, value)
        self.ads_client: Optional[AmazonAdsClient] = None
        if self.USE_AMAZON_SUGGESTED_BIDS or self.APPLY_LIVE_BID_UPDATES:
            self.ads_client = AmazonAdsClient()

    def get_bid_mode(self) -> str:
        hour = datetime.now().hour
        if self.PEAK_HOURS_START <= hour <= self.PEAK_HOURS_END:
            return "PEAK"
        if self.OFF_PEAK_START <= hour <= self.OFF_PEAK_END:
            return "OFF_PEAK"
        return "NORMAL"

    def classify_campaign(self, metrics: CampaignMetrics) -> PerformanceTier:
        if metrics.impressions < self.MIN_IMPRESSIONS:
            return PerformanceTier.ZERO_PERFORMANCE

        if metrics.clicks >= 12 and metrics.purchases == 0:
            return PerformanceTier.UNPROFITABLE

        if (
            metrics.roas >= self.TOP_PERFORMER_ROAS_MIN
            and metrics.acos <= self.TOP_PERFORMER_ACOS_MAX
        ):
            return PerformanceTier.TOP_PERFORMER

        if (
            metrics.roas >= self.PROFITABLE_ROAS_MIN
            and metrics.acos <= self.PROFITABLE_ACOS_MAX
        ):
            return PerformanceTier.PROFITABLE

        if (
            metrics.roas >= self.MARGINAL_ROAS_MIN
            and metrics.acos <= self.MARGINAL_ACOS_MAX
        ):
            return PerformanceTier.MARGINAL

        return PerformanceTier.UNPROFITABLE

    def is_priority_compost_campaign(self, metrics: CampaignMetrics) -> bool:
        name = (metrics.campaign_name or "").lower()
        keyword = (metrics.keyword_text or "").lower()
        text = f"{name} {keyword}"
        return any(term in text for term in [
            "compost",
            "living soil",
            "worm castings",
            "biochar compost",
            "mycorrhizae",
        ])

    def calculate_recommended_budget(self, metrics: CampaignMetrics, tier: PerformanceTier) -> float:
        current = metrics.current_budget
        is_compost = self.is_priority_compost_campaign(metrics)

        if tier == PerformanceTier.TOP_PERFORMER:
            scale = 1.30 if is_compost and metrics.acos <= 0.40 else self.TOP_PERFORMER_SCALE
            return round(current * scale, 2)

        if tier == PerformanceTier.PROFITABLE:
            scale = 1.20 if is_compost and metrics.acos <= 0.40 else self.PROFITABLE_SCALE
            return round(current * scale, 2)

        if tier == PerformanceTier.MARGINAL:
            return round(current * self.MARGINAL_SCALE, 2)

        if tier == PerformanceTier.ZERO_PERFORMANCE:
            return 8.0 if is_compost else self.TEST_BUDGET

        return 0.0

    def hydrate_bid_recommendations(self, metrics: CampaignMetrics) -> CampaignMetrics:
        if not self.ads_client or not self.USE_AMAZON_SUGGESTED_BIDS:
            return metrics

        if not metrics.ad_group_id or not metrics.keyword_text:
            return metrics

        try:
            low, high = self.ads_client.get_bid_recommendation(
                campaign_id=metrics.campaign_id,
                ad_group_id=metrics.ad_group_id,
                keyword_text=metrics.keyword_text,
                match_type="EXACT",
            )
            metrics.suggested_bid_low = low
            metrics.suggested_bid_high = high
        except Exception as e:
            logger.warning(
                "Bid recommendation lookup failed for campaign=%s keyword=%s: %s",
                metrics.campaign_id,
                metrics.keyword_text,
                e,
            )
        return metrics

    def select_bid(self, metrics: CampaignMetrics) -> Tuple[float, str]:
        mode = self.get_bid_mode()

        low = float(metrics.suggested_bid_low or 0)
        high = float(metrics.suggested_bid_high or 0)

        if mode == "PEAK":
            if high > 0:
                return round(high, 2), "PEAK"
            return round(metrics.cpc or 0.75, 2), "PEAK"

        if mode == "OFF_PEAK":
            if low > 0:
                return round(low, 2), "OFF_PEAK"
            return round(metrics.cpc or 0.30, 2), "OFF_PEAK"

        if self.NORMAL_MODE_STRATEGY == "LOW" and low > 0:
            return round(low, 2), "NORMAL"
        if self.NORMAL_MODE_STRATEGY == "HIGH" and high > 0:
            return round(high, 2), "NORMAL"
        if low > 0 and high > 0:
            return round((low + high) / 2, 2), "NORMAL"

        return round(metrics.cpc or 0.50, 2), "NORMAL"

    def maybe_apply_bid(self, metrics: CampaignMetrics, bid: float) -> None:
        if not self.APPLY_LIVE_BID_UPDATES or not self.ads_client:
            return
        if not metrics.keyword_id or not metrics.ad_group_id:
            return

        self.ads_client.update_keyword_bid(
            keyword_id=metrics.keyword_id,
            campaign_id=metrics.campaign_id,
            ad_group_id=metrics.ad_group_id,
            bid=bid,
            state="ENABLED",
        )

    def generate_optimization_action(self, metrics: CampaignMetrics) -> OptimizationAction:
        metrics = self.hydrate_bid_recommendations(metrics)

        tier = self.classify_campaign(metrics)
        recommended_budget = self.calculate_recommended_budget(metrics, tier)
        selected_bid, bid_mode = self.select_bid(metrics)

        if tier == PerformanceTier.TOP_PERFORMER:
            action = "scale_up"
            reason = f"High ROAS ({metrics.roas:.2f}) and efficient ACoS ({metrics.acos:.1%})."
            priority = 1
        elif tier == PerformanceTier.PROFITABLE:
            action = "scale_up"
            reason = f"Profitable performance with ROAS {metrics.roas:.2f}."
            priority = 2
        elif tier == PerformanceTier.MARGINAL:
            action = "optimize"
            reason = f"Marginal performance with ACoS {metrics.acos:.1%}."
            priority = 3
        elif tier == PerformanceTier.UNPROFITABLE:
            action = "pause"
            reason = f"Unprofitable performance; clicks without acceptable return."
            priority = 1
        else:
            action = "test"
            reason = "Low data volume; keep budget restrained while collecting more data."
            priority = 4

        try:
            self.maybe_apply_bid(metrics, selected_bid)
        except Exception as e:
            logger.warning(
                "Live bid update failed for campaign=%s keyword=%s: %s",
                metrics.campaign_id,
                metrics.keyword_text,
                e,
            )

        return OptimizationAction(
            campaign_id=metrics.campaign_id,
            campaign_name=metrics.campaign_name,
            tier=tier,
            current_budget=metrics.current_budget,
            recommended_budget=recommended_budget,
            action=action,
            reason=reason,
            priority=priority,
            amazonSuggestedBidLow=round(metrics.suggested_bid_low or 0, 2),
            amazonSuggestedBidHigh=round(metrics.suggested_bid_high or 0, 2),
            currentBidMode=bid_mode,
            currentAppliedBid=round(selected_bid, 2),
            keyword_id=metrics.keyword_id,
            keyword_text=metrics.keyword_text,
            ad_group_id=metrics.ad_group_id,
        )

    def optimize_campaign_set(self, campaigns: List[CampaignMetrics]) -> List[OptimizationAction]:
        actions = [self.generate_optimization_action(c) for c in campaigns]
        actions.sort(key=lambda x: (x.priority, -abs(x.recommended_budget - x.current_budget)))
        return actions

    def generate_budget_reallocation_summary(self, actions: List[OptimizationAction]) -> Dict[str, Any]:
        current_total = sum(a.current_budget for a in actions)
        recommended_total = sum(a.recommended_budget for a in actions)

        by_tier: Dict[str, Dict[str, Any]] = {}
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
            "additional_budget_needed": round(
                sum(max(0, a.recommended_budget - a.current_budget) for a in scaled_up_campaigns), 2
            ),
        }


def parse_campaign_csv(csv_content: str) -> List[CampaignMetrics]:
    campaigns: List[CampaignMetrics] = []
    reader = csv.DictReader(csv_content.splitlines())

    for row in reader:
        try:
            def safe_float(value: Any, default: float = 0.0) -> float:
                if value in (None, "", "0"):
                    return default
                cleaned = str(value).replace("%", "").replace("$", "").replace(",", "").strip()
                if cleaned.startswith("<"):
                    return 0.0
                try:
                    return float(cleaned)
                except ValueError:
                    return default

            def safe_int(value: Any, default: int = 0) -> int:
                try:
                    return int(safe_float(value, default))
                except ValueError:
                    return default

            campaign_name = row.get("Campaign name", "") or row.get("campaignName", "")
            campaign_id = (
                row.get("Campaign ID")
                or row.get("campaignId")
                or campaign_name
            )

            metrics = CampaignMetrics(
                campaign_id=str(campaign_id),
                campaign_name=str(campaign_name),
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
                cpc=safe_float(row.get("CPC", "0")),
                suggested_bid_low=safe_float(row.get("Suggested bid (low)", "0")),
                suggested_bid_high=safe_float(row.get("Suggested bid (high)", "0")),
                keyword_id=str(row.get("Keyword ID", "") or row.get("keywordId", "")),
                keyword_text=str(row.get("Keyword", "") or row.get("keywordText", "")),
                ad_group_id=str(row.get("Ad group ID", "") or row.get("adGroupId", "")),
            )
            campaigns.append(metrics)

        except Exception as e:
            logger.warning("Error parsing campaign row: %s", e)
            continue

    return campaigns


def format_optimization_report(actions: List[OptimizationAction], summary: Dict[str, Any]) -> str:
    report: List[str] = []
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

    for action in actions:
        report.append(f"Campaign: {action.campaign_name}")
        report.append(f"  Action: {action.action.upper()}")
        report.append(f"  Budget: ${action.current_budget:.2f} -> ${action.recommended_budget:.2f}")
        report.append(
            f"  Bid Mode: {action.currentBidMode} | "
            f"Low: ${action.amazonSuggestedBidLow:.2f} | "
            f"High: ${action.amazonSuggestedBidHigh:.2f} | "
            f"Applied: ${action.currentAppliedBid:.2f}"
        )
        report.append(f"  Reason: {action.reason}")
        report.append("")

    return "\n".join(report)
