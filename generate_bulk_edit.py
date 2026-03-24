#!/usr/bin/env python3
"""Generate Amazon Ads Bulk Edit CSV for campaign optimizations"""

import csv
from pathlib import Path
from campaign_optimizer import CampaignOptimizer, parse_campaign_csv

def generate_bulk_edit_csv(csv_file: str, priority_filter: int = None, output_file: str = None):
    """Generate bulk edit CSV for Amazon Ads"""
    
    csv_path = Path(csv_file)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_file}")
        return
    
    with open(csv_path) as f:
        campaigns = parse_campaign_csv(f.read())
    
    optimizer = CampaignOptimizer()
    actions = optimizer.optimize_campaign_set(campaigns)
    
    if priority_filter:
        actions = [a for a in actions if a.priority <= priority_filter]
    
    if not output_file:
        output_file = f"bulk_edit_priority_{priority_filter or 'all'}.csv"
    
    # Create bulk edit CSV
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Amazon Ads bulk edit header
        writer.writerow([
            'Campaign Name',
            'Action',
            'Daily Budget',
            'Status'
        ])
        
        for action in actions:
            if action.action == "pause":
                writer.writerow([
                    action.campaign_name,
                    'UPDATE',
                    f'{action.current_budget:.2f}',
                    'PAUSED'
                ])
            else:
                writer.writerow([
                    action.campaign_name,
                    'UPDATE',
                    f'{action.recommended_budget:.2f}',
                    'ENABLED'
                ])
    
    print("=" * 80)
    print("BULK EDIT FILE GENERATED")
    print("=" * 80)
    print(f"File: {output_file}")
    print(f"Actions: {len(actions)}")
    print("\nNext steps:")
    print("1. Go to Amazon Ads: https://advertising.amazon.com/cm/campaigns")
    print("2. Click 'Bulk operations' → 'Upload'")
    print(f"3. Upload this file: {output_file}")
    print("4. Review changes and click 'Apply'")
    print("=" * 80)
    
    # Also print a quick summary
    print("\nChanges to apply:")
    for action in actions:
        if action.action == "pause":
            print(f"  🛑 PAUSE: {action.campaign_name}")
        else:
            print(f"  💰 ${action.current_budget:.2f} → ${action.recommended_budget:.2f}: {action.campaign_name[:60]}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate Amazon Ads bulk edit CSV")
    parser.add_argument("csv_file", help="Campaign CSV from Amazon Ads")
    parser.add_argument("--priority", type=int, choices=[1,2,3,4,5], help="Priority filter")
    parser.add_argument("--output", help="Output filename")
    
    args = parser.parse_args()
    
    generate_bulk_edit_csv(args.csv_file, args.priority, args.output)

