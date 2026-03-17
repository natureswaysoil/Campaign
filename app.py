import csv
import gzip
import io
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


# ============================================================
# Config / Secret loading
# ============================================================

USE_SECRET_MANAGER = True
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")


def get_secret(project_id: str, secret_id: str) -> str:
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def load_env_or_secret(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name)
    if val:
        return val
    if USE_SECRET_MANAGER and GCP_PROJECT_ID:
        try:
            return get_secret(GCP_PROJECT_ID, name)
        except Exception:
            pass
    if default is not None:
        return default
    raise RuntimeError(f"Missing required config: {name}")


def optional_env_or_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        return load_env_or_secret(name, default=default)
    except Exception:
        return default


# ============================================================
# Amazon Ads config
# IMPORTANT:
# Verify these endpoint paths and report field names against your
# current Amazon Ads API version before using live.
# ============================================================

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
BASE_URLS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}

ENDPOINTS = {
    "campaigns": "/sp/campaigns",
    "ad_groups": "/sp/adGroups",
    "product_ads": "/sp/productAds",
    "keywords": "/sp/keywords",
    "negative_keywords": "/sp/negativeKeywords",
    "reports": "/reporting/reports",  # verify for your account/API version
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "our", "this", "that",
    "soil", "organic", "liquid", "natural", "safe", "kids", "pets", "beneficial",
    "plants", "plant", "garden", "lawn", "yards", "yard", "made", "makes",
    "concentrate", "revitalizer", "treatment", "formula", "product", "products",
    "nature", "way"
}


def today_yyyymmdd() -> str:
    return time.strftime("%Y%m%d")


def yyyymmdd_days_ago(days: int) -> str:
    return time.strftime("%Y%m%d", time.localtime(time.time() - (days * 86400)))


def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def unique_in_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_seed_keywords(product_name: str) -> List[str]:
    clean = normalize_text(product_name)
    words = [w for w in clean.split() if w not in STOPWORDS and len(w) > 2]

    phrases = [clean]
    for n in (2, 3, 4):
        for i in range(0, max(0, len(words) - n + 1)):
            phrases.append(" ".join(words[i:i+n]))

    if "dog urine" in clean:
        phrases += [
            "dog urine neutralizer",
            "dog urine lawn repair",
            "pet urine grass treatment",
            "dog spot lawn treatment",
            "grass saver for dog urine",
        ]
    if "pasture" in clean or "hay" in clean or "lawn fertilizer" in clean:
        phrases += [
            "pasture fertilizer",
            "hay fertilizer",
            "liquid lawn fertilizer",
            "grass fertilizer",
            "soil conditioner for lawn",
        ]
    if "bone meal" in clean:
        phrases += [
            "liquid bone meal",
            "phosphorus fertilizer",
            "bloom fertilizer",
            "root and flower fertilizer",
        ]
    return unique_in_order([p for p in phrases if len(p) <= 80])[:40]


def keyword_rows(keywords: List[str], ad_group_id: int, bid: float) -> List[Dict[str, Any]]:
    rows = []
    for kw in keywords:
        rows.append({
            "adGroupId": ad_group_id,
            "keywordText": kw,
            "matchType": "exact",
            "state": "enabled",
            "bid": round(bid * 1.15, 2),
        })
        rows.append({
            "adGroupId": ad_group_id,
            "keywordText": kw,
            "matchType": "phrase",
            "state": "enabled",
            "bid": round(bid * 1.00, 2),
        })
        rows.append({
            "adGroupId": ad_group_id,
            "keywordText": kw,
            "matchType": "broad",
            "state": "enabled",
            "bid": round(bid * 0.85, 2),
        })
    return rows


def negative_keyword_rows(
    negatives: List[str],
    campaign_id: int,
    ad_group_id: Optional[int] = None,
    match_type: str = "negativeExact",
) -> List[Dict[str, Any]]:
    rows = []
    for term in negatives:
        row = {
            "campaignId": campaign_id,
            "keywordText": term,
            "state": "enabled",
            "matchType": match_type,
        }
        if ad_group_id is not None:
            row["adGroupId"] = ad_group_id
        rows.append(row)
    return rows


