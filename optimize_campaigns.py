from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
import datetime
import gzip
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


import csv
import io
import requests
from google.cloud import storage as gcs_storage
from campaign_engine import build_all_campaign_plans, clean_product_rows

app = FastAPI(title="Amazon PPC Optimizer")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CACHE_FILE = Path("/tmp/dashboard_cache.json")
OPTIMIZER_LOG_FILE = Path("/tmp/optimizer_history.json")

TOKEN_URL = "https://api.amazon.com/auth/o2/token"

BASE_URLS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}

PEAK_START = int(os.getenv("PEAK_HOURS_START", "10"))   # 10am EST — retail shopping window
PEAK_END   = int(os.getenv("PEAK_HOURS_END",   "20"))   # 8pm  EST — end of prime shopping
OFF_START  = int(os.getenv("OFF_PEAK_START",   "0"))    # midnight
OFF_END    = int(os.getenv("OFF_PEAK_END",     "8"))    # 8am  EST — overnight minimum

WINNER_MIN_CLICKS = int(os.getenv("WINNER_MIN_CLICKS", "8"))
WINNER_MIN_ORDERS = int(os.getenv("WINNER_MIN_ORDERS", "2"))
WINNER_MAX_ACOS = float(os.getenv("WINNER_MAX_ACOS", "0.35"))

NEGATIVE_MIN_CLICKS = int(os.getenv("NEGATIVE_MIN_CLICKS", "20"))
DEFAULT_WINNER_BID = float(os.getenv("DEFAULT_WINNER_BID", "0.90"))
DEFAULT_FALLBACK_BID = float(os.getenv("DEFAULT_FALLBACK_BID", "0.75"))

MEDIA_TYPES = {
    "campaigns_v3": "application/vnd.spcampaign.v3+json",
    "keywords_v3": "application/vnd.spkeyword.v3+json",
    "neg_keywords_v3": "application/vnd.spcampaignnegativekeyword.v3+json",
    "bid_rec_v3": "application/vnd.spkeywordbidrecommendation.v3+json",
    "json": "application/json",
}


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name)
    if value:
        return value.strip()
    if default is not None:
        return default
    raise RuntimeError(f"Missing env var: {name}")


def today_iso_date() -> str:
    return datetime.date.today().isoformat()


