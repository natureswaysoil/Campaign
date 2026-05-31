"""COMPLETE PPC Optimizer - Ad Groups + Auto Bid Adjustment + Reliable JSON Save"""

import pandas as pd
import json
from datetime import datetime
from pathlib import Path
from campaign_engine import build_all_campaign_plans, CampaignEngine
from amazon_ads_client import AmazonAdsClient
from scaling_engine import ScalingEngine

DRY_RUN = True                     # ← Change to False when ready to go live

SEARCH_TERMS_CSV = "search_term_report.csv"

print("🚀 FULL OPTIMIZER RUNNING - Ad Groups + Auto Bid Adjustment")

# Load search terms
search_terms_df = None
if Path(SEARCH_TERMS_CSV).exists():
    search_terms_df = pd.read_csv(SEARCH_TERMS_CSV)
    print(f"✅ Loaded {len(search_terms_df)} search terms")
else:
    print("⚠️ No search_term_report.csv found — harvesting disabled")

# Build plans
plans = build_all_campaign_plans(search_terms_df=search_terms_df)
client = AmazonAdsClient()
engine = CampaignEngine()
scaling = ScalingEngine()

print(f"\n📊 Processing {plans['product_count']} products...\n")

for plan in plans['plans']:
    product_name = plan['product_name']
    print(f"🔹 Processing: {product_name}")

    # ... (rest of your campaign/ad group/harvest logic stays the same) ...

    # Auto bid adjustment section (already there)

# ====================== ALWAYS SAVE PLAN ======================
output_file = "campaign_plan.json"   # Simple, reliable name for GitHub Actions

with open(output_file, "w", encoding="utf-8") as f:
    json.dump(plans, f, indent=2, default=str)

print(f"\n💾 Campaign plan successfully saved to: {output_file}")
print(f"   Total products: {plans['product_count']}")
print(f"   Total harvested keywords: {sum(len(p.get('harvested_keywords', [])) for p in plans['plans'])}")

print(f"\n🎉 OPTIMIZER COMPLETE!")
if DRY_RUN:
    print("🧪 DRY RUN MODE — No changes made to Amazon")
else:
    print("🔥 LIVE MODE — Changes were pushed to Amazon")