# ============================================================
# Amazon Ads API client
# ============================================================

class AmazonAdsClient:
    def __init__(self):
        self.client_id = load_env_or_secret("AMAZON_ADS_CLIENT_ID")
        self.client_secret = load_env_or_secret("AMAZON_ADS_CLIENT_SECRET")
        self.refresh_token = load_env_or_secret("AMAZON_ADS_REFRESH_TOKEN")
        self.profile_id = load_env_or_secret("AMAZON_ADS_PROFILE_ID")
        self.region = load_env_or_secret("AMAZON_ADS_REGION", "na").lower()

        if self.region not in BASE_URLS:
            raise RuntimeError("AMAZON_ADS_REGION must be na, eu, or fe")

        self.base_url = BASE_URLS[self.region]
        self.access_token = self._get_token()
        self.session = requests.Session()

    def _get_token(self) -> str:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"Access token missing from response: {data}")
        return token

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, endpoint: str, body: Any) -> Any:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.post(url, headers=self.headers(), json=body, timeout=60)
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else None

    def get(self, endpoint: str) -> Any:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.get(url, headers=self.headers(), timeout=60)
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else None

    def download_binary(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=120)
        resp.raise_for_status()
        return resp.content

    def request_sp_search_term_report(self, start_date: str, end_date: str) -> Any:
        body = {
            "name": f"sp-search-term-{start_date}-{end_date}",
            "startDate": start_date,
            "endDate": end_date,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS",
                "reportTypeId": "spSearchTerm",
                "columns": [
                    "campaignId",
                    "adGroupId",
                    "keywordId",
                    "searchTerm",
                    "clicks",
                    "cost",
                    "sales7d",
                    "purchases7d",
                ],
                "timeUnit": "SUMMARY",
                "format": "GZIP_JSON",
            },
        }
        return self.post(ENDPOINTS["reports"], body)

    def get_report_status(self, report_id: str) -> Any:
        return self.get(f"{ENDPOINTS['reports']}/{report_id}")


def extract_first_id(payload: Any) -> int:
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Unexpected payload: {payload}")
    row = payload[0]
    for key in ("campaignId", "adGroupId", "keywordId", "adId", "id"):
        if key in row:
            return int(row[key])
    raise RuntimeError(f"No ID found in payload: {payload}")


# ============================================================
# Report parsing + optimization
# ============================================================

def parse_report_csv(csv_text: str) -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    return [{str(k).strip(): v for k, v in row.items()} for row in reader]