def iso_date_days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def utc_now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def current_hour_eastern() -> int:
    """Return current hour in US/Eastern time (handles EST/EDT automatically)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        tz = pytz.timezone("America/New_York")
    return datetime.datetime.now(tz).hour


def get_bid_mode() -> str:
    h = current_hour_eastern()
    if PEAK_START <= h <= PEAK_END:
        return "PEAK"
    if OFF_START <= h <= OFF_END:
        return "OFF_PEAK"
    return "NORMAL"


def load_optimizer_history() -> List[Dict[str, Any]]:
    if OPTIMIZER_LOG_FILE.exists():
        try:
            return json.loads(OPTIMIZER_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_optimizer_history(entry: Dict[str, Any]) -> None:
    history = load_optimizer_history()
    history.append(entry)
    history = history[-50:]
    try:
        OPTIMIZER_LOG_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save optimizer history: %s", e)


def verify_internal_token(
    authorization: Optional[str] = None,
    x_daily_optimizer_token: Optional[str] = None,
) -> None:
    token = os.getenv("DAILY_OPTIMIZER_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="DAILY_OPTIMIZER_TOKEN not configured")

    # Accept either X-Daily-Optimizer-Token header or Authorization: Bearer
    supplied = None
    if x_daily_optimizer_token:
        supplied = x_daily_optimizer_token.strip()
    elif authorization and authorization.startswith("Bearer "):
        supplied = authorization.replace("Bearer ", "", 1).strip()

    if not supplied:
        raise HTTPException(status_code=401, detail="Missing token")
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail="Invalid token")


def parse_report_json_bytes(content: bytes) -> List[Dict[str, Any]]:
    try:
        decompressed = gzip.decompress(content)
    except OSError:
        decompressed = content
    data = json.loads(decompressed.decode("utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("rows", [])
    return []


def num(row: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            try:
                return float(str(row[key]).replace("$", "").replace(",", "").strip())
            except Exception:
                continue
    return default


def text(row: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return default


def unique_in_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# Peak/off-peak multipliers — used when Amazon suggested bids unavailable
PEAK_MULTIPLIER     = float(os.getenv("PEAK_BID_MULTIPLIER",     "1.30"))  # +30% during prime
OFF_PEAK_MULTIPLIER = float(os.getenv("OFF_PEAK_BID_MULTIPLIER", "0.70"))  # -30% overnight

def choose_bid(rec: Dict[str, float], fallback: float) -> Tuple[float, float, float]:
    mode = get_bid_mode()
    low      = float(rec.get("low")       or 0.0)
    high     = float(rec.get("high")      or 0.0)
    suggested = float(rec.get("suggested") or 0.0)

    # Use Amazon suggested bids if available
    if low > 0 and high > 0:
        if mode == "PEAK":
            applied = high
        elif mode == "OFF_PEAK":
            applied = low
        else:
            applied = round((low + high) / 2.0, 2)
        return round(low, 2), round(high, 2), round(applied, 2)

    # Fallback: apply multiplier to current bid based on time of day
    if mode == "PEAK":
        applied = round(fallback * PEAK_MULTIPLIER, 2)
    elif mode == "OFF_PEAK":
        applied = round(fallback * OFF_PEAK_MULTIPLIER, 2)
    else:
        applied = round(fallback, 2)

    return round(low, 2), round(high, 2), applied


def classify_terms(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    winners: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []
    hold: List[Dict[str, Any]] = []

    for row in rows:
        term_value = text(row, ["Customer Search Term", "searchTerm", "Search Term"])
        if not term_value:
            continue

        clicks = int(num(row, ["Clicks", "clicks"], 0))
        cost = num(row, ["Spend", "Cost", "cost", "spend"], 0.0)
        sales = num(row, ["7 Day Total Sales", "14 Day Total Sales", "sales7d", "sales"], 0.0)
        orders = int(num(row, ["7 Day Total Orders (#)", "14 Day Total Orders (#)", "orders", "purchases7d"], 0))
        acos = (cost / sales) if sales > 0 else None

        result = {
            "term": term_value,
            "campaign_id": int(num(row, ["Campaign Id", "campaignId"], 0)),
            "ad_group_id": int(num(row, ["Ad Group Id", "adGroupId"], 0)),
            "clicks": clicks,
            "orders": orders,
            "cost": round(cost, 2),
            "sales": round(sales, 2),
            "acos": round(acos, 4) if acos is not None else None,
        }

        if (
            orders >= WINNER_MIN_ORDERS
            and clicks >= WINNER_MIN_CLICKS
            and sales > 0
            and (acos is None or acos <= WINNER_MAX_ACOS)
        ):
            winners.append({**result, "reason": "winner"})
        elif clicks >= NEGATIVE_MIN_CLICKS and orders == 0:
            negatives.append({**result, "reason": "negative"})
        else:
            hold.append({**result, "reason": "hold"})

    return {"winners": winners, "negatives": negatives, "hold": hold}


def read_cache() -> Dict[str, Any]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "campaigns": [],
        "count": 0,
        "bid_mode": get_bid_mode(),
        "peak_hours_label": f"{PEAK_START}:00–{PEAK_END}:59",
        "note": "No dashboard cache yet.",
        "refreshed_at": None,
        "summary": {
            "spend": 0,
            "sales": 0,
            "clicks": 0,
            "orders": 0,
            "acos": None,
        },
    }


def write_cache(data: Dict[str, Any]) -> None:
    CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


class AmazonAdsClient:
    def __init__(self):
        self.client_id = get_env("AMAZON_ADS_CLIENT_ID")
        self.client_secret = get_env("AMAZON_ADS_CLIENT_SECRET")
        self.refresh_token = get_env("AMAZON_ADS_REFRESH_TOKEN")
        self.profile_id = get_env("AMAZON_ADS_PROFILE_ID")
        self.region = get_env("AMAZON_ADS_REGION", "na").lower()

        if self.region not in BASE_URLS:
            raise RuntimeError("AMAZON_ADS_REGION must be 'na', 'eu', or 'fe'")

        self.base_url = BASE_URLS[self.region]
        self.session = requests.Session()
        self.token = self.get_token()

    def get_token(self) -> str:
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"Failed to obtain access token: {payload}")
        return token.strip()

    def headers(self, content_type: str, accept: Optional[str] = None) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Content-Type": content_type,
            "Accept": accept or content_type,
        }

    def post(self, endpoint: str, body: Any, *, content_type: str, accept: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        response = self.session.post(
            url,
            headers=self.headers(content_type=content_type, accept=accept),
            json=body,
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(response.text)
        return response.json() if response.text.strip() else {}

    def get_json(self, endpoint: str, *, accept: str) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        response = self.session.get(
            url,
            headers=self.headers(content_type=MEDIA_TYPES["json"], accept=accept),
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(response.text)
        return response.json() if response.text.strip() else {}

    def download_binary(self, url: str) -> bytes:
        response = self.session.get(url, timeout=120)
        response.raise_for_status()
        return response.content

    def list_campaigns(self) -> List[Dict[str, Any]]:
        data = self.post(
            "/sp/campaigns/list",
            {
                "maxResults": 100,
                "filters": {"stateFilter": {"include": ["ENABLED"]}},
            },
            content_type=MEDIA_TYPES["campaigns_v3"],
            accept=MEDIA_TYPES["campaigns_v3"],
        )
        return data.get("campaigns", [])

    def list_keywords(self, campaign_id: str) -> List[Dict[str, Any]]:
        data = self.post(
            "/sp/keywords/list",
            {
                "maxResults": 100,
                "filters": {
                    "campaignIdFilter": {"include": [str(campaign_id)]},
                    "stateFilter": {"include": ["ENABLED"]},
                },
            },
            content_type=MEDIA_TYPES["keywords_v3"],
            accept=MEDIA_TYPES["keywords_v3"],
        )
        return data.get("keywords", [])

    def create_keywords(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.post(
            "/sp/keywords",
            {"keywords": rows},
            content_type=MEDIA_TYPES["keywords_v3"],
            accept=MEDIA_TYPES["keywords_v3"],
        )

    def put(self, endpoint: str, body: Any, *, content_type: str, accept: Optional[str] = None) -> Dict[str, Any]:
        """HTTP PUT — required for updating existing resources (keyword bids, etc.)."""
        url = f"{self.base_url}{endpoint}"
        response = self.session.put(
            url,
            headers=self.headers(content_type=content_type, accept=accept),
            json=body,
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(f"PUT {endpoint} {response.status_code}: {response.text[:400]}")
        return response.json() if response.text.strip() else {}

    def update_keywords(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Update existing keyword bids via PUT /sp/keywords.
        Amazon Ads v3: PUT payload must only have keywordId + bid + state.
        Do NOT include keywordText or matchType — those trigger a 400.
        """
        clean_rows = [
            {
                "keywordId": str(r["keywordId"]),
                "bid": round(float(r["bid"]), 2),
                "state": r.get("state", "ENABLED"),
            }
            for r in rows
            if r.get("keywordId") and r.get("bid") is not None
        ]
        if not clean_rows:
            return {}
        return self.put(
            "/sp/keywords",
            {"keywords": clean_rows},
            content_type=MEDIA_TYPES["keywords_v3"],
            accept=MEDIA_TYPES["keywords_v3"],
        )

    def create_negative_keywords(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.post(
            "/sp/campaignNegativeKeywords",
            {"campaignNegativeKeywords": rows},
            content_type=MEDIA_TYPES["neg_keywords_v3"],
            accept=MEDIA_TYPES["neg_keywords_v3"],
        )

    def get_bid_recommendation(
        self,
        campaign_id: str,
        ad_group_id: str,
        keyword: str,
        match_type: str = "PHRASE",
    ) -> Dict[str, float]:
        try:
            data = self.post(
                "/sp/keywords/bidRecommendations",
                {
                    "recommendations": [{
                        "campaignId": str(campaign_id),
                        "adGroupId": str(ad_group_id),
                        "keywordText": keyword,
                        "matchType": str(match_type).upper(),
                    }]
                },
                content_type=MEDIA_TYPES["bid_rec_v3"],
                accept=MEDIA_TYPES["bid_rec_v3"],
            )
            recs = data.get("recommendations", [])
            if not recs:
                return {}

            rec = recs[0]
            return {
                "low": rec.get("suggestedBidLow"),
                "high": rec.get("suggestedBidHigh"),
                "suggested": rec.get("suggestedBid"),
            }
        except Exception as e:
            logger.warning("Bid recommendation unavailable for %s: %s", keyword, e)
            return {}

    def request_report(self, body: Dict[str, Any]) -> str:
        try:
            data = self.post(
                "/reporting/reports",
                body,
                content_type=MEDIA_TYPES["json"],
                accept=MEDIA_TYPES["json"],
            )
            report_id = data.get("reportId")
            if not report_id:
                raise RuntimeError(f"Failed to request report ID: {data}")
            return report_id
        except Exception as e:
            msg = str(e)
            if '"code":"425"' in msg and "duplicate of" in msg.lower():
                match = re.search(r'duplicate of\s*:\s*([a-f0-9-]+)', msg, re.I)
                if match:
                    logger.info("Using existing duplicate report ID: %s", match.group(1))
                    return match.group(1)
            raise

    def wait_for_report(self, report_id: str, timeout_loops: int = 180, sleep_seconds: int = 15) -> str:
        for attempt in range(timeout_loops):
            try:
                status = self.get_json(
                    f"/reporting/reports/{report_id}",
                    accept=MEDIA_TYPES["json"],
                )
            except RuntimeError as e:
                if "429" in str(e) or "Throttled" in str(e):
                    backoff = min(60, sleep_seconds * 2)
                    logger.warning("Report status throttled, backing off %ds (attempt %d)", backoff, attempt)
                    time.sleep(backoff)
                    continue
                raise

            state = status.get("status")
            if state in ("SUCCESS", "COMPLETED"):
                location = status.get("location") or status.get("url")
                if not location:
                    raise RuntimeError(f"Report SUCCESS but no download URL: {status}")
                return location
            if state in {"FAILURE", "CANCELLED"}:
                raise RuntimeError(f"Report failed: {status}")
            time.sleep(sleep_seconds)

        raise RuntimeError(f"Report generation timed out for reportId={report_id}")


def keyword_create_rows(
    keywords: List[str],
    campaign_id: int,
    ad_group_id: int,
    bid_map: Dict[str, float],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for kw in keywords:
        base_bid = float(bid_map.get(kw) or DEFAULT_WINNER_BID)
        rows.append({
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "keywordText": kw,
            "matchType": "EXACT",
            "state": "ENABLED",
            "bid": round(base_bid * 1.15, 2),
        })
        rows.append({
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "keywordText": kw,
            "matchType": "PHRASE",
            "state": "ENABLED",
            "bid": round(base_bid, 2),
        })
        rows.append({
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "keywordText": kw,
            "matchType": "BROAD",
            "state": "ENABLED",
            "bid": round(base_bid * 0.85, 2),
        })
    return rows


def negative_keyword_rows(negatives: List[str], campaign_id: int) -> List[Dict[str, Any]]:
    return [{
        "campaignId": str(campaign_id),
        "keywordText": term,
        "matchType": "negativeExact",
        "state": "ENABLED",
    } for term in negatives]


def build_dashboard_cache(lookback_days: int = 14) -> Dict[str, Any]:
    client = AmazonAdsClient()
    campaigns = client.list_campaigns()
    mode = get_bid_mode()
    start_date = iso_date_days_ago(lookback_days)
    end_date = today_iso_date()

    report_id = client.request_report({
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": ["campaignId", "campaignName", "impressions", "clicks", "cost", "sales7d", "purchases7d"],
            "reportTypeId": "spCampaigns",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        }
    })
    report_url = client.wait_for_report(report_id)
    rows = parse_report_json_bytes(client.download_binary(report_url))

    summary_by_campaign: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = str(row.get("campaignId") or "")
        if not cid:
            continue
        summary_by_campaign[cid] = {
            "impressions": int(num(row, ["impressions"], 0)),
            "clicks": int(num(row, ["clicks"], 0)),
            "spend": round(num(row, ["cost", "spend"], 0), 2),
            "sales": round(num(row, ["sales7d", "sales"], 0), 2),
            "orders": int(num(row, ["purchases7d", "orders"], 0)),
        }

    results: List[Dict[str, Any]] = []
    total_spend = 0.0
    total_sales = 0.0
    total_clicks = 0
    total_orders = 0

    for c in campaigns:
        cid = str(c.get("campaignId") or "")
        summary = summary_by_campaign.get(cid, {})
        spend = float(summary.get("spend", 0))
        sales = float(summary.get("sales", 0))
        clicks = int(summary.get("clicks", 0))
        orders = int(summary.get("orders", 0))
        acos = round(spend / sales, 4) if sales > 0 else None

        total_spend += spend
        total_sales += sales
        total_clicks += clicks
        total_orders += orders

        bid_low = None
        bid_high = None
        applied = None

        try:
            keywords = client.list_keywords(cid)
        except Exception:
            keywords = []

        if keywords:
            kw = keywords[0]
            rec = client.get_bid_recommendation(
                cid,
                str(kw.get("adGroupId") or ""),
                str(kw.get("keywordText") or ""),
                str(kw.get("matchType") or "PHRASE"),
            )
            low, high, applied_bid = choose_bid(rec, float(kw.get("bid") or DEFAULT_FALLBACK_BID))
            bid_low = low
            bid_high = high
            applied = applied_bid

        results.append({
            **c,
            "impressions": int(summary.get("impressions", 0)),
            "clicks": clicks,
            "spend": round(spend, 2),
            "sales": round(sales, 2),
            "orders": orders,
            "acos": acos,
            "amazonSuggestedBidLow": bid_low,
            "amazonSuggestedBidHigh": bid_high,
            "currentAppliedBid": applied,
            "currentBidMode": mode,
        })

    cache = {
        "campaigns": results,
        "count": len(results),
        "bid_mode": mode,
        "peak_hours_label": f"{PEAK_START}:00–{PEAK_END}:59",
        "note": "Cached dashboard data from Amazon reports.",
        "refreshed_at": utc_now_iso(),
        "report_id": report_id,
        "summary": {
            "spend": round(total_spend, 2),
            "sales": round(total_sales, 2),
            "clicks": total_clicks,
            "orders": total_orders,
            "acos": round(total_spend / total_sales, 4) if total_sales > 0 else None,
        },
    }
    write_cache(cache)
    return cache


GCS_BUCKET = os.getenv("GCS_BUCKET", "amazon-ppc-bid-optimizer-state")
GCS_REPORT_KEY = "pending_report.json"

def save_pending_report(report_id: str, start_date: str, end_date: str, params: dict) -> None:
    try:
        client = gcs_storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_REPORT_KEY)
        blob.upload_from_string(json.dumps({
            "report_id": report_id,
            "start_date": start_date,
            "end_date": end_date,
            "params": params,
            "requested_at": utc_now_iso(),
        }))
        logger.info("Saved pending report %s to GCS", report_id)
    except Exception as e:
        logger.warning("Failed to save pending report to GCS: %s", e)

