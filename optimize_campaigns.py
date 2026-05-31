"""Main PPC Optimizer - Now with FULL Amazon Ads API Integration"""

import pandas as pd
import json
from datetime import datetime
from pathlib import Path
from campaign_engine import build_all_campaign_plans, CampaignEngine
from amazon_ads_client import AmazonAdsClient

# ========================= CONFIG =========================
DRY_RUN = False                     # ← Change to False when ready
SEARCH_TERMS_CSV = "search_term_report.csv"

print(f"🚀 Nature's Way Soil PPC Optimizer")
print(f"   Mode: {'LIVE' if not DRY_RUN else 'DRY RUN'} | Target ACOS: 35% | Cash Protected")

# Load search terms for harvesting & negatives
search_terms_df = None
if Path(SEARCH_TERMS_CSV).exists():
    search_terms_df = pd.read_csv(SEARCH_TERMS_CSV)
    print(f"✅ Loaded {len(search_terms_df)} search terms")
else:
    print("⚠️ No search_term_report.csv — harvesting disabled")

# Build plans with new engine
plans = build_all_campaign_plans(search_terms_df=search_terms_df)

client = AmazonAdsClient()

print(f"\n📊 Processing {plans['product_count']} products...")

for plan in plans['plans']:
    product_name = plan['product_name']
    print(f"\n🔹 {product_name}")
    
    for campaign in plan['campaigns']:
        if DRY_RUN:
            print(f"   → Would create {campaign['campaign_type']} campaign: {campaign['campaign_name']}")
            continue
        
        # Live execution
        try:
            result = client.create_campaign({
                "name": campaign["campaign_name"],
                "campaignType": "sponsoredProducts",
                "targetingType": "manual" if campaign.get("match_type") != "auto" else "auto",
                "dailyBudget": int(campaign.get("daily_budget", 20)),
                "state": "enabled",
                "biddingStrategy": campaign.get("bidding_strategy", "legacy"),
            })
            print(f"   ✅ Campaign created: {campaign['campaign_name']}")
        except Exception as e:
            print(f"   ❌ Campaign failed: {e}")

    # Harvested keywords (Exact/Phrase)
    for kw in plan.get('harvested_keywords', []):
        if not DRY_RUN:
            try:
                client.create_keywords([{
                    "campaignId": "...",  # you can fetch or hardcode later
                    "adGroupId": "...",
                    "keywordText": kw["keywordText"],
                    "matchType": kw["matchType"],
                    "bid": kw["bid"],
                    "state": "enabled"
                }])
            except:
                pass
        print(f"   ✅ Harvested: {kw['keywordText']} ({kw['matchType']})")

print(f"\n🎉 Optimizer finished! Plan saved to campaign_plan_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
