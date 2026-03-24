#!/usr/bin/env python3
"""Automatically apply campaign optimizations via Amazon Ads API"""

import sys
import json
import logging
from pathlib import Path
from campaign_optimizer import CampaignOptimizer, parse_campaign_csv
from app import AmazonAdsClient, ENDPOINTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_campaign_ids():
    """Load campaign ID mapping from file"""
    try:
        with open('campaign_ids.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("campaign_ids.json not found. Run fetch_campaign_ids.py first!")
        return {}


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
    
    if not apply_live:
        results = []
        for action in actions:
            results.append({
                'campaign': action.campaign_name,
                'action': action.action,
                'status': 'dry_run',
                'old_budget': action.current_budget,
                'new_budget': action.recommended_budget,
                'message': f'Would {"pause" if action.action == "pause" else "update budget"}: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}'
            })
        
        return {
            'mode': 'DRY_RUN',
            'total_actions': len(actions),
            'summary': summary,
            'results': results
        }
    
    # LIVE MODE - use campaign IDs
    client = AmazonAdsClient()
    campaign_ids = load_campaign_ids()
    
    if not campaign_ids:
        return {"error": "No campaign IDs found. Run: python fetch_campaign_ids.py"}
    
    results = []
    
    for action in actions:
        campaign_id = campaign_ids.get(action.campaign_name)
        
        if not campaign_id:
            results.append({
                'campaign': action.campaign_name,
                'status': 'error',
                'message': 'Campaign ID not found - run fetch_campaign_ids.py to refresh'
            })
            continue
        
        try:
            if action.action == "pause":
                payload = {
                    "campaignId": campaign_id,
                    "state": "PAUSED"
                }
                
                logger.info(f"Pausing campaign {campaign_id}: {action.campaign_name[:50]}")
                response = client.put(ENDPOINTS['campaigns'], [payload])
                
                results.append({
                    'campaign': action.campaign_name,
                    'action': 'pause',
                    'status': 'applied',
                    'message': 'Campaign paused'
                })
                logger.info(f"✓ Paused: {action.campaign_name[:50]}")
                
            else:
                payload = {
                    "campaignId": campaign_id,
                    "budget": {
                        "budget": action.recommended_budget,
                        "budgetType": "DAILY"
                    }
                }
                
                logger.info(f"Updating campaign {campaign_id}: ${action.recommended_budget}")
                response = client.put(ENDPOINTS['campaigns'], [payload])
                
                results.append({
                    'campaign': action.campaign_name,
                    'action': action.action,
                    'status': 'applied',
                    'old_budget': action.current_budget,
                    'new_budget': action.recommended_budget,
                    'message': f'Budget updated: ${action.current_budget:.2f} → ${action.recommended_budget:.2f}'
                })
                logger.info(f"✓ Updated: {action.campaign_name[:50]}")
        
        except Exception as e:
            results.append({
                'campaign': action.campaign_name,
                'status': 'error',
                'message': str(e)
            })
            logger.error(f"✗ Error: {e}")
    
    return {
        'mode': 'LIVE',
        'total_actions': len(actions),
        'summary': summary,
        'results': results
    }


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Auto-apply campaign optimizations")
    parser.add_argument("csv_file", help="Campaign CSV")
    parser.add_argument("--apply", action="store_true", help="Apply changes LIVE")
    parser.add_argument("--priority", type=int, choices=[1,2,3,4,5], help="Priority filter")
    parser.add_argument("--output", help="Save JSON")
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("AUTOMATIC CAMPAIGN OPTIMIZATION")
    print("=" * 80)
    print(f"CSV: {args.csv_file}")
    print(f"Mode: {'🔴 LIVE - AUTOMATIC' if args.apply else '🟡 DRY RUN'}")
    if args.priority:
        print(f"Priority: <= {args.priority}")
    print("=" * 80)
    
    if args.apply:
        print("\n⚠️  AUTOMATIC MODE - Will apply changes immediately!")
        print("Changes:")
        print("  - Pause lawn fertilizerC")
        print("  - Scale Enhanced Living Compost to $75/day")
        print("  - Scale Liquid Humic & Fulvic to $75/day")
        print("  - Scale Pasture to $30/day")
        confirm = input("\nType 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Cancelled.")
            sys.exit(0)
    
    results = apply_all_optimizations(args.csv_file, args.apply, args.priority)
    
    if 'error' in results:
        print(f"\nERROR: {results['error']}")
        sys.exit(1)
    
    print(f"\n{results['mode']} COMPLETE")
    
    summary = results['summary']
    print(f"\nBudget Impact:")
    print(f"  Current: ${summary['current_total_budget']:.2f}/day")
    print(f"  Recommended: ${summary['recommended_total_budget']:.2f}/day")
    
    print("\nResults:")
    for r in results['results']:
        status_icon = {'applied': '✓', 'dry_run': '○', 'error': '✗'}.get(r['status'], '?')
        print(f"  {status_icon} {r['campaign'][:50]}")
        print(f"     {r['message']}")
    
    success = sum(1 for r in results['results'] if r['status'] in ['applied', 'dry_run'])
    errors = sum(1 for r in results['results'] if r['status'] == 'error')
    
    print(f"\n✓ Success: {success}")
    if errors > 0:
        print(f"✗ Errors: {errors}")

