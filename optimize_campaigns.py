#!/usr/bin/env python3
"""Standalone Campaign Optimizer Script"""

import sys
import json
import argparse
from pathlib import Path
from campaign_optimizer import CampaignOptimizer, parse_campaign_csv, format_optimization_report

def main():
    parser = argparse.ArgumentParser(description="Optimize Amazon PPC campaigns")
    parser.add_argument("csv_file", help="Path to campaign CSV file")
    parser.add_argument("--apply", action="store_true", help="Apply changes")
    parser.add_argument("--config", help="Config JSON file")
    parser.add_argument("--output", help="Output report file")
    
    args = parser.parse_args()
    
    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"Error: CSV file not found: {args.csv_file}")
        sys.exit(1)
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        csv_content = f.read()
    
    print(f"Loading campaign data from {args.csv_file}...")
    campaigns = parse_campaign_csv(csv_content)
    
    if not campaigns:
        print("Error: No valid campaigns found")
        sys.exit(1)
    
    print(f"Loaded {len(campaigns)} campaigns")
    
    config = {}
    if args.config:
        with open(args.config, 'r') as f:
            config = json.load(f)
    
    optimizer = CampaignOptimizer(config=config)
    
    print("\nAnalyzing campaign performance...")
    actions = optimizer.optimize_campaign_set(campaigns)
    summary = optimizer.generate_budget_reallocation_summary(actions)
    
    report = format_optimization_report(actions, summary)
    print("\n" + report)
    
    if args.output:
        with open(args.output, 'w') as f:
            f.write(report)
        print(f"\nReport saved to: {args.output}")
    
    if args.apply:
        print("\n" + "=" * 80)
        print("APPLYING CHANGES")
        print("=" * 80)
        print("\nNote: Requires Amazon Ads API credentials")
        critical_actions = [a for a in actions if a.priority == 1]
        for action in critical_actions:
            print(f"\n  Campaign: {action.campaign_name}")
            print(f"  Action: {action.action.upper()}")
            print(f"  Budget: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}")
    else:
        print("\n" + "=" * 80)
        print("DRY RUN - No changes applied")
        print("Add --apply flag to apply recommendations")
        print("=" * 80)

if __name__ == "__main__":
    main()
