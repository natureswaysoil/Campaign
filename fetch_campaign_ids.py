#!/usr/bin/env python3
"""
Fetch campaign IDs using Amazon Ads Reporting API
"""

import json
import time
from app import AmazonAdsClient

def fetch_campaign_ids():
    """Fetch all campaign IDs and save to JSON"""
    
    # Initialize client - AmazonAdsClient loads credentials from env/secrets automatically
    client = AmazonAdsClient()
    
    print("Requesting campaign report...")
    
    # Request report
    report_config = {
        "reportDate": "YESTERDAY",
        "metrics": "campaignId,campaignName,campaignStatus"
    }
    
    report_id = client.request_report("campaigns", report_config)
    print(f"Report ID: {report_id}")
    
    # Wait for report (check every 30 seconds)
    max_attempts = 30
    for attempt in range(max_attempts):
        status = client.get_report_status(report_id)
        print(f"Attempt {attempt + 1}/{max_attempts}: {status}")
        
        if status == "SUCCESS":
            break
        elif status in ["FAILURE", "FATAL"]:
            raise Exception(f"Report generation failed: {status}")
        
        time.sleep(30)
    
    if status != "SUCCESS":
        raise Exception("Report timed out")
    
    # Download report
    print("Downloading report...")
    report_data = client.download_report(report_id)
    
    # Parse and save campaign IDs
    campaign_map = {}
    for row in report_data:
        campaign_id = row.get("campaignId")
        campaign_name = row.get("campaignName")
        if campaign_id and campaign_name:
            campaign_map[campaign_name] = campaign_id
    
    print(f"Found {len(campaign_map)} campaigns")
    
    # Save to /tmp (exists in Cloud Run)
    output_path = "/tmp/campaign_ids.json"
    with open(output_path, "w") as f:
        json.dump(campaign_map, f, indent=2)
    
    print(f"Saved to {output_path}")
    return campaign_map

if __name__ == "__main__":
    fetch_campaign_ids()