def load_pending_report() -> Optional[dict]:
    try:
        client = gcs_storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_REPORT_KEY)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    except Exception as e:
        logger.warning("Failed to load pending report from GCS: %s", e)
        return None

def clear_pending_report() -> None:
    try:
        client = gcs_storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_REPORT_KEY)
        if blob.exists():
            blob.delete()
    except Exception as e:
        logger.warning("Failed to clear pending report from GCS: %s", e)

def fetch_search_term_classification(lookback_days: int) -> Tuple[AmazonAdsClient, Dict[str, Any], str, str, List[Dict[str, Any]]]:
    client = AmazonAdsClient()
    start_date = iso_date_days_ago(lookback_days)
    end_date = today_iso_date()

    report_id = client.request_report({
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["searchTerm"],
            "columns": ["campaignId", "adGroupId", "searchTerm", "clicks", "cost", "sales7d", "purchases7d"],
            "reportTypeId": "spSearchTerm",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        }
    })
    report_url = client.wait_for_report(report_id)
    rows = parse_report_json_bytes(client.download_binary(report_url))
    classified = classify_terms(rows)
    return client, classified, report_id, start_date, end_date


def apply_negatives_step(client: AmazonAdsClient, classified: Dict[str, Any]) -> List[Dict[str, Any]]:
    negatives_applied: List[Dict[str, Any]] = []
    if not classified["negatives"]:
        return negatives_applied

    by_campaign: Dict[int, List[str]] = {}
    for item in classified["negatives"]:
        cid = item.get("campaign_id")
        if cid:
            by_campaign.setdefault(cid, []).append(item["term"])

    for cid, terms in by_campaign.items():
        neg_terms = unique_in_order(terms)
        neg_rows = negative_keyword_rows(neg_terms, cid)
        if neg_rows:
            client.create_negative_keywords(neg_rows)
            negatives_applied.append({
                "campaign_id": cid,
                "count": len(neg_rows),
                "terms_sample": neg_terms[:10],
            })

    return negatives_applied


