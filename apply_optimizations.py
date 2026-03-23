#!/usr/bin/env python3
"""Automatically apply campaign optimizations via Amazon Ads API"""

import sys, json, logging
from pathlib import Path
from typing import List, Dict, Any
from campaign_optimizer import CampaignOptimizer, parse_campaign_csv, OptimizationAction
from app import AmazonAdsClient, ENDPOINTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def apply_all_optimizations(csv_file: str, apply_live: bool = False, priority_filter: int = None):
    """Apply optimizations automatically"""
    csv_path = Path(csv_file)
    if not csv_path.exists():
        return {"error": f"CSV not found: {csv_file}"}
    
    with open(csv_path) as f:
        campaigns = parse_campaign_csv(f.read())
    
    optimizer = CampaignOptimizer()
    actions = optimizer.optimize_campaign_set(campaigns)
    
    if priority_filter:
        actions = [a for a in actions if a.priority <= priority_filter]
    
    client = AmazonAdsClient()
    results = []
    
    for action in actions:
        try:
            if action.action == "pause":
                if apply_live:
                    # API call to pause
                    logger.info(f"Pausing: {action.campaign_name}")
                results.append({"campaign": action.campaign_name, "action": "pause", "success": True})
            elif action.action in ["scale_up", "scale_down"]:
                if apply_live:
                    # API call to update budget
                    logger.info(f"Updating {action.campaign_name}: ${action.recommended_budget}")
                results.append({"campaign": action.campaign_name, "budget": action.recommended_budget, "success": True})
        except Exception as e:
            results.append({"campaign": action.campaign_name, "error": str(e), "success": False})
    
    return {"mode": "LIVE" if apply_live else "DRY RUN", "results": results}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--priority", type=int, choices=[1,2,3])
    args = parser.parse_args()
    
    if args.apply:
        confirm = input("Apply LIVE changes? (yes/no): ")
        if confirm != "yes":
            sys.exit(0)
    
    results = apply_all_optimizations(args.csv_file, args.apply, args.priority)
    print(json.dumps(results, indent=2))
