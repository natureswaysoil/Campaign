#!/usr/bin/env python3
"""Automatically apply campaign optimizations via Amazon Ads API"""

import sys
import json
import logging
from pathlib import Path
from typing import List, Dict, Any

from campaign_optimizer import CampaignOptimizer, parse_campaign_csv, OptimizationAction
from app import AmazonAdsClient, ENDPOINTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def apply_all_optimizations(csv_file: str, apply_live: bool = False, priority_filter: int = None):
    """Apply optimizations via Amazon Ads API"""
    
    # Load CSV
    csv_path = Path(csv_file)
    if not csv_path.exists():
        return {"error": f"CSV not found: {csv_file}"}
    
    with open(csv_path) as f:
        campaigns = parse_campaign_csv(f.read())
    
    if not campaigns:
        return {"error": "No campaigns found in CSV"}
    
    # Generate optimization actions
    optimizer = CampaignOptimizer()
    actions = optimizer.optimize_campaign_set(campaigns)
    summary = optimizer.generate_budget_reallocation_summary(actions)
    
    # Filter by priority if specified
    if priority_filter:
        actions = [a for a in actions if a.priority <= priority_filter]
        logger.info(f"Filtered to {len(actions)} priority {priority_filter} actions")
    
    # Initialize Amazon Ads client (uses secrets from GCP)
    client = AmazonAdsClient()
    
    # Fetch campaigns to get IDs
    logger.info("Fetching campaigns from Amazon Ads API...")
    response = client.get(ENDPOINTS['campaigns'])
    
    # Map campaign names to IDs
    name_to_id = {}
    if response and 'campaigns' in response:
        for camp_wrapper in response.get('campaigns', []):
            camp = camp_wrapper.get('campaign', camp_wrapper)
            name = camp.get('name', '')
            cid = camp.get('campaignId', '')
            if name and cid:
                name_to_id[name] = str(cid)
    
    logger.info(f"Mapped {len(name_to_id)} campaigns")
    
    # Apply each action
    results = []
    
    for action in actions:
        campaign_id = name_to_id.get(action.campaign_name)
        
        if not campaign_id:
            results.append({
                'campaign': action.campaign_name,
                'action': action.action,
                'status': 'skipped',
                'message': 'Campaign ID not found in API'
            })
            continue
        
        try:
            if action.action == "pause":
                payload = {
                    "campaignId": campaign_id,
                    "state": "PAUSED"
                }
                
                if apply_live:
                    resp = client.post(ENDPOINTS['campaigns'], [payload])
                    logger.info(f"✓ Paused: {action.campaign_name}")
                    results.append({
                        'campaign': action.campaign_name,
                        'action': 'pause',
                        'status': 'applied',
                        'message': f'Campaign paused'
                    })
                else:
                    results.append({
                        'campaign': action.campaign_name,
                        'action': 'pause',
                        'status': 'dry_run',
                        'message': 'Would pause campaign'
                    })
            
            elif action.action in ["scale_up", "scale_down", "test", "monitor"]:
                payload = {
                    "campaignId": campaign_id,
                    "budget": {
                        "budget": action.recommended_budget,
                        "budgetType": "DAILY"
                    }
                }
                
                if apply_live:
                    resp = client.post(ENDPOINTS['campaigns'], [payload])
                    logger.info(f"✓ Updated {action.campaign_name}: ${action.current_budget} → ${action.recommended_budget}")
                    results.append({
                        'campaign': action.campaign_name,
                        'action': action.action,
                        'status': 'applied',
                        'old_budget': action.current_budget,
                        'new_budget': action.recommended_budget,
                        'message': f'Budget updated: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}'
                    })
                else:
                    results.append({
                        'campaign': action.campaign_name,
                        'action': action.action,
                        'status': 'dry_run',
                        'old_budget': action.current_budget,
                        'new_budget': action.recommended_budget,
                        'message': f'Would update: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}'
                    })
        
        except Exception as e:
            logger.error(f"✗ Failed {action.campaign_name}: {e}")
            results.append({
                'campaign': action.campaign_name,
                'action': action.action,
                'status': 'error',
                'message': str(e)
            })
    
    return {
        'mode': 'LIVE' if apply_live else 'DRY_RUN',
        'total_actions': len(actions),
        'summary': summary,
        'results': results
    }


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Auto-apply campaign optimizations")
    parser.add_argument("csv_file", help="Campaign performance CSV")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--priority", type=int, choices=[1,2,3,4,5], help="Only apply priority <= N")
    parser.add_argument("--output", help="Save JSON results")
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("AUTOMATIC CAMPAIGN OPTIMIZATION")
    print("=" * 80)
    print(f"CSV: {args.csv_file}")
    print(f"Mode: {'🔴 LIVE - WILL APPLY CHANGES' if args.apply else '🟡 DRY RUN'}")
    if args.priority:
        print(f"Priority Filter: <= {args.priority} (1=most urgent)")
    print("=" * 80)
    print()
    
    if args.apply:
        confirm = input("⚠️  WARNING: Apply LIVE changes to Amazon Ads? Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Cancelled.")
            sys.exit(0)
    
    # Run optimization
    results = apply_all_optimizations(args.csv_file, args.apply, args.priority)
    
    if 'error' in results:
        print(f"ERROR: {results['error']}")
        sys.exit(1)
    
    # Display results
    print(f"\n{results['mode']} COMPLETE")
    print("-" * 80)
    print(f"Total Actions: {results['total_actions']}")
    
    summary = results['summary']
    print(f"\nBudget Impact:")
    print(f"  Current: ${summary['current_total_budget']:.2f}/day")
    print(f"  Recommended: ${summary['recommended_total_budget']:.2f}/day")
    print(f"  Change: ${summary['budget_change']:.2f}")
    print()
    
    print("Results:")
    for r in results['results']:
        status_icon = {'applied': '✓', 'dry_run': '○', 'skipped': '⊘', 'error': '✗'}[r['status']]
        print(f"  {status_icon} {r['campaign'][:50]}")
        print(f"     {r['message']}")
    
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n📄 Results saved: {args.output}")
    
    print("\n" + "=" * 80)