def apply_winners_step(client: AmazonAdsClient, classified: Dict[str, Any], fallback_winner_bid: float) -> List[Dict[str, Any]]:
    winners_applied: List[Dict[str, Any]] = []
    if not classified["winners"]:
        return winners_applied

    by_adgroup: Dict[int, Dict[str, Any]] = {}
    for item in classified["winners"]:
        agid = item.get("ad_group_id")
        cid = item.get("campaign_id")
        if agid and cid:
            by_adgroup.setdefault(agid, {"campaign_id": cid, "terms": []})
            by_adgroup[agid]["terms"].append(item["term"])

    for agid, data in by_adgroup.items():
        terms = unique_in_order(data["terms"])
        bid_map: Dict[str, float] = {}
        bid_details: List[Dict[str, Any]] = []

        for term in terms:
            rec = client.get_bid_recommendation(
                str(data["campaign_id"]),
                str(agid),
                term,
                "PHRASE",
            )
            low, high, applied = choose_bid(rec, fallback_winner_bid)
            bid_map[term] = applied
            bid_details.append({
                "term": term,
                "suggested_low": low,
                "suggested_high": high,
                "applied_bid": applied,
                "bid_mode": get_bid_mode(),
            })

        rows_to_create = keyword_create_rows(
            terms,
            int(data["campaign_id"]),
            int(agid),
            bid_map,
        )
        if rows_to_create:
            client.create_keywords(rows_to_create)
            winners_applied.append({
                "campaign_id": data["campaign_id"],
                "ad_group_id": agid,
                "count": len(terms),
                "terms_sample": terms[:10],
                "bid_details": bid_details[:10],
            })

    return winners_applied


