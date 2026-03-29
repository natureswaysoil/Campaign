from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
import os
import requests
import datetime
import json
import logging
from pathlib import Path

# -----------------------
# BASIC SETUP
# -----------------------

app = FastAPI(title="Amazon PPC Optimizer")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# -----------------------
# AMAZON CONFIG
# -----------------------

TOKEN_URL = "https://api.amazon.com/auth/o2/token"

BASE_URLS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}

# -----------------------
# ENV / SECRETS
# -----------------------

def get_env(name, default=None):
    val = os.getenv(name)
    if val:
        return val.strip()
    if default is not None:
        return default
    raise RuntimeError(f"Missing env var: {name}")

# -----------------------
# TIME LOGIC (PEAK / OFF PEAK)
# -----------------------

PEAK_START = 18
PEAK_END = 23

OFF_START = 0
OFF_END = 6

def current_hour():
    return datetime.datetime.now().hour

def get_bid_mode():
    h = current_hour()
    if PEAK_START <= h <= PEAK_END:
        return "PEAK"
    if OFF_START <= h <= OFF_END:
        return "OFF_PEAK"
    return "NORMAL"

# -----------------------
# AMAZON CLIENT
# -----------------------

class AmazonAdsClient:
    def __init__(self):
        self.client_id = get_env("AMAZON_ADS_CLIENT_ID")
        self.client_secret = get_env("AMAZON_ADS_CLIENT_SECRET")
        self.refresh_token = get_env("AMAZON_ADS_REFRESH_TOKEN")
        self.profile_id = get_env("AMAZON_ADS_PROFILE_ID")
        self.region = get_env("AMAZON_ADS_REGION", "na")

        self.base_url = BASE_URLS[self.region]
        self.token = self.get_token()

    def get_token(self):
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def headers(self, content_type="application/json", accept=None):
        return {
            "Authorization": f"Bearer {self.token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Content-Type": content_type,
            "Accept": accept or content_type,
        }

    def post(self, endpoint, body, content_type="application/json", accept=None):
        url = f"{self.base_url}{endpoint}"
        r = requests.post(
            url,
            headers=self.headers(content_type=content_type, accept=accept),
            json=body,
            timeout=60,
        )
        if not r.ok:
            raise RuntimeError(r.text)
        return r.json() if r.text.strip() else {}

    def get(self, endpoint, accept="application/json"):
        url = f"{self.base_url}{endpoint}"
        r = requests.get(
            url,
            headers=self.headers(content_type="application/json", accept=accept),
            timeout=60,
        )
        if not r.ok:
            raise RuntimeError(r.text)
        return r.json() if r.text.strip() else {}

    def list_campaigns(self):
        return self.post(
            "/sp/campaigns/list",
            {
                "maxResults": 100,
                "filters": {"stateFilter": {"include": ["ENABLED"]}}
            },
            content_type="application/vnd.spcampaign.v3+json",
            accept="application/vnd.spcampaign.v3+json",
        ).get("campaigns", [])

    def get_keywords(self, campaign_id):
        return self.post(
            "/sp/keywords/list",
            {
                "maxResults": 50,
                "filters": {
                    "campaignIdFilter": {"include": [str(campaign_id)]}
                }
            },
            content_type="application/vnd.spkeyword.v3+json",
            accept="application/vnd.spkeyword.v3+json",
        ).get("keywords", [])

    def get_bid_recommendation(self, campaign_id, ad_group_id, keyword):
        try:
            data = self.post(
                "/sp/keywords/bidRecommendations",
                {
                    "recommendations": [{
                        "campaignId": str(campaign_id),
                        "adGroupId": str(ad_group_id),
                        "keywordText": keyword,
                        "matchType": "PHRASE"
                    }]
                },
                content_type="application/vnd.spkeyword.v3+json",
                accept="application/vnd.spkeyword.v3+json",
            )

            recs = data.get("recommendations", [])
            if not recs:
                return {}

            rec = recs[0]
            return {
                "low": rec.get("suggestedBidLow"),
                "high": rec.get("suggestedBidHigh"),
                "suggested": rec.get("suggestedBid")
            }
        except Exception:
            return {}


    # -----------------------
    # CAMPAIGNS
    # -----------------------

    def list_campaigns(self):
        return self.post("/sp/campaigns/list", {
            "maxResults": 100,
            "filters": {"stateFilter": {"include": ["ENABLED"]}}
        }).get("campaigns", [])

    # -----------------------
    # KEYWORDS
    # -----------------------

    def get_keywords(self, campaign_id):
        return self.post("/sp/keywords/list", {
            "maxResults": 50,
            "filters": {
                "campaignIdFilter": {"include": [str(campaign_id)]}
            }
        }).get("keywords", [])

    # -----------------------
    # BID RECOMMENDATIONS
    # -----------------------

    def get_bid_recommendation(self, campaign_id, ad_group_id, keyword):
        try:
            data = self.post("/sp/keywords/bidRecommendations", {
                "recommendations": [{
                    "campaignId": str(campaign_id),
                    "adGroupId": str(ad_group_id),
                    "keywordText": keyword,
                    "matchType": "PHRASE"
                }]
            })
            rec = data["recommendations"][0]

            return {
                "low": rec.get("suggestedBidLow"),
                "high": rec.get("suggestedBidHigh"),
                "suggested": rec.get("suggestedBid")
            }
        except:
            return {}

# -----------------------
# BID LOGIC
# -----------------------

def choose_bid(rec, fallback):
    mode = get_bid_mode()

    low = rec.get("low") or 0
    high = rec.get("high") or 0
    suggested = rec.get("suggested") or fallback

    if mode == "PEAK":
        return high or suggested or fallback

    if mode == "OFF_PEAK":
        return low or suggested or fallback

    return suggested or fallback

# -----------------------
# DASHBOARD ROUTE (FIXED)
# -----------------------

@app.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        path = BASE_DIR / "templates" / "dashboard.html"
        return HTMLResponse(path.read_text())
    except Exception as e:
        return HTMLResponse(f"""
        <h2>Dashboard Error</h2>
        <p>{str(e)}</p>
        <p>Path: {BASE_DIR}</p>
        """)

# -----------------------
# API: CAMPAIGN PERFORMANCE
# -----------------------

@app.get("/api/campaign-performance")
def campaign_performance():
    try:
        client = AmazonAdsClient()
        campaigns = client.list_campaigns()

        results = []

        for c in campaigns:
            cid = c.get("campaignId")
            keywords = client.get_keywords(cid)

            bid_low = None
            bid_high = None
            applied = None

            if keywords:
                kw = keywords[0]

                rec = client.get_bid_recommendation(
                    cid,
                    kw.get("adGroupId"),
                    kw.get("keywordText")
                )

                applied = choose_bid(rec, kw.get("bid") or 0.75)
                bid_low = rec.get("low")
                bid_high = rec.get("high")

            results.append({
                **c,
                "spend": 0,
                "sales": 0,
                "clicks": 0,
                "orders": 0,
                "acos": None,
                "amazonSuggestedBidLow": bid_low,
                "amazonSuggestedBidHigh": bid_high,
                "currentAppliedBid": applied,
                "currentBidMode": get_bid_mode()
            })

        return {
            "campaigns": results,
            "count": len(results),
            "bid_mode": get_bid_mode(),
            "peak_hours_label": "6pm–11pm"
        }

    except Exception as e:
        logger.exception("Campaign performance failed")
        return {
            "error": True,
            "message": str(e)
        }
# -----------------------
# HEALTH
# -----------------------

@app.get("/health")
def health():
    return {"status": "ok"}
