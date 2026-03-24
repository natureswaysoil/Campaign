#!/usr/bin/env python3
"""Fetch campaign IDs from Amazon Ads reporting API"""

import json
import gzip
import time
from datetime import datetime, timedelta
from app import AmazonAdsClient, ENDPOINTS

def fetch_campaign_ids():
    """Fetch campaign IDs using the SP report API"""
    
    client = AmazonAdsClient()
    
    # Request a campaign report for the last 30 days
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    print(f"Requesting campaign report from {start_date} to {end_date}...")
    
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": ["campaignId", "campaignName", "campaignBudgetAmount", "campaignStatus"],
            "reportTypeId": "spCampaigns",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON"
        }
    }
    
    # Request the report
    report_resp = client.post(ENDPOINTS["reports"], body)
    report_id = report_resp.get("reportId")
    
    print(f"Report ID: {report_id}")
    print("Report generating (this can take 5-10 minutes)...")
    print("You can check status manually at: https://advertising.amazon.com")
    
    # Poll for completion - wait longer between checks
    for i in range(60):  # 60 attempts x 30 sec = 30 minutes max
        time.sleep(30)  # Wait 30 seconds between checks
        
        try:
            status_resp = client.get(f"{ENDPOINTS['reports']}/{report_id}")
            status = status_resp.get("status")
            
            if i % 2 == 0:  # Print every minute
                print(f"  {(i+1)*30//60} min - Status: {status}")
            
            if status == "COMPLETED":
                download_url = status_resp.get("url")
                print(f"\n✓ Report ready!")
                
                # Download and decompress
                data = client.download_binary(download_url)
                decompressed = gzip.decompress(data).decode('utf-8')
                report = json.loads(decompressed)
                
                # Extract campaign ID mapping
                campaign_map = {}
                for row in report:
                    camp_id = row.get("campaignId")
                    camp_name = row.get("campaignName")
                    if camp_id and camp_name:
                        campaign_map[camp_name] = str(camp_id)
                
                # Save to file
                with open('campaign_ids.json', 'w') as f:
                    json.dump(campaign_map, f, indent=2)
                
                print(f"\n✓ Found {len(campaign_map)} campaigns")
                print(f"✓ Saved to campaign_ids.json")
                return campaign_map
            
            elif status in ["FAILED", "FATAL"]:
                print(f"✗ Report failed: {status_resp}")
                return {}
                
        except Exception as e:
            print(f"  Error checking status: {e}")
            continue
    
    print("\n✗ Timeout after 30 minutes")
    print(f"Check report status manually: Report ID {report_id}")
    return {}

if __name__ == "__main__":
    campaign_map = fetch_campaign_ids()
    
    if campaign_map:
        print("\nSample campaigns:")
        for name, cid in list(campaign_map.items())[:5]:
            print(f"  {cid}: {name[:60]}")