def retune_existing_bids_step(client: AmazonAdsClient) -> List[Dict[str, Any]]:
    bids_updated: List[Dict[str, Any]] = []
    campaigns = client.list_campaigns()

    for c in campaigns[:50]:
        cid = str(c.get("campaignId") or "")
        if not cid:
            continue

        try:
            keywords = client.list_keywords(cid)
        except Exception:
            continue

        update_rows: List[Dict[str, Any]] = []
        for kw in keywords[:100]:
            keyword_id = kw.get("keywordId")
            ad_group_id = kw.get("adGroupId")
            keyword_text = kw.get("keywordText")
            match_type = kw.get("matchType")
            current_bid = float(kw.get("bid") or DEFAULT_FALLBACK_BID)

            # Skip any keyword missing required fields
            if not keyword_id or not ad_group_id or not keyword_text or not match_type:
                continue

            # Normalize match type to valid Amazon values
            match_type = match_type.upper()
            if match_type not in ("EXACT", "PHRASE", "BROAD"):
                continue

            rec = client.get_bid_recommendation(
                cid,
                str(ad_group_id),
                str(keyword_text),
                str(match_type),
            )
            low, high, applied = choose_bid(rec, current_bid)

            # PUT payload: keywordId + bid + state ONLY (no keywordText/matchType — causes 400)
            update_rows.append({
                "keywordId": str(keyword_id),
                "bid": applied,
                "state": kw.get("state", "ENABLED"),
            })

            bids_updated.append({
                "campaign_id": cid,
                "keyword_id": str(keyword_id),
                "keyword_text": str(keyword_text),
                "match_type": str(match_type),
                "suggested_low": low,
                "suggested_high": high,
                "applied_bid": applied,
                "bid_mode": get_bid_mode(),
            })

        if update_rows:
            client.update_keywords(update_rows)

    return bids_updated


# ========================= PRODUCTS =========================
PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv",
)

_products_cache: Optional[list] = None
_products_cache_ts: float = 0.0