def parse_report_json_bytes(content: bytes) -> List[Dict[str, Any]]:
    try:
        decompressed = gzip.decompress(content)
    except OSError:
        decompressed = content
    data = json.loads(decompressed.decode("utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "rows" in data and isinstance(data["rows"], list):
        return data["rows"]
    raise RuntimeError("Unsupported report payload format")


def num(row: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            try:
                value = str(row[key]).replace("$", "").replace(",", "").strip()
                return float(value)
            except Exception:
                continue
    return default


def text(row: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return default


def classify_terms(
    rows: List[Dict[str, Any]],
    min_clicks_for_negative: int = 20,
    min_orders_for_winner: int = 2,
    max_acos_for_winner: float = 0.35,
    min_clicks_for_winner: int = 8,
) -> Dict[str, Any]:
    winners, negatives, hold = [], [], []

    for row in rows:
        term = text(row, ["Customer Search Term", "searchTerm", "Search Term", "customer_search_term"])
        campaign_id = int(num(row, ["Campaign Id", "campaignId"], 0))
        ad_group_id = int(num(row, ["Ad Group Id", "adGroupId"], 0))
        clicks = int(num(row, ["Clicks", "clicks"], 0))
        cost = num(row, ["Spend", "Cost", "cost", "spend"], 0.0)
        sales = num(row, ["7 Day Total Sales", "14 Day Total Sales", "Sales", "sales", "sales7d"], 0.0)
        orders = int(num(row, ["7 Day Total Orders (#)", "14 Day Total Orders (#)", "Orders", "orders", "purchases7d"], 0))

        acos = (cost / sales) if sales > 0 else None
        result = {
            "term": term,
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "clicks": clicks,
            "orders": orders,
            "cost": round(cost, 2),
            "sales": round(sales, 2),
            "acos": round(acos, 4) if acos is not None else None,
        }

        if not term:
            hold.append({**result, "reason": "empty search term"})
            continue

        if orders >= min_orders_for_winner and clicks >= min_clicks_for_winner and sales > 0:
            if acos is None or acos <= max_acos_for_winner:
                winners.append({**result, "reason": "meets winner thresholds"})
                continue

        if clicks >= min_clicks_for_negative and orders == 0:
            negatives.append({**result, "reason": ">= minimum clicks with zero orders"})
            continue

        hold.append({**result, "reason": "insufficient data or mixed performance"})

    return {"winners": winners, "negatives": negatives, "hold": hold}


def adjust_bid(current_bid: float, acos: Optional[float]) -> float:
    if acos is None:
        return current_bid
    if acos < 0.20:
        return round(current_bid * 1.15, 2)
    if acos > 0.40:
        return round(current_bid * 0.80, 2)
    return round(current_bid, 2)


def optimize_rows(
    report_rows: List[Dict[str, Any]],
    min_clicks_for_negative: int,
    min_orders_for_winner: int,
    max_acos_for_winner: float,
    min_clicks_for_winner: int,
) -> Dict[str, Any]:
    summary = classify_terms(
        report_rows,
        min_clicks_for_negative=min_clicks_for_negative,
        min_orders_for_winner=min_orders_for_winner,
        max_acos_for_winner=max_acos_for_winner,
        min_clicks_for_winner=min_clicks_for_winner,
    )
    return {
        "summary_counts": {
            "rows": len(report_rows),
            "winners": len(summary["winners"]),
            "negatives": len(summary["negatives"]),
            "hold": len(summary["hold"]),
        },
        **summary,
    }


# ============================================================
# Auth helper for scheduled route
# ============================================================

def verify_internal_token(authorization: Optional[str]) -> None:
    required = optional_env_or_secret("DAILY_OPTIMIZER_TOKEN")
    if not required:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.replace("Bearer ", "", 1).strip()
    if supplied != required:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(title="Amazon Ads Autopilot")


class CampaignRequest(BaseModel):
    sku: str
    asin: str
    product_name: str
    daily_budget: float = Field(gt=0)
    default_bid: float = Field(gt=0)
    mode: str = Field(default="manual", pattern="^(manual|auto)$")


class OptimizeReportRequest(BaseModel):
    csv_text: str
    min_clicks_for_negative: int = 20
    min_orders_for_winner: int = 2
    max_acos_for_winner: float = 0.35
    min_clicks_for_winner: int = 8
    apply_negatives_live: bool = False
    apply_winners_live: bool = False
    winner_bid: Optional[float] = None


class DailyOptimizationRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    min_clicks_for_negative: int = 20
    min_orders_for_winner: int = 2
    max_acos_for_winner: float = 0.35
    min_clicks_for_winner: int = 8
    apply_negatives_live: bool = True
    apply_winners_live: bool = True
    winner_bid: float = 0.90
    report_poll_seconds: int = 20
    report_poll_attempts: int = 18


class KeywordPromotionRequest(BaseModel):
    terms: List[str]
    ad_group_id: int
    bid: float = Field(gt=0)


class NegativeRequest(BaseModel):
    terms: List[str]
    campaign_id: int
    ad_group_id: Optional[int] = None
    match_type: str = "negativeExact"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/create-campaign")
def create_campaign(req: CampaignRequest):
    try:
        client = AmazonAdsClient()
        start_date = today_yyyymmdd()

        campaign = client.post(ENDPOINTS["campaigns"], [{
            "name": f"{req.product_name} | {req.mode.upper()} | {start_date}",
            "campaignType": "sponsoredProducts",
            "targetingType": req.mode,
            "state": "enabled",
            "dailyBudget": round(req.daily_budget, 2),
            "startDate": start_date,
        }])
        campaign_id = extract_first_id(campaign)

        ad_group = client.post(ENDPOINTS["ad_groups"], [{
            "name": "Main Ad Group",
            "campaignId": campaign_id,
            "state": "enabled",
            "defaultBid": round(req.default_bid, 2),
        }])
        ad_group_id = extract_first_id(ad_group)

        client.post(ENDPOINTS["product_ads"], [{
            "campaignId": campaign_id,
            "adGroupId": ad_group_id,
            "asin": req.asin,
            "sku": req.sku,
            "state": "enabled",
        }])

        generated_keywords = []
        if req.mode == "manual":
            generated_keywords = build_seed_keywords(req.product_name)
            client.post(ENDPOINTS["keywords"], keyword_rows(generated_keywords, ad_group_id, req.default_bid))

        return {
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "keywords_created": len(generated_keywords),
            "generated_keywords": generated_keywords,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/optimize-from-report")
def optimize_from_report(req: OptimizeReportRequest):
    try:
        report_rows = parse_report_csv(req.csv_text)
        result = optimize_rows(
            report_rows=report_rows,
            min_clicks_for_negative=req.min_clicks_for_negative,
            min_orders_for_winner=req.min_orders_for_winner,
            max_acos_for_winner=req.max_acos_for_winner,
            min_clicks_for_winner=req.min_clicks_for_winner,
        )

        live_actions = {"negative_terms_added": [], "winner_terms_promoted": []}

        if req.apply_negatives_live or req.apply_winners_live:
            client = AmazonAdsClient()

            if req.apply_negatives_live:
                grouped_negatives: Dict[tuple, List[str]] = {}
                for item in result["negatives"]:
                    key = (item["campaign_id"], item["ad_group_id"])
                    grouped_negatives.setdefault(key, []).append(item["term"])

                for (campaign_id, ad_group_id), terms in grouped_negatives.items():
                    rows = negative_keyword_rows(unique_in_order(terms), campaign_id, ad_group_id or None, "negativeExact")
                    client.post(ENDPOINTS["negative_keywords"], rows)
                    live_actions["negative_terms_added"].append({
                        "campaign_id": campaign_id,
                        "ad_group_id": ad_group_id,
                        "count": len(rows),
                        "terms": unique_in_order(terms),
                    })

            if req.apply_winners_live:
                grouped_winners: Dict[int, List[str]] = {}
                for item in result["winners"]:
                    grouped_winners.setdefault(item["ad_group_id"], []).append(item["term"])

                bid = req.winner_bid if req.winner_bid is not None else 0.90
                for ad_group_id, terms in grouped_winners.items():
                    rows = [{
                        "adGroupId": ad_group_id,
                        "keywordText": term,
                        "matchType": "exact",
                        "state": "enabled",
                        "bid": round(bid, 2),
                    } for term in unique_in_order(terms)]
                    client.post(ENDPOINTS["keywords"], rows)
                    live_actions["winner_terms_promoted"].append({
                        "ad_group_id": ad_group_id,
                        "count": len(rows),
                        "terms": unique_in_order(terms),
                    })

        result["live_actions"] = live_actions
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/promote-keywords")
def promote_keywords(req: KeywordPromotionRequest):
    try:
        client = AmazonAdsClient()
        rows = [{
            "adGroupId": req.ad_group_id,
            "keywordText": term,
            "matchType": "exact",
            "state": "enabled",
            "bid": round(req.bid, 2),
        } for term in unique_in_order(req.terms)]
        result = client.post(ENDPOINTS["keywords"], rows)
        return {"created": len(rows), "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/add-negatives")
def add_negatives(req: NegativeRequest):
    try:
        client = AmazonAdsClient()
        rows = negative_keyword_rows(unique_in_order(req.terms), req.campaign_id, req.ad_group_id, req.match_type)
        result = client.post(ENDPOINTS["negative_keywords"], rows)
        return {"created": len(rows), "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/run-daily-optimization")
def run_daily_optimization(
    req: DailyOptimizationRequest,
    authorization: Optional[str] = Header(default=None),
):
    verify_internal_token(authorization)
    try:
        client = AmazonAdsClient()

        end_date = req.end_date or yyyymmdd_days_ago(1)
        start_date = req.start_date or yyyymmdd_days_ago(8)

        report_job = client.request_sp_search_term_report(start_date=start_date, end_date=end_date)

        report_id = None
        if isinstance(report_job, dict):
            report_id = report_job.get("reportId") or report_job.get("id")
        if not report_id and isinstance(report_job, list) and report_job:
            report_id = report_job[0].get("reportId") or report_job[0].get("id")
        if not report_id:
            raise RuntimeError(f"Could not determine report ID from response: {report_job}")

        status_payload = None
        download_url = None
        for _ in range(req.report_poll_attempts):
            status_payload = client.get_report_status(str(report_id))
            status = ""
            location = None

            if isinstance(status_payload, dict):
                status = str(status_payload.get("status") or status_payload.get("processingStatus") or "").upper()
                location = status_payload.get("url") or status_payload.get("location") or status_payload.get("downloadUrl")

            if status in {"COMPLETED", "SUCCESS"} and location:
                download_url = location
                break

            if status in {"FAILED", "FAILURE"}:
                raise RuntimeError(f"Report failed: {status_payload}")

            time.sleep(req.report_poll_seconds)

        if not download_url:
            raise RuntimeError(f"Timed out waiting for report. Last status: {status_payload}")

        content = client.download_binary(download_url)
        rows = parse_report_json_bytes(content)

        result = optimize_rows(
            report_rows=rows,
            min_clicks_for_negative=req.min_clicks_for_negative,
            min_orders_for_winner=req.min_orders_for_winner,
            max_acos_for_winner=req.max_acos_for_winner,
            min_clicks_for_winner=req.min_clicks_for_winner,
        )

        live_actions = {"negative_terms_added": [], "winner_terms_promoted": []}

        if req.apply_negatives_live:
            grouped_negatives: Dict[tuple, List[str]] = {}
            for item in result["negatives"]:
                key = (item["campaign_id"], item["ad_group_id"])
                grouped_negatives.setdefault(key, []).append(item["term"])

            for (campaign_id, ad_group_id), terms in grouped_negatives.items():
                rows_to_add = negative_keyword_rows(unique_in_order(terms), campaign_id, ad_group_id or None, "negativeExact")
                client.post(ENDPOINTS["negative_keywords"], rows_to_add)
                live_actions["negative_terms_added"].append({
                    "campaign_id": campaign_id,
                    "ad_group_id": ad_group_id,
                    "count": len(rows_to_add),
                    "terms": unique_in_order(terms),
                })

        if req.apply_winners_live:
            grouped_winners: Dict[int, List[str]] = {}
            for item in result["winners"]:
                grouped_winners.setdefault(item["ad_group_id"], []).append(item["term"])

            for ad_group_id, terms in grouped_winners.items():
                keyword_rows_to_add = [{
                    "adGroupId": ad_group_id,
                    "keywordText": term,
                    "matchType": "exact",
                    "state": "enabled",
                    "bid": round(req.winner_bid, 2),
                } for term in unique_in_order(terms)]
                client.post(ENDPOINTS["keywords"], keyword_rows_to_add)
                live_actions["winner_terms_promoted"].append({
                    "ad_group_id": ad_group_id,
                    "count": len(keyword_rows_to_add),
                    "terms": unique_in_order(terms),
                })

        result["live_actions"] = live_actions
        result["report_window"] = {"start_date": start_date, "end_date": end_date}
        result["report_id"] = report_id
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
