"""Standalone Amazon Ads Client.

Kept for scripts that import amazon_ads_client directly.  The Cloud Run app mainly
uses optimize_campaigns.AmazonAdsClient, but this class now reads the same secret
names used by the deploy workflow.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import requests


class AmazonAdsClient:
    BASE_URLS = {
        "na": "https://advertising-api.amazon.com",
        "eu": "https://advertising-api-eu.amazon.com",
        "fe": "https://advertising-api-fe.amazon.com",
    }

    def __init__(self):
        self.client_id = os.getenv("AMAZON_ADS_CLIENT_ID") or os.getenv("AMAZON_CLIENT_ID")
        self.client_secret = os.getenv("AMAZON_ADS_CLIENT_SECRET") or os.getenv("AMAZON_CLIENT_SECRET")
        self.refresh_token = os.getenv("AMAZON_ADS_REFRESH_TOKEN") or os.getenv("AMAZON_REFRESH_TOKEN")
        self.profile_id = os.getenv("AMAZON_ADS_PROFILE_ID") or os.getenv("AMAZON_PROFILE_ID")
        self.region = (os.getenv("AMAZON_ADS_REGION") or os.getenv("AMAZON_REGION") or "na").lower()
        self.base_url = self.BASE_URLS.get(self.region, self.BASE_URLS["na"])
        self.access_token = None
        self.token_expires = 0.0
        self.headers: Dict[str, str] | None = None

    def _get_access_token(self) -> None:
        if self.access_token and time.time() < self.token_expires:
            return
        missing = [
            name for name, value in {
                "AMAZON_ADS_CLIENT_ID": self.client_id,
                "AMAZON_ADS_CLIENT_SECRET": self.client_secret,
                "AMAZON_ADS_REFRESH_TOKEN": self.refresh_token,
                "AMAZON_ADS_PROFILE_ID": self.profile_id,
            }.items() if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Amazon Ads config: {', '.join(missing)}")
        resp = requests.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json()
        self.access_token = token["access_token"]
        self.token_expires = time.time() + int(token.get("expires_in", 3600)) - 60
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Amazon-Advertising-API-ClientId": str(self.client_id),
            "Amazon-Advertising-API-Scope": str(self.profile_id),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        self._get_access_token()
        url = f"{self.base_url}{endpoint}"
        resp = requests.request(method, url, headers=self.headers, timeout=kwargs.pop("timeout", 60), **kwargs)
        if resp.status_code == 429:
            time.sleep(2)
            return self._request(method, endpoint, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def get_keywords(self, ad_group_id: str) -> List[Dict[str, Any]]:
        params = {"adGroupIdFilter": ad_group_id, "stateFilter": "enabled"}
        data = self._request("GET", "/sp/keywords", params=params)
        return data if isinstance(data, list) else data.get("keywords", [])

    def update_keyword_bids(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._request("PUT", "/sp/keywords", json={"keywords": updates})
