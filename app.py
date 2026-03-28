from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import csv
import datetime
import gzip
import hmac
import html
import io
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import List, Dict, Any, Optional
from collections import defaultdict

import requests
from google.cloud import secretmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Amazon Ads Campaign Optimizer")

# === CRITICAL FIX: Use absolute path for templates ===
BASE_DIR = Path(__file__).parent.absolute()
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ========================= CONFIG =========================
PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv",
)

USE_SECRET_MANAGER = True
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")

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
    "negative_keywords": "/sp/campaignNegativeKeywords",
    "reports": "/reporting/reports",
}

ENDPOINT_CONTENT_TYPES = {
    "/sp/campaigns": "application/vnd.spcampaign.v3+json",
    "/sp/adGroups": "application/vnd.spadgroup.v3+json",
    "/sp/productAds": "application/vnd.spproductad.v3+json",
    "/sp/keywords": "application/vnd.spkeyword.v3+json",
    "/sp/campaignNegativeKeywords": "application/vnd.spcampaignnegativekeyword.v3+json",
}

ENDPOINT_BATCH_KEYS = {
    "/sp/campaigns": "campaigns",
    "/sp/adGroups": "adGroups",
    "/sp/productAds": "productAds",
    "/sp/keywords": "keywords",
    "/sp/campaignNegativeKeywords": "campaignNegativeKeywords",
}

BATCH_ITEM_KEYS = {
    "campaigns": "campaign",
    "adGroups": "adGroup",
    "productAds": "productAd",
    "keywords": "keyword",
    "campaignNegativeKeywords": "campaignNegativeKeyword",
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "our", "this", "that",
    "soil", "organic", "liquid", "natural", "plants", "plant", "garden", "lawn",
    "safe", "kids", "pets", "beneficial", "nature", "way"
}

OPTIMIZER_LOG_FILE = Path("/tmp/optimizer_history.json")


# ========================= HELPERS =========================
def get_secret(project_id: str, secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()


def load_env_or_secret(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name)
    if value:
        return value.strip()

    if USE_SECRET_MANAGER and GCP_PROJECT_ID:
        try:
            return get_secret(GCP_PROJECT_ID, name)
        except Exception as e:
            logger.warning(f"Secret Manager failed for {name}: {e}")

    if default is not None:
        return default

    raise RuntimeError(f"Missing required config: {name}")


def optional_env_or_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        return load_env_or_secret(name, default)
    except Exception:
        return default


def today_iso_date() -> str:
    return datetime.date.today().isoformat()


def iso_date_days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_UNICODE_TO_ASCII = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...",
    "\u00ae": "", "\u2122": "",
}


def sanitize_campaign_name(name: Optional[str]) -> str:
    name = html.unescape(name or "")
    for char, repl in _UNICODE_TO_ASCII.items():
        name = name.replace(char, repl)
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", errors="ignore").decode("ascii")
    return re.sub(r"\s+", " ", name).strip()


def unique_in_order(items: List[str]) -> List[str]:
    seen = set()
    return [item for item in items if item and item not in seen and not seen.add(item)]


def truthy(v: str) -> bool:
    return str(v).strip().lower() in {"true", "yes", "1", "y", "active"}


def budget_from_price(price_value: str) -> float:
    try:
        price = float(str(price_value).replace("$", "").replace(",", "").strip())
        if price < 15: return 12.0
        if price < 25: return 18.0
        if price < 40: return 25.0
        return 35.0
    except Exception:
        return 25.0


def bid_from_price(price_value: str) -> float:
    try:
        price = float(str(price_value).replace("$", "").replace(",", "").strip())
        if price < 15: return 0.55
        if price < 25: return 0.75
        if price < 40: return 0.95
        return 1.10
    except Exception:
        return 0.85


