#!/usr/bin/env python3
"""Automatically apply campaign optimizations via Amazon Ads API"""

import sys
import json
import logging
from pathlib import Path
from campaign_optimizer import CampaignOptimizer, parse_campaign_csv, OptimizationAction
from app import AmazonAdsClient, ENDPOINTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def apply_all_optimizations(csv_file: str, apply_live: bool = False, priority_filter: int = None):
    """Apply optimizations via Amazon Ads API"""
    
    csv_path = Path(csv_file)
    if not csv_path.exists():
        return {"error": f"CSV not found: {csv_file}"}
    
    with open(csv_path) as f:
        campaigns = parse_campaign_csv(f.read())
    
    if not campaigns:
        return {"error": "No campaigns found"}
    
    optimizer = CampaignOptimizer()
    actions = optimizer.optimize_campaign_set(campaigns)
    summary = optimizer.generate_budget_reallocation_summary(actions)
    
    if priority_filter:
        actions = [a for a in actions if a.priority <= priority_filter]
        logger.info(f"Filtered to {len(actions)} priority {priority_filter} actions")
    
    # For now, just return dry run results without API calls
    # The API endpoint issue needs to be resolved first
    results = []
    
    for action in actions:
        if action.action == "pause":
            results.append({
                'campaign': action.campaign_name,
                'action': 'pause',
                'status': 'dry_run' if not apply_live else 'pending',
                'message': f'Would pause campaign (ACOS {action.tier.value})'
            })
        else:
            results.append({
                'campaign': action.campaign_name,
                'action': action.action,
                'status': 'dry_run' if not apply_live else 'pending',
                'old_budget': action.current_budget,
                'new_budget': action.recommended_budget,
                'message': f'Would update: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}'
            })
    
    return {
        'mode': 'LIVE' if apply_live else 'DRY_RUN',
        'total_actions': len(actions),
        'summary': summary,
        'results': results,
        'note': 'API integration pending - showing recommendations only'
    }

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Auto-apply campaign optimizations")
    parser.add_argument("csv_file", help="Campaign CSV")
    parser.add_argument("--apply", action="store_true", help="Apply changes")
    parser.add_argument("--priority", type=int, choices=[1,2,3,4,5], help="Priority filter")
    parser.add_argument("--output", help="Save JSON")
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("AUTOMATIC CAMPAIGN OPTIMIZATION")
    print("=" * 80)
    print(f"CSV: {args.csv_file}")
    print(f"Mode: {'🔴 LIVE' if args.apply else '🟡 DRY RUN'}")
    if args.priority:
        print(f"Priority: <= {args.priority}")
    print("=" * 80)
    
    if args.apply:
        confirm = input("\n⚠️  Apply LIVE changes? (yes/no): ")
        if confirm.lower() != 'yes':
            print("Cancelled.")
            sys.exit(0)
    
    results = apply_all_optimizations(args.csv_file, args.apply, args.priority)
    
    if 'error' in results:
        print(f"ERROR: {results['error']}")
        sys.exit(1)
    
    print(f"\n{results['mode']} - {results.get('note', '')}")
    print(f"Total Actions: {results['total_actions']}")
    
    summary = results['summary']
    print(f"\nBudget Impact:")
    print(f"  Current: ${summary['current_total_budget']:.2f}/day")
    print(f"  Recommended: ${summary['recommended_total_budget']:.2f}/day")
    print(f"  Change: ${summary['budget_change']:.2f}")
    
    print("\nPriority 1 Actions:")
    for r in results['results']:
        status_icon = {'applied': '✓', 'dry_run': '○', 'pending': '⊙', 'skipped': '⊘', 'error': '✗'}.get(r['status'], '?')
        print(f"  {status_icon} {r['campaign'][:50]}")
        print(f"     {r['message']}")
    
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n📄 Saved: {args.output}")
