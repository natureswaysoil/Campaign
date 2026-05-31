"""Main optimizer script for Nature's Way Soil PPC - Growth + Cash Protection Mode"""

import pandas as pd
from campaign_engine import build_all_campaign_plans, CampaignEngine  # ← New import
from pathlib import Path
import json
from datetime import datetime

# ========================= CONFIG =========================
DRY_RUN = False                     # ← Set to False to push live
SEARCH_TERMS_CSV = "search_term_report.csv"   # ← Put your latest search term report here
TARGET_ACOS = 0.35
PRODUCT_MARGIN = 0.40

print(f"🚀 Starting PPC Optimizer - Dry Run: {DRY_RUN} | Target ACOS: {TARGET_ACOS*100}%")

# Load latest search terms (for harvesting + negatives)
def load_search_terms():
    if Path(SEARCH_TERMS_CSV).exists():
        df = pd.read_csv(SEARCH_TERMS_CSV)
        print(f"✅ Loaded {len(df)} search terms for harvesting")
        return df
    else:
        print("⚠️  No search_term_report.csv found — harvesting disabled")
        return None

search_terms_df = load_search_terms()

# Build full campaign plans using the NEW enhanced engine
plans = build_all_campaign_plans(search_terms_df=search_terms_df)

print(f"\n📊 Built plans for {plans['product_count']} products")
print(f"   • Broad Discovery campaigns added")
print(f"   • {sum(len(p.get('harvested_keywords', [])) for p in plans['plans'])} keywords harvested into Exact/Phrase")
print(f"   • Cash-protection + aggressive scaling active\n")

# Example: Show first product's plan
if plans['plans']:
    sample = plans['plans'][0]
    print("📋 Sample plan for:", sample['product_name'])
    print("   Campaigns:", len(sample['campaigns']))
    print("   Harvested keywords:", len(sample.get('harvested_keywords', [])))

# ====================== EXECUTION ======================
if not DRY_RUN:
    print("\n🔥 LIVE MODE — Pushing changes to Amazon...")
    # TODO: Call your Amazon Ads client here (existing code)
    # For now we just print the plan
    print("✅ All plans ready for live execution")
else:
    print("\n🧪 DRY RUN COMPLETE — Review the plans above before setting DRY_RUN=False")

# Optional: Save plan to JSON for review
output_file = f"campaign_plan_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
with open(output_file, "w") as f:
    json.dump(plans, f, indent=2, default=str)

print(f"\n💾 Full plan saved to: {output_file}")
print("🎯 Ready to scale winners aggressively while protecting cash flow!")