def parse_keyword_cell(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[\n,;|]+", value)
    return [normalize(p) for p in parts if normalize(p)]


def title_ngrams(title: str) -> List[str]:
    clean = normalize(title)
    words = [w for w in clean.split() if w not in STOPWORDS and len(w) > 2]
    phrases = [clean] if clean else []
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            phrases.append(" ".join(words[i:i + n]))
    return phrases


def keyword_hints_from_category(category: str) -> List[str]:
    c = normalize(category)
    hints = []
    if "dog" in c or "pet" in c:
        hints += ["dog urine neutralizer", "dog urine lawn repair", "pet urine grass treatment"]
    if "pasture" in c or "hay" in c or "lawn" in c:
        hints += ["pasture fertilizer", "hay fertilizer", "liquid lawn fertilizer", "grass fertilizer"]
    if "bone" in c or "bloom" in c:
        hints += ["liquid bone meal", "phosphorus fertilizer", "bloom fertilizer"]
    return hints


def generate_keywords(product: Dict[str, Any]) -> List[str]:
    merged = []
    merged.extend(parse_keyword_cell(product.get("keywords", "")))
    merged.extend(parse_keyword_cell(product.get("research_keywords", "")))
    merged.extend(title_ngrams(product.get("title", "")))
    merged.extend(keyword_hints_from_category(product.get("category", "")))

    clean_keywords = []
    seen = set()
    for kw in merged:
        kw = normalize(kw)
        if not kw or len(kw) < 3 or len(kw) > 40 or kw in seen:
            continue
        seen.add(kw)
        clean_keywords.append(kw)
    return clean_keywords[:30]


# ========================= OPTIMIZER HISTORY =========================
def load_optimizer_history() -> List[Dict]:
    if OPTIMIZER_LOG_FILE.exists():
        try:
            with open(OPTIMIZER_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_optimizer_history(entry: Dict):
    history = load_optimizer_history()
    history.append(entry)
    if len(history) > 30:
        history = history[-30:]
    try:
        with open(OPTIMIZER_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to save optimizer history: {e}")


# ========================= PRODUCT LOADING =========================
def load_products() -> List[Dict[str, str]]:
    r = requests.get(PRODUCTS_CSV_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def normalized_product(product: Dict[str, str]) -> Dict[str, Any]:
    return {
        "product_id": product.get("Product_ID", ""),
        "sku": product.get("SKU", ""),
        "asin": product.get("ASIN", ""),
        "title": product.get("Title", ""),
        "price": product.get("Selling_Price", ""),
        "active": truthy(product.get("Active", "TRUE")),
        "category": product.get("Category", ""),
        "keywords": product.get("Keywords", ""),
        "research_keywords": product.get("Research_Keywords", ""),
        "suggested_budget": budget_from_price(product.get("Selling_Price", "")),
        "suggested_bid": bid_from_price(product.get("Selling_Price", "")),
        "raw": product,
    }


def find_product(key: str) -> Dict[str, Any]:
    key = key.lower().strip()
    for row in load_products():
        p = normalized_product(row)
        if p["product_id"].lower() == key or p["sku"].lower() == key:
            return p
    raise HTTPException(status_code=404, detail="Product not found")


# ========================= AMAZON ADS CLIENT =========================
class AmazonAdsClient:
    def __init__(self):
        self.client_id = load_env_or_secret("AMAZON_ADS_CLIENT_ID")
        self.client_secret = load_env_or_secret("AMAZON_ADS_CLIENT_SECRET")
        self.refresh_token = load_env_or_secret("AMAZON_ADS_REFRESH_TOKEN")
        self.profile_id = load_env_or_secret("AMAZON_ADS_PROFILE_ID")
        self.region = load_env_or_secret("AMAZON_ADS_REGION", "na").lower()

        if self.region not in BASE_URLS:
            raise RuntimeError("AMAZON_ADS_REGION must be 'na', 'eu', or 'fe'")

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
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError("Failed to obtain access token")
        return token

    def headers(self, content_type: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Content-Type": content_type,
            "Accept": content_type,
        }

    def _content_type_for(self, endpoint: str) -> str:
        for path, ct in ENDPOINT_CONTENT_TYPES.items():
            if endpoint.startswith(path):
                return ct
        return "application/json"

    def _batch_key_for(self, endpoint: str) -> Optional[str]:
        return ENDPOINT_BATCH_KEYS.get(endpoint)

    def _wrap_batch(self, endpoint: str, body: Any) -> Any:
        batch_key = self._batch_key_for(endpoint)
        if batch_key and isinstance(body, list):
            return {batch_key: body}
        return body

    def post(self, endpoint: str, body: Any):
        url = f"{self.base_url}{endpoint}"
        wrapped = self._wrap_batch(endpoint, body)
        content_type = self._content_type_for(endpoint)

        resp = self.session.post(url, headers=self.headers(content_type), json=wrapped, timeout=60)
        if not resp.ok:
            logger.error(f"API Error {resp.status_code}: {resp.text[:400]}")
            raise RuntimeError(f"Amazon Ads API error {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.text.strip() else None

    def get(self, endpoint: str):
        url = f"{self.base_url}{endpoint}"
        content_type = self._content_type_for(endpoint)
        resp = self.session.get(url, headers=self.headers(content_type), timeout=60)
        if not resp.ok:
            raise RuntimeError(f"GET failed {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.text.strip() else None

    def download_binary(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=120)
        resp.raise_for_status()
        return resp.content


# ========================= UTILITY FUNCTIONS =========================
def extract_first_id(payload: Any) -> int:
    if isinstance(payload, dict):
        for batch_key, item_key in BATCH_ITEM_KEYS.items():
            if batch_key in payload:
                inner = payload[batch_key]
                if isinstance(inner, dict) and "success" in inner and inner["success"]:
                    item = inner["success"][0].get(item_key, inner["success"][0])
                    for k in ("campaignId", "adGroupId", "keywordId", "adId", "id"):
                        if k in item:
                            return int(item[k])
        for k in ("campaignId", "adGroupId", "keywordId", "adId", "id"):
            if k in payload:
                return int(payload[k])
    raise RuntimeError("Could not extract ID from API response")


def keyword_rows(keywords: List[str], campaign_id: int, ad_group_id: int, bid: float) -> List[Dict]:
    rows = []
    for kw in keywords:
        rows.append({"campaignId": str(campaign_id), "adGroupId": str(ad_group_id), "keywordText": kw, "matchType": "EXACT",  "state": "ENABLED", "bid": round(bid * 1.15, 2)})
        rows.append({"campaignId": str(campaign_id), "adGroupId": str(ad_group_id), "keywordText": kw, "matchType": "PHRASE", "state": "ENABLED", "bid": round(bid, 2)})
        rows.append({"campaignId": str(campaign_id), "adGroupId": str(ad_group_id), "keywordText": kw, "matchType": "BROAD",  "state": "ENABLED", "bid": round(bid * 0.85, 2)})
    return rows


def negative_keyword_rows(negatives: List[str], campaign_id: int) -> List[Dict]:
    return [{
        "campaignId": str(campaign_id),
        "keywordText": term,
        "matchType": "negativeExact",
        "state": "ENABLED",
    } for term in negatives]


def parse_report_json_bytes(content: bytes) -> List[Dict]:
    try:
        decompressed = gzip.decompress(content)
    except OSError:
        decompressed = content
    data = json.loads(decompressed.decode("utf-8"))
    if isinstance(data, list):
        return data
    return data.get("rows", []) if isinstance(data, dict) else []


def num(row: Dict, keys: List[str], default: float = 0.0) -> float:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            try:
                return float(str(row[k]).replace("$", "").replace(",", "").strip())
            except Exception:
                continue
    return default


def text(row: Dict, keys: List[str], default: str = "") -> str:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return str(row[k]).strip()
    return default


def classify_terms(rows: List[Dict]):
    winners, negatives, hold = [], [], []
    for row in rows:
        term = text(row, ["Customer Search Term", "searchTerm", "Search Term"])
        if not term:
            continue
        clicks = int(num(row, ["Clicks", "clicks"], 0))
        cost = num(row, ["Spend", "Cost", "cost", "spend"], 0.0)
        sales = num(row, ["7 Day Total Sales", "14 Day Total Sales", "sales7d", "sales"], 0.0)
        orders = int(num(row, ["7 Day Total Orders (#)", "14 Day Total Orders (#)", "orders", "purchases7d"], 0))
        acos = (cost / sales) if sales > 0 else None

        result = {
            "term": term,
            "campaign_id": int(num(row, ["Campaign Id", "campaignId"], 0)),
            "ad_group_id": int(num(row, ["Ad Group Id", "adGroupId"], 0)),
            "clicks": clicks,
            "orders": orders,
            "cost": round(cost, 2),
            "sales": round(sales, 2),
            "acos": round(acos, 4) if acos is not None else None,
        }

        if orders >= 2 and clicks >= 8 and sales > 0 and (acos is None or acos <= 0.35):
            winners.append({**result, "reason": "winner"})
        elif clicks >= 20 and orders == 0:
            negatives.append({**result, "reason": "negative"})
        else:
            hold.append({**result, "reason": "hold"})

    return {"winners": winners, "negatives": negatives, "hold": hold}


def verify_internal_token(authorization: Optional[str]):
    token = optional_env_or_secret("DAILY_OPTIMIZER_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="DAILY_OPTIMIZER_TOKEN not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    supplied = authorization.replace("Bearer ", "").strip()
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail="Invalid token")


# ========================= CAMPAIGN CREATION =========================
def create_live_campaign_for_product(product: Dict[str, Any]) -> Dict[str, Any]:
    client = AmazonAdsClient()
    start_date = today_iso_date()
    keywords = generate_keywords(product)

    campaign_payload = [{
        "name": f"{sanitize_campaign_name(product.get('title'))[:100]} | MANUAL | {start_date}",
        "targetingType": "MANUAL",
        "state": "ENABLED",
        "budget": {"budget": round(product["suggested_budget"], 2), "budgetType": "DAILY"},
        "startDate": start_date,
    }]
    campaign_resp = client.post(ENDPOINTS["campaigns"], campaign_payload)
    campaign_id = extract_first_id(campaign_resp)

    ad_group_payload = [{
        "name": "Main Ad Group",
        "campaignId": str(campaign_id),
        "state": "ENABLED",
        "defaultBid": round(product["suggested_bid"], 2),
    }]
    ad_group_resp = client.post(ENDPOINTS["ad_groups"], ad_group_payload)
    ad_group_id = extract_first_id(ad_group_resp)

    product_ad_payload = [{
        "campaignId": str(campaign_id),
        "adGroupId": str(ad_group_id),
        "asin": product["asin"],
        "state": "ENABLED",
    }]
    product_ad_resp = client.post(ENDPOINTS["product_ads"], product_ad_payload)

    keywords_resp = None
    if keywords:
        kw_rows = keyword_rows(keywords, campaign_id, ad_group_id, product["suggested_bid"])
        keywords_resp = client.post(ENDPOINTS["keywords"], kw_rows)

    return {
        "message": "Campaign created successfully",
        "product_id": product["product_id"],
        "sku": product["sku"],
        "asin": product["asin"],
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "keywords_count": len(keywords),
    }


# ========================= MAIN ROUTES =========================
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    try:
        return templates.TemplateResponse("dashboard.html", {"request": request})
    except Exception as e:
        logger.error(f"Template loading failed: {e}")
        return HTMLResponse(f"""
            <h2>Amazon PPC Optimizer Dashboard</h2>
            <p>Service is running.</p>
            <p style="color: red; margin-top: 20px;">
                <strong>Template Error:</strong> {str(e)}<br><br>
                Make sure <code>templates/dashboard.html</code> exists in the project root.
            </p>
        """)


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.datetime.utcnow().isoformat()}


@app.get("/api/products")
def api_products():
    products = [normalized_product(r) for r in load_products()]
    return {"count": len(products), "products": products}


@app.get("/api/product/{key}")
def api_product(key: str):
    return find_product(key)


@app.post("/api/create-campaign-from-product")
def api_create_campaign(payload: Dict[str, Any]):
    key = payload.get("product_id") or payload.get("sku")
    if not key:
        raise HTTPException(400, "product_id or sku is required")
    product = find_product(key)
    return create_live_campaign_for_product(product)


@app.post("/api/bulk-create-campaigns")
def api_bulk_create(payload: Dict[str, Any]):
    dry_run = payload.get("dry_run", True)
    limit = payload.get("limit", 10)
    only_active = payload.get("only_active", True)

    products = [normalized_product(r) for r in load_products()]
    if only_active:
        products = [p for p in products if p["active"]]
    products = products[:limit]

    results = []
    for p in products:
        if dry_run:
            results.append({
                "sku": p["sku"],
                "title": p["title"],
                "suggested_budget": p["suggested_budget"],
                "suggested_bid": p["suggested_bid"],
                "keywords_count": len(generate_keywords(p)),
                "status": "dry_run"
            })
        else:
            try:
                results.append(create_live_campaign_for_product(p))
            except Exception as e:
                results.append({"sku": p["sku"], "error": str(e)})

    return {"processed": len(products), "dry_run": dry_run, "results": results}


@app.post("/api/run-daily-optimization")
def api_run_optimizer(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None)
):
    verify_internal_token(authorization)

    apply_negatives = payload.get("apply_negatives_live", False)
    apply_winners = payload.get("apply_winners_live", False)
    winner_bid = float(payload.get("winner_bid", 0.90))
    lookback_days = int(payload.get("lookback_days", 14))

    client = AmazonAdsClient()
    start_date = iso_date_days_ago(lookback_days)
    end_date = today_iso_date()

    report_resp = client.post(ENDPOINTS["reports"], {
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

    report_id = (report_resp or {}).get("reportId")
    if not report_id:
        raise HTTPException(status_code=500, detail="Failed to request report ID")

    for _ in range(30):
        status_resp = client.get(f"{ENDPOINTS['reports']}/{report_id}")
        if status_resp.get("status") == "SUCCESS":
            break
        if status_resp.get("status") in ("FAILURE", "CANCELLED"):
            raise HTTPException(status_code=500, detail="Report generation failed")
        time.sleep(10)
    else:
        raise HTTPException(status_code=504, detail="Report generation timed out")

    content = client.download_binary(status_resp.get("location") or status_resp.get("url"))
    rows = parse_report_json_bytes(content)
    classified = classify_terms(rows)

    winners_applied = []
    negatives_applied = []

    if apply_negatives and classified["negatives"]:
        by_campaign: Dict[int, List[str]] = {}
        for item in classified["negatives"]:
            cid = item.get("campaign_id")
            if cid:
                by_campaign.setdefault(cid, []).append(item["term"])
        for cid, terms in by_campaign.items():
            neg_rows = negative_keyword_rows(unique_in_order(terms), cid)
            client.post(ENDPOINTS["negative_keywords"], neg_rows)
            negatives_applied.append({"campaign_id": cid, "count": len(terms), "terms_sample": terms[:8]})

    if apply_winners and classified["winners"]:
        by_adgroup: Dict[int, Dict] = {}
        for item in classified["winners"]:
            agid = item.get("ad_group_id")
            cid = item.get("campaign_id")
            if agid and cid:
                if agid not in by_adgroup:
                    by_adgroup[agid] = {"campaign_id": cid, "terms": []}
                by_adgroup[agid]["terms"].append(item["term"])
        for agid, data in by_adgroup.items():
            kw_rows = keyword_rows(unique_in_order(data["terms"]), data["campaign_id"], agid, winner_bid)
            client.post(ENDPOINTS["keywords"], kw_rows)
            winners_applied.append({"ad_group_id": agid, "campaign_id": data["campaign_id"], "count": len(data["terms"]), "terms_sample": data["terms"][:8]})

    run_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "lookback_days": lookback_days,
        "rows_analyzed": len(rows),
        "winners_found": len(classified["winners"]),
        "negatives_found": len(classified["negatives"]),
        "winners_applied": len(winners_applied),
        "negatives_applied": len(negatives_applied),
        "winners_details": winners_applied,
        "negatives_details": negatives_applied,
        "apply_winners_live": apply_winners,
        "apply_negatives_live": apply_negatives
    }
    save_optimizer_history(run_entry)

    return {
        "success": True,
        "report_id": report_id,
        "date_range": {"start": start_date, "end": end_date},
        "rows_analyzed": len(rows),
        "winners": len(classified["winners"]),
        "negatives": len(classified["negatives"]),
        "winners_applied_count": len(winners_applied),
        "negatives_applied_count": len(negatives_applied),
        "winners_applied": winners_applied,
        "negatives_applied": negatives_applied
    }


@app.get("/api/optimizer-history")
def api_optimizer_history():
    history = load_optimizer_history()
    return {"history": history, "count": len(history)}


@app.get("/api/campaigns")
def api_list_campaigns():
    client = AmazonAdsClient()
    try:
        response = client.session.post(
            f"{client.base_url}/sp/campaigns/list",
            headers={
                "Authorization": f"Bearer {client.access_token}",
                "Amazon-Advertising-API-ClientId": client.client_id,
                "Amazon-Advertising-API-Scope": client.profile_id,
                "Content-Type": "application/vnd.spcampaign.v3+json",
                "Accept": "application/vnd.spcampaign.v3+json",
            },
            json={"maxResults": 100, "filters": {"stateFilter": {"include": ["ENABLED"]}}},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        campaigns = data.get("campaigns", []) if isinstance(data, dict) else data
        return {"count": len(campaigns), "campaigns": campaigns}
    except Exception as e:
        logger.error("Failed to fetch campaigns", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ========================= REAL DAILY PERFORMANCE =========================
@app.get("/api/campaign-performance")
def api_campaign_performance():
    client = AmazonAdsClient()
    start_date = iso_date_days_ago(14)
    end_date = today_iso_date()

    # Get active campaigns
    list_resp = client.session.post(
        f"{client.base_url}/sp/campaigns/list",
        headers={
            "Authorization": f"Bearer {client.access_token}",
            "Amazon-Advertising-API-ClientId": client.client_id,
            "Amazon-Advertising-API-Scope": client.profile_id,
            "Content-Type": "application/vnd.spcampaign.v3+json",
            "Accept": "application/vnd.spcampaign.v3+json",
        },
        json={"maxResults": 100, "filters": {"stateFilter": {"include": ["ENABLED"]}}},
        timeout=30,
    )
    list_resp.raise_for_status()
    data = list_resp.json()
    campaigns = data.get("campaigns", []) if isinstance(data, dict) else data

    if not campaigns:
        return {"count": 0, "campaigns": []}

    try:
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS",
                "groupBy": ["campaign"],
                "columns": ["campaignId", "campaignName", "date", "impressions", "clicks", "cost", "sales14d", "purchases14d"],
                "reportTypeId": "spCampaigns",
                "timeUnit": "DAILY",
                "format": "GZIP_JSON",
            }
        }

        report_resp = client.post(ENDPOINTS["reports"], body)
        report_id = report_resp.get("reportId")
        if not report_id:
            raise RuntimeError("No reportId returned")

        for _ in range(30):
            status = client.get(f"{ENDPOINTS['reports']}/{report_id}")
            if status.get("status") == "SUCCESS":
                break
            time.sleep(10)
        else:
            raise RuntimeError("Report timeout")

        download_url = status.get("location") or status.get("url")
        content = client.download_binary(download_url)
        rows = parse_report_json_bytes(content)

        daily_by_campaign = defaultdict(list)
        summary_by_campaign = defaultdict(lambda: {"impressions": 0, "clicks": 0, "spend": 0.0, "sales": 0.0, "orders": 0})

        for row in rows:
            cid = str(row.get("campaignId") or "")
            if not cid:
                continue

            daily = {
                "date": row.get("date"),
                "spend": round(num(row, ["cost", "spend"], 0), 2),
                "sales": round(num(row, ["sales14d"], 0), 2),
                "acos": round(num(row, ["cost"], 0) / num(row, ["sales14d"], 1), 4) if num(row, ["sales14d"], 0) > 0 else None,
            }
            daily_by_campaign[cid].append(daily)

            s = summary_by_campaign[cid]
            s["impressions"] += int(num(row, ["impressions"], 0))
            s["clicks"] += int(num(row, ["clicks"], 0))
            s["spend"] += num(row, ["cost", "spend"], 0)
            s["sales"] += num(row, ["sales14d"], 0)
            s["orders"] += int(num(row, ["purchases14d"], 0))

        enriched = []
        for c in campaigns:
            cid = str(c.get("campaignId") or "")
            summary = summary_by_campaign.get(cid, {})
            daily_data = sorted(daily_by_campaign.get(cid, []), key=lambda x: x.get("date") or "")

            acos = (summary.get("spend", 0) / summary.get("sales", 1)) if summary.get("sales", 0) > 0 else None

            enriched.append({
                **c,
                "impressions": summary.get("impressions", 0),
                "clicks": summary.get("clicks", 0),
                "spend": round(summary.get("spend", 0), 2),
                "sales": round(summary.get("sales", 0), 2),
                "orders": summary.get("orders", 0),
                "acos": round(acos, 4) if acos is not None else None,
                "daily_trend": daily_data
            })

        return {"count": len(enriched), "campaigns": enriched}

    except Exception as e:
        logger.error(f"Daily performance report failed: {e}", exc_info=True)
        return {"count": len(campaigns), "campaigns": campaigns, "note": "Trend data unavailable"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