def load_products() -> list:
    global _products_cache, _products_cache_ts
    import time
    if _products_cache and (time.time() - _products_cache_ts) < 900:
        return _products_cache
    r = requests.get(PRODUCTS_CSV_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    rows = [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    _products_cache = rows
    _products_cache_ts = time.time()
    return rows

def normalized_product(p: dict) -> dict:
    price = p.get("Selling_Price", "")
    try:
        pf = float(str(price).replace("$","").replace(",","").strip())
    except:
        pf = 25.0
    if pf < 15: budget, bid = 12.0, 0.55
    elif pf < 25: budget, bid = 18.0, 0.75
    elif pf < 40: budget, bid = 25.0, 0.95
    else: budget, bid = 35.0, 1.10
    active = str(p.get("Active","TRUE")).strip().lower() in {"true","yes","1","y","active"}
    return {
        "product_id": p.get("Product_ID",""),
        "sku": p.get("SKU",""),
        "asin": p.get("ASIN",""),
        "title": p.get("Title",""),
        "price": price,
        "active": active,
        "category": p.get("Category",""),
        "suggested_budget": budget,
        "suggested_bid": bid,
    }

@app.get("/api/products")
def api_products() -> JSONResponse:
    try:
        raw_rows = load_products()
        rows = clean_product_rows(raw_rows)
        products = [normalized_product(r) for r in rows]
        return JSONResponse({
            "raw_count": len(raw_rows),
            "count": len(products),
            "products": products
        })
    except Exception as e:
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)

@app.get("/api/campaign-plan")
def api_campaign_plan():
    return build_all_campaign_plans()   

@app.post("/api/create-campaign-from-product")
def api_create_campaign(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    # Token optional — dashboard sends it when available but don't block if absent
    token = os.getenv("DAILY_OPTIMIZER_TOKEN", "")
    if token:
        supplied = None
        if x_daily_optimizer_token:
            supplied = x_daily_optimizer_token.strip()
        elif authorization and authorization.startswith("Bearer "):
            supplied = authorization.replace("Bearer ", "", 1).strip()
        if supplied and not hmac.compare_digest(supplied, token):
            return JSONResponse({"error": True, "message": "Invalid token"}, status_code=403)
    try:
        key = (payload.get("product_id") or payload.get("sku") or "").lower().strip()
        if not key:
            return JSONResponse({"error": True, "message": "product_id or sku required"}, status_code=400)
        product = next((normalized_product(r) for r in load_products()
                        if r.get("Product_ID","").lower()==key or r.get("SKU","").lower()==key), None)
        if not product:
            return JSONResponse({"error": True, "message": "Product not found"}, status_code=404)

        client = AmazonAdsClient()
        import datetime, re, unicodedata, html as html_mod

        def sanitize(name):
            name = html_mod.unescape(name or "")
            name = unicodedata.normalize("NFKD", name).encode("ascii","ignore").decode("ascii")
            return re.sub(r"\s+", " ", name).strip()

        start_date = datetime.date.today().isoformat()
        camp_resp = client.post("/sp/campaigns", {
            "campaigns": [{
                "name": f"{sanitize(product['title'])[:80]} | MANUAL | {start_date}",
                "targetingType": "MANUAL",
                "state": "ENABLED",
                "budget": {"budget": product["suggested_budget"], "budgetType": "DAILY"},
                "startDate": start_date,
            }]
        }, content_type="application/vnd.spcampaign.v3+json",
           accept="application/vnd.spcampaign.v3+json")

        # Safely extract campaign ID — handle empty success list or alternate response shapes
        def extract_id(resp, batch_key, item_key, id_key):
            """Pull an ID out of v3 batch response regardless of nesting shape."""
            # Shape 1: {batch_key: {success: [{item_key: {id_key: X}}]}}
            inner = resp.get(batch_key, {})
            if isinstance(inner, dict):
                success = inner.get("success", [])
                if success:
                    return (success[0].get(item_key) or success[0]).get(id_key)
            # Shape 2: {batch_key: [{id_key: X}]}
            if isinstance(inner, list) and inner:
                return inner[0].get(id_key)
            # Shape 3: flat {id_key: X}
            return resp.get(id_key)

        camp_id = extract_id(camp_resp, "campaigns", "campaign", "campaignId")
        if not camp_id:
            return JSONResponse({"error": True, "message": f"Campaign creation failed: {camp_resp}"}, status_code=500)

        ag_resp = client.post("/sp/adGroups", {
            "adGroups": [{
                "name": "Main Ad Group",
                "campaignId": str(camp_id),
                "state": "ENABLED",
                "defaultBid": product["suggested_bid"],
            }]
        }, content_type="application/vnd.spadgroup.v3+json",
           accept="application/vnd.spadgroup.v3+json")

        ag_id = extract_id(ag_resp, "adGroups", "adGroup", "adGroupId")

        client.post("/sp/productAds", {
            "productAds": [{
                "campaignId": str(camp_id),
                "adGroupId": str(ag_id),
                "asin": product["asin"],
                "state": "ENABLED",
            }]
        }, content_type="application/vnd.spproductad.v3+json",
           accept="application/vnd.spproductad.v3+json")

        return JSONResponse({
            "success": True,
            "campaign_id": camp_id,
            "ad_group_id": ag_id,
            "product": product["title"],
            "asin": product["asin"],
            "daily_budget": product["suggested_budget"],
            "starting_bid": product["suggested_bid"],
        })
    except Exception as e:
        logger.exception("Campaign creation failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    path = BASE_DIR / "templates" / "dashboard.html"
    try:
        html = path.read_text(encoding="utf-8")
        # Inject token so browser never prompts — replaced at serve time
        token = os.getenv("DAILY_OPTIMIZER_TOKEN", "")
        html = html.replace(
            "/* __SERVER_TOKEN__ */",
            "var SERVER_TOKEN = " + json.dumps(token) + ";",
        )
        return HTMLResponse(html)
    except Exception as e:
        logger.exception("Dashboard load failed")
        return HTMLResponse(
            f"<h2>Dashboard Error</h2><p>{str(e)}</p><p>Path: {path}</p>",
            status_code=500,
        )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/optimizer-history")
def optimizer_history() -> Dict[str, Any]:
    history = load_optimizer_history()
    return {"history": history, "count": len(history)}


@app.get("/api/bid-recommendation")
def api_bid_recommendation(asin: str = "", keyword: str = "fertilizer") -> JSONResponse:
    """Return live Amazon suggested bid range for a product/keyword.
    Used by the launch modal to pre-fill bids based on current peak/off-peak mode.
    """
    try:
        client = AmazonAdsClient()
        mode = get_bid_mode()

        # Try to find the first campaign/ad-group to use as context for the recommendation
        try:
            campaigns = client.list_campaigns()
            camp = campaigns[0] if campaigns else None
            cid = str(camp.get("campaignId", "")) if camp else ""
            kws = client.list_keywords(cid) if cid else []
            kw = kws[0] if kws else {}
            ag_id = str(kw.get("adGroupId", "")) if kw else ""
            kw_text = kw.get("keywordText", keyword) or keyword
            match_type = kw.get("matchType", "PHRASE") or "PHRASE"
        except Exception:
            cid, ag_id, kw_text, match_type = "", "", keyword, "PHRASE"

        rec = {}
        if cid and ag_id:
            rec = client.get_bid_recommendation(cid, ag_id, kw_text, match_type)

        low  = rec.get("low")
        high = rec.get("high")
        suggested = rec.get("suggested")

        # Choose applied bid based on mode
        if low and high:
            applied = high if mode == "PEAK" else (low if mode == "OFF_PEAK" else round((low + high) / 2, 2))
        else:
            applied = None

        return JSONResponse({
            "bid_mode": mode,
            "peak_hours_label": f"{PEAK_START}:00-{PEAK_END}:59 EST",
            "low": low,
            "high": high,
            "suggested": suggested,
            "applied": applied,
            "note": "live" if rec else "fallback-no-recommendation-available",
        })
    except Exception as e:
        logger.warning("Bid recommendation endpoint failed: %s", e)
        mode = get_bid_mode()
        return JSONResponse({"bid_mode": mode, "low": None, "high": None, "suggested": None, "applied": None, "note": str(e)})


@app.get("/api/dashboard-data")
def dashboard_data() -> JSONResponse:
    cache = read_cache()
    if not cache.get("campaigns"):
        try:
            logger.info("Cache empty — auto-rebuilding dashboard cache")
            cache = build_dashboard_cache(lookback_days=14)
        except Exception as e:
            logger.warning("Auto cache rebuild failed: %s", e)
    return JSONResponse(cache)


@app.post("/api/refresh-dashboard-cache")
def refresh_dashboard_cache(
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        cache = build_dashboard_cache(lookback_days=14)
        return JSONResponse({"success": True, "cache": cache})
    except Exception as e:
        logger.exception("Dashboard cache refresh failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)


@app.post("/api/apply-negatives")
def apply_negatives(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        lookback_days = int(payload.get("lookback_days", 14))
        refresh_cache_after = bool(payload.get("refresh_cache_after", True))

        client, classified, report_id, start_date, end_date = fetch_search_term_classification(lookback_days)
        negatives_applied = apply_negatives_step(client, classified)

        cache_result = None
        if refresh_cache_after:
            try:
                cache_result = build_dashboard_cache(lookback_days=14)
            except Exception as e:
                logger.warning("Cache refresh after negatives failed: %s", e)

        entry = {
            "timestamp": utc_now_iso(),
            "action": "apply_negatives",
            "lookback_days": lookback_days,
            "negatives_found": len(classified["negatives"]),
            "negatives_applied_count": len(negatives_applied),
        }
        save_optimizer_history(entry)

        return JSONResponse({
            "success": True,
            "action": "apply_negatives",
            "report_id": report_id,
            "date_range": {"start": start_date, "end": end_date},
            "negatives_found": len(classified["negatives"]),
            "negatives_applied_count": len(negatives_applied),
            "negatives_applied": negatives_applied,
            "cache_refreshed": cache_result is not None,
        })
    except Exception as e:
        logger.exception("Apply negatives failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)


@app.post("/api/apply-winners")
def apply_winners(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        lookback_days = int(payload.get("lookback_days", 14))
        fallback_winner_bid = float(payload.get("winner_bid", DEFAULT_WINNER_BID))
        refresh_cache_after = bool(payload.get("refresh_cache_after", True))

        client, classified, report_id, start_date, end_date = fetch_search_term_classification(lookback_days)
        winners_applied = apply_winners_step(client, classified, fallback_winner_bid)

        cache_result = None
        if refresh_cache_after:
            try:
                cache_result = build_dashboard_cache(lookback_days=14)
            except Exception as e:
                logger.warning("Cache refresh after winners failed: %s", e)

        entry = {
            "timestamp": utc_now_iso(),
            "action": "apply_winners",
            "lookback_days": lookback_days,
            "winners_found": len(classified["winners"]),
            "winners_applied_count": len(winners_applied),
        }
        save_optimizer_history(entry)

        return JSONResponse({
            "success": True,
            "action": "apply_winners",
            "report_id": report_id,
            "date_range": {"start": start_date, "end": end_date},
            "winners_found": len(classified["winners"]),
            "winners_applied_count": len(winners_applied),
            "winners_applied": winners_applied,
            "cache_refreshed": cache_result is not None,
        })
    except Exception as e:
        logger.exception("Apply winners failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)


@app.post("/api/apply-optimization")
def apply_optimization(
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Phase 2: download completed report and apply winners/negatives."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        pending = load_pending_report()
        if not pending:
            return JSONResponse({"error": True, "message": "No pending report found in GCS"}, status_code=404)

        report_id = pending["report_id"]
        params = pending.get("params", {})
        start_date = pending["start_date"]
        end_date = pending["end_date"]

        client = AmazonAdsClient()

        # Check status with throttle retry
        status = None
        for attempt in range(5):
            try:
                status = client.get_json(f"/reporting/reports/{report_id}", accept=MEDIA_TYPES["json"])
                break
            except RuntimeError as e:
                if "429" in str(e) or "Throttled" in str(e):
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
        if status is None:
            return JSONResponse({"error": True, "message": "Report status check throttled, try again in a few minutes"}, status_code=429)

        state = status.get("status")
        if state not in ("SUCCESS", "COMPLETED"):
            return JSONResponse({
                "error": True,
                "message": f"Report not ready yet (status={state}). Try again later.",
                "report_id": report_id,
            }, status_code=202)

        location = status.get("location") or status.get("url")
        if not location:
            return JSONResponse({"error": True, "message": "Report SUCCESS but no download URL"}, status_code=500)

        rows = parse_report_json_bytes(client.download_binary(location))
        classified = classify_terms(rows)

        negatives_applied = []
        winners_applied = []

        if params.get("apply_negatives_live", True):
            negatives_applied = apply_negatives_step(client, classified)

        if params.get("apply_winners_live", True):
            winners_applied = apply_winners_step(client, classified, float(params.get("winner_bid", DEFAULT_WINNER_BID)))

        clear_pending_report()

        entry = {
            "timestamp": utc_now_iso(),
            "action": "apply_optimization",
            "report_id": report_id,
            "date_range": {"start": start_date, "end": end_date},
            "rows_analyzed": len(rows),
            "winners_found": len(classified["winners"]),
            "negatives_found": len(classified["negatives"]),
            "winners_applied_count": len(winners_applied),
            "negatives_applied_count": len(negatives_applied),
            "bid_mode": get_bid_mode(),
        }
        save_optimizer_history(entry)

        return JSONResponse({
            "success": True,
            "action": "apply_optimization",
            "report_id": report_id,
            "rows_analyzed": len(rows),
            "winners_found": len(classified["winners"]),
            "negatives_found": len(classified["negatives"]),
            "winners_applied_count": len(winners_applied),
            "negatives_applied_count": len(negatives_applied),
            "winners_applied": winners_applied,
            "negatives_applied": negatives_applied,
        })
    except Exception as e:
        logger.exception("Apply optimization failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)


@app.post("/api/retune-existing-bids")
def retune_existing_bids(
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        client = AmazonAdsClient()
        bids_updated = retune_existing_bids_step(client)

        entry = {
            "timestamp": utc_now_iso(),
            "action": "retune_existing_bids",
            "existing_keyword_bids_updated": len(bids_updated),
            "bid_mode": get_bid_mode(),
            "sample": [
                f"{b['keyword_text']} {b['match_type']} -> {b['applied_bid']}"
                for b in bids_updated[:3]
            ],
        }
        save_optimizer_history(entry)

        return JSONResponse({
            "success": True,
            "action": "retune_existing_bids",
            "existing_keyword_bids_updated": len(bids_updated),
            "bids_updated_sample": bids_updated[:20],
        })
    except Exception as e:
        logger.exception("Retune existing bids failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)


@app.post("/api/run-daily-optimization")
def run_daily_optimization(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)

    try:
        apply_negatives_live = bool(payload.get("apply_negatives_live", True))
        apply_winners_live = bool(payload.get("apply_winners_live", True))
        retune_existing = bool(payload.get("retune_existing_keyword_bids", False))
        lookback_days = int(payload.get("lookback_days", 14))
        fallback_winner_bid = float(payload.get("winner_bid", DEFAULT_WINNER_BID))

        client = AmazonAdsClient()
        start_date = iso_date_days_ago(lookback_days)
        end_date = today_iso_date()

        report_id = client.request_report({
            "startDate": start_date,
            "endDate": end_date,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS",
                "groupBy": ["searchTerm"],
                "columns": ["campaignId", "adGroupId", "searchTerm", "clicks", "cost", "sales7d", "purchases7d"],
                "reportTypeId": "spSearchTerm",
                "timeUnit": "SUMMARY",
                "format": "GZIP_JSON",
            }
        })

        save_pending_report(report_id, start_date, end_date, {
            "apply_negatives_live": apply_negatives_live,
            "apply_winners_live": apply_winners_live,
            "retune_existing_keyword_bids": retune_existing,
            "winner_bid": fallback_winner_bid,
        })

        entry = {
            "timestamp": utc_now_iso(),
            "action": "report_requested",
            "report_id": report_id,
            "lookback_days": lookback_days,
        }
        save_optimizer_history(entry)

        return JSONResponse({
            "success": True,
            "action": "report_requested",
            "report_id": report_id,
            "message": "Report requested. Run /api/apply-optimization in 30-60 minutes to apply results.",
            "date_range": {"start": start_date, "end": end_date},
        })
    except Exception as e:
        logger.exception("Daily optimization failed")
        return JSONResponse({"error": True, "message": str(e)}, status_code=500)

from optimized_launch_endpoint import register_optimized_launch_routes

register_optimized_launch_routes(
    app,
    AmazonAdsClient=AmazonAdsClient,
    MEDIA_TYPES=MEDIA_TYPES,
    DEFAULT_FALLBACK_BID=DEFAULT_FALLBACK_BID,
    verify_internal_token=verify_internal_token,
    get_bid_mode=get_bid_mode,
    today_iso_date=today_iso_date,
    utc_now_iso=utc_now_iso,
    save_optimizer_history=save_optimizer_history,
    logger=logger,
)
