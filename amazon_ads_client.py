"""Amazon Ads Client - Full ad group + AUTO BID ADJUSTMENT on existing keywords"""

import os
import time
import requests
from typing import Dict, List, Any

class AmazonAdsClient:
    BASE_URL = "https://advertising-api.amazon.com/v2"

    def __init__(self):
        self.client_id = os.getenv("AMAZON_CLIENT_ID")
        self.client_secret = os.getenv("AMAZON_CLIENT_SECRET")
        self.refresh_token = os.getenv("AMAZON_REFRESH_TOKEN")
        self.profile_id = os.getenv("AMAZON_PROFILE_ID")
        self.access_token = None
        self.token_expires = 0
        self.headers = None

    def _get_access_token(self):
        # ... (same as before - unchanged) ...
        if time.time() < self.token_expires:
            return
        # refresh logic (copy from previous version)
        url = "https://api.amazon.com/auth/o2/token"
        data = {"grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.client_id, "client_secret": self.client_secret}
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        token = resp.json()
        self.access_token = token["access_token"]
        self.token_expires = time.time() + token["expires_in"] - 60
        self.headers = {"Authorization": f"Bearer {self.access_token}", "Amazon-Advertising-API-ClientId": self.client_id, "Content-Type": "application/json", "Amazon-Advertising-API-Scope": self.profile_id}

    def _request(self, method: str, endpoint: str, **kwargs):
        # ... (same as before) ...
        self._get_access_token()
        url = f"{self.BASE_URL}{endpoint}"
        resp = requests.request(method, url, headers=self.headers, **kwargs)
        if resp.status_code == 429:
            time.sleep(2)
            return self._request(method, endpoint, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # Existing methods (create_campaign, create_ad_group, etc.) stay the same

    def get_keywords(self, ad_group_id: str) -> List[Dict]:
        """Fetch existing keywords for auto bid adjustment"""
        params = {"adGroupIdFilter": ad_group_id, "stateFilter": "enabled"}
        return self._request("GET", "/sp/keywords", params=params)

    def update_keyword_bids(self, updates: List[Dict]) -> Dict:
        """Full auto bid adjustment"""
        return self._request("PUT", "/sp/keywords", json=updates)
