"""Amazon Advertising API Client for Nature's Way Soil PPC Optimizer"""

import os
import time
import requests
from typing import Dict, List, Any, Optional
from pathlib import Path

class AmazonAdsClient:
    BASE_URL = "https://advertising-api.amazon.com/v2"
    
    def __init__(self):
        self.client_id = os.getenv("AMAZON_CLIENT_ID")
        self.client_secret = os.getenv("AMAZON_CLIENT_SECRET")
        self.refresh_token = os.getenv("AMAZON_REFRESH_TOKEN")
        self.profile_id = os.getenv("AMAZON_PROFILE_ID")
        
        if not all([self.client_id, self.client_secret, self.refresh_token, self.profile_id]):
            raise ValueError("Missing Amazon Ads credentials in environment variables")
        
        self.access_token = None
        self.token_expires = 0
        self.headers = None

    def _get_access_token(self):
        """Refresh LWA access token"""
        if time.time() < self.token_expires:
            return
        
        url = "https://api.amazon.com/auth/o2/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        token_data = resp.json()
        
        self.access_token = token_data["access_token"]
        self.token_expires = time.time() + token_data["expires_in"] - 60
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Content-Type": "application/json",
            "Amazon-Advertising-API-Scope": self.profile_id,
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        self._get_access_token()
        url = f"{self.BASE_URL}{endpoint}"
        resp = requests.request(method, url, headers=self.headers, **kwargs)
        
        if resp.status_code == 429:
            print("⚠️ Rate limited — waiting 2 seconds...")
            time.sleep(2)
            return self._request(method, endpoint, **kwargs)
        
        try:
            resp.raise_for_status()
        except Exception as e:
            print(f"❌ API Error {resp.status_code}: {resp.text}")
            raise
        
        return resp.json() if resp.content else {}

    # ====================== CORE METHODS ======================
    def create_campaign(self, campaign_data: Dict) -> Dict:
        return self._request("POST", "/sp/campaigns", json=[campaign_data])

    def create_ad_group(self, ad_group_data: Dict) -> Dict:
        return self._request("POST", "/sp/adGroups", json=[ad_group_data])

    def create_keywords(self, keywords: List[Dict]) -> Dict:
        return self._request("POST", "/sp/keywords", json=keywords)

    def update_keyword_bids(self, keyword_updates: List[Dict]) -> Dict:
        return self._request("PUT", "/sp/keywords", json=keyword_updates)

    def create_negative_keywords(self, negatives: List[Dict]) -> Dict:
        return self._request("POST", "/sp/negativeKeywords", json=negatives)

    def get_profile(self):
        """Quick test to verify credentials"""
        return self._request("GET", "/profiles")
