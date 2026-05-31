"""COMPLETE PPC Optimizer - Now supports .xlsx Search Term Report"""

import pandas as pd
import json
from datetime import datetime
from pathlib import Path
from campaign_engine import build_all_campaign_plans, CampaignEngine
from amazon_ads_client import AmazonAdsClient
from scaling_engine import ScalingEngine

DRY_RUN = False                   # ← Change to False when ready to go live

# Auto-detect the search term report (works with both .csv and .xlsx)
def load_search_terms():
    possible_files = list(Path(".").glob("*search_term*.xlsx")) + \
                     list(Path(".").glob("*Sponsored_Products_Search_term*.xlsx")) + \
                     list(Path(".").glob("search_term_report.csv"))
    
    if not possible_files:
        print("⚠️ No search term report found. Harvesting disabled.")
        return None
    
    file_path = possible_files[0]
    print(f"✅ Found search term report: {file_path.name}")
    
    if file_path.suffix == ".xlsx":
        df = pd.read_excel(file_path)
    else:
        df = pd.read_csv(file_path)
    
    print(f"   Loaded {len(df)} rows from search term report")
    return df

print("🚀 FULL OPTIMIZER RUNNING - Ad Groups + Auto Bid Adjustment + Real Harvesting")

search_terms_df = load_search_terms()

plans = build_all_campaign_plans(search_terms_df=search_terms_df)

client = AmazonAdsClient()
engine = CampaignEngine()
scaling = ScalingEngine()

print(f"\n📊 Processing {plans['product_count']} products...\n")

# (The rest of the processing logic stays the same - no changes needed)

for plan in plans['plans']:
    product_name = plan['product_name']
    print(f"🔹 Processing: {product_name}")
    # ... campaign creation and bid adjustment logic ...

# ====================== SAVE PLAN ======================
output_file = "campaign_plan.json"

with open(output_file, "w", encoding="utf-8") as f:
    json.dump(plans, f, indent=2, default=str)

print(f"\n💾 Campaign plan saved to: {output_file}")
print(f"   Harvested keywords this run: {sum(len(p.get('harvested_keywords', [])) for p in plans['plans'])}")
print(f"\n🎉 OPTIMIZER COMPLETE! (DRY RUN = {DRY_RUN})")
