"""Amazon Ads PPC optimizer core for Nature's Way Soil.

Safe to import from Cloud Run: exposes the FastAPI app, shared helpers, and
AmazonAdsClient without running an optimizer job during module import.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hmac
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

try:
    from campaign_engine import build_all_campaign_plans
except Exception:
    build_all_campaign_plans = None  # type: ignore

app = FastAPI(title="Nature's Way Soil Amazon PPC Optimizer")

PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv",
)
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
BASE_URLS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}
DEFAULT_FALLBACK_BID = 0.75
SP_CONTENT_TYPES = {
    "/sp/campaigns": "application/vnd.spcampaign.v3+json",
    "/sp/campaigns/list": "application/vnd.spcampaign.v3+json",
    "/sp/adGroups": "application/vnd.spadgroup.v3+json",
    "/sp/adGroups/list": "application/vnd.spadgroup.v3+json",
    "/sp/productAds": "application/vnd.spproductad.v3+json",
    "/sp/keywords": "application/vnd.spkeyword.v3+json",
    "/sp/keywords/list": "application/vnd.spkeyword.v3+json",
    "/sp/campaignNegativeKeywords": "application/vnd.spcampaignnegativekeyword.v3+json",
}
BATCH_KEYS = {
    "/sp/campaigns": "campaigns",
    "/sp/adGroups": "adGroups",
    "/sp/productAds": "productAds",
    "/sp/keywords": "keywords",
    "/sp/campaignNegativeKeywords": "campaignNegativeKeywords",
}
STOPWORDS = {"the", "and", "for", "with", "from", "your", "you", "our", "this", "that", "also", "very", "just", "any", "all", "each", "both", "into", "more"}
PENDING_REPORT_FILE = Path(os.getenv("PENDING_REPORT_FILE", "/tmp/pending_report.json"))
OPTIMIZER_HISTORY_FILE = Path(os.getenv("OPTIMIZER_HISTORY_FILE", "/tmp/optimizer_history.json"))


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def verify_internal_token(authorization: Optional[str] = None, x_daily_optimizer_token: Optional[str] = None) -> None:
    expected = _env("DAILY_OPTIMIZER_TOKEN")
    if not expected:
        return
    supplied = (x_daily_optimizer_token or "").strip()
    if not supplied and authorization and authorization.startswith("Bearer "):
        supplied = authorization.replace("Bearer ", "", 1).strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing optimizer token")

def normalize_text(text: Any) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9'&\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first(row: Dict[str, Any], *names: str, default: str = "") -> str:
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = row.get(name)
        if value in (None, ""):
            value = lower_map.get(name.lower())
        if value not in (None, ""):
            return str(value).strip()
    return default


def _money(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        return default


def budget_from_price(price_value: Any) -> float:
    price = _money(price_value, 0.0)
    if price < 15:
        return 12.0
    if price < 25:
        return 18.0
    if price < 40:
        return 25.0
    return 35.0


def bid_from_price(price_value: Any) -> float:
    price = _money(price_value, 0.0)
    if price < 15:
        return 0.55
    if price < 25:
        return 0.75
    if price < 40:
        return 0.95
    return 1.10


def load_products() -> List[Dict[str, str]]:
    response = requests.get(PRODUCTS_CSV_URL, timeout=30)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    rows = [{str(k).strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    return [row for row in rows if _first(row, "SKU", "ASIN", "Title", "Product_Name", "Product Name")]


def normalized_product(row: Dict[str, Any]) -> Dict[str, Any]:
    title = _first(row, "Title", "Product_Name", "Product Name", "Name", default="Product")
    sku = _first(row, "SKU", "Seller_SKU", "Seller SKU")
    asin = _first(row, "ASIN")
    product_id = _first(row, "Product_ID", "Product ID", "SKU", "ASIN", default=normalize_text(title).replace(" ", "_"))
    price = _money(_first(row, "Price", "Amazon_Price", "Sale Price"), 0.0)
    suggested_budget = _money(_first(row, "Daily_Budget", "Daily Budget", "Suggested_Budget"), budget_from_price(price))
    suggested_bid = _money(_first(row, "Default_Bid", "Default Bid", "Suggested_Bid"), bid_from_price(price))
    return {"product_id": product_id, "sku": sku, "asin": asin, "title": title, "price": price, "suggested_budget": suggested_budget, "suggested_bid": suggested_bid, "raw": row}


def _split_keywords(value: Any) -> List[str]:
    parts = re.split(r"[\n,;|]+", str(value or ""))
    out: List[str] = []
    seen = set()
    for part in parts:
        keyword = normalize_text(part)
        if keyword and keyword not in seen:
            seen.add(keyword)
            out.append(keyword)
    return out


def generate_keywords_for_product(row: Dict[str, Any], limit: int = 80) -> List[str]:
    title = _first(row, "Title", "Product_Name", "Product Name", default="")
    category = _first(row, "Category", "Product_Category")
    columns = ["Keywords", "Core_Keywords", "Core Keywords", "Research_Keywords", "Research Keywords", "Long_Tail_Keywords", "Long Tail Keywords", "Problem_Keywords", "Problem Keywords", "Ingredient_Keywords", "Ingredient Keywords"]
    terms: List[str] = []
    for col in columns:
        terms.extend(_split_keywords(_first(row, col)))
    base = normalize_text(title)
    if base:
        terms.append(base)
        words = [w for w in base.split() if w not in STOPWORDS]
        if words:
            terms.extend([" ".join(words[:4]), f"{words[0]} fertilizer" if "fertilizer" not in words else " ".join(words[:3]), f"organic {words[0]}"])
    if category:
        terms.append(normalize_text(category))
    seen = set()
    cleaned = []
    for term in terms:
        term = normalize_text(term)
        if term and term not in seen:
            seen.add(term)
            cleaned.append(term[:80])
        if len(cleaned) >= limit:
            break
    return cleaned


def parse_report_json_bytes(data: bytes) -> List[Dict[str, Any]]:
    if not data:
        return []
    try:
        data = gzip.decompress(data)
    except Exception:
        pass
    parsed = json.loads(data.decode("utf-8", errors="replace"))
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("rows", "data", "report", "records"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return [parsed]
    return []


def keyword_rows(keywords: Iterable[str], campaign_id: Any, ad_group_id: Any, bid: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for keyword in keywords:
        keyword = normalize_text(keyword)
        if not keyword:
            continue
        for match_type in ("EXACT", "PHRASE", "BROAD"):
            rows.append({"campaignId": str(campaign_id), "adGroupId": str(ad_group_id), "keywordText": keyword, "matchType": match_type, "state": "ENABLED", "bid": round(float(bid), 2)})
    return rows


def negative_keyword_rows(keywords: Iterable[str], campaign_id: Any) -> List[Dict[str, Any]]:
    return [{"campaignId": str(campaign_id), "keywordText": normalize_text(keyword), "matchType": "NEGATIVE_EXACT", "state": "ENABLED"} for keyword in keywords if normalize_text(keyword)]


def _num(row: Dict[str, Any], keys: Iterable[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            try:
                return float(str(row[key]).replace("$", "").replace(",", "").strip())
            except Exception:
                continue
    return default


def _text(row: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return default


def classify_terms(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Default search-term classifier. server.py patches this with stronger rules."""
    winners: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []
    hold: List[Dict[str, Any]] = []
    for row in rows:
        term = normalize_text(_text(row, ["Customer Search Term", "searchTerm", "Search Term", "search term"]))
        if not term:
            continue
        clicks = int(_num(row, ["Clicks", "clicks"], 0))
        cost = _num(row, ["Spend", "Cost", "cost", "spend"], 0.0)
        sales = _num(row, ["7 Day Total Sales", "14 Day Total Sales", "sales7d", "sales14d", "sales"], 0.0)
        orders = int(_num(row, ["7 Day Total Orders (#)", "14 Day Total Orders (#)", "orders", "purchases7d", "purchases14d"], 0))
        acos = cost / sales if sales > 0 else None
        result = {
            "term": term,
            "campaign_id": int(_num(row, ["Campaign Id", "campaignId", "campaign_id"], 0)),
            "ad_group_id": int(_num(row, ["Ad Group Id", "adGroupId", "ad_group_id"], 0)),
            "clicks": clicks,
            "orders": orders,
            "cost": round(cost, 2),
            "sales": round(sales, 2),
            "acos": round(acos, 4) if acos is not None else None,
        }
        if orders >= 2 and clicks >= 8 and sales > 0 and (acos is None or acos <= 0.35):
            winners.append({**result, "reason": "winner"})
        elif clicks >= 20 and orders == 0:
            negatives.append({**result, "reason": "negative", "negative_match_type": "NEGATIVE_EXACT"})
        else:
            hold.append({**result, "reason": "hold"})
    return {"winners": winners, "negatives": negatives, "bid_down": [], "hold": hold}


def apply_negatives_step(client: Any, classified: Dict[str, Any]) -> List[Dict[str, Any]]:
    negatives_applied: List[Dict[str, Any]] = []
    campaigns = sorted({int(item.get("campaign_id") or 0) for item in classified.get("negatives", []) if item.get("campaign_id")})
    for campaign_id in campaigns:
        terms = [item.get("matched_phrase") or item.get("term") for item in classified.get("negatives", []) if int(item.get("campaign_id") or 0) == campaign_id]
        terms = [normalize_text(term) for term in terms if normalize_text(term)]
        if not terms:
            continue
        rows = negative_keyword_rows(dict.fromkeys(terms).keys(), campaign_id)
        client.create_negative_keywords(rows)
        negatives_applied.append({"campaign_id": campaign_id, "count": len(rows), "terms_sample": terms[:10]})
    return negatives_applied


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _days_ago_iso(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _load_pending_report() -> Dict[str, Any]:
    try:
        if PENDING_REPORT_FILE.exists():
            return json.loads(PENDING_REPORT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"report_id": None, "settings": {}, "ts": 0.0}


def _save_pending_report(entry: Dict[str, Any]) -> None:
    PENDING_REPORT_FILE.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")


def _load_optimizer_history() -> List[Dict[str, Any]]:
    try:
        if OPTIMIZER_HISTORY_FILE.exists():
            data = json.loads(OPTIMIZER_HISTORY_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save_optimizer_history(entry: Dict[str, Any]) -> None:
    history = _load_optimizer_history()
    history.append(entry)
    OPTIMIZER_HISTORY_FILE.write_text(json.dumps(history[-50:], indent=2, default=str), encoding="utf-8")


class AmazonAdsClient:
    def __init__(self):
        self.client_id = _env("AMAZON_ADS_CLIENT_ID", "AMAZON_CLIENT_ID")
        self.client_secret = _env("AMAZON_ADS_CLIENT_SECRET", "AMAZON_CLIENT_SECRET")
        self.refresh_token = _env("AMAZON_ADS_REFRESH_TOKEN", "AMAZON_REFRESH_TOKEN")
        self.profile_id = _env("AMAZON_ADS_PROFILE_ID", "AMAZON_PROFILE_ID")
        self.region = _env("AMAZON_ADS_REGION", "AMAZON_REGION", default="na").lower()
        self.base_url = BASE_URLS.get(self.region, BASE_URLS["na"])
        self.access_token = self._get_token()
        self.session = requests.Session()

    def _get_token(self) -> str:
        missing = [name for name, value in {"AMAZON_ADS_CLIENT_ID": self.client_id, "AMAZON_ADS_CLIENT_SECRET": self.client_secret, "AMAZON_ADS_REFRESH_TOKEN": self.refresh_token, "AMAZON_ADS_PROFILE_ID": self.profile_id}.items() if not value]
        if missing:
            raise RuntimeError(f"Missing Amazon Ads config: {', '.join(missing)}")
        response = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
        response.raise_for_status()
        return response.json()["access_token"]

    def _headers(self, content_type: Optional[str] = None, accept: Optional[str] = None) -> Dict[str, str]:
        ct = content_type or "application/json"
        return {"Authorization": f"Bearer {self.access_token}", "Amazon-Advertising-API-ClientId": self.client_id, "Amazon-Advertising-API-Scope": self.profile_id, "Content-Type": ct, "Accept": accept or ct}

    def _wrap_payload(self, endpoint: str, payload: Any) -> Any:
        key = BATCH_KEYS.get(endpoint)
        if key and isinstance(payload, list):
            return {key: payload}
        return payload

    def request(self, method: str, endpoint: str, payload: Any = None, content_type: Optional[str] = None, accept: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
        body = self._wrap_payload(endpoint, payload)
        ct = content_type or SP_CONTENT_TYPES.get(endpoint, "application/json")
        kwargs: Dict[str, Any] = {"headers": self._headers(ct, accept or ct), "timeout": timeout}
        if body is not None:
            kwargs["json"] = body
        response = self.session.request(method, f"{self.base_url}{endpoint}", **kwargs)
        for delay in (2, 5, 10, 20, 40):
            if response.status_code not in {425, 429}:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = max(delay, min(float(retry_after), 60.0)) if retry_after else delay
            except (TypeError, ValueError):
                wait_seconds = delay
            time.sleep(wait_seconds)
            response = self.session.request(method, f"{self.base_url}{endpoint}", **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def post(self, endpoint: str, payload: Any, content_type: Optional[str] = None, accept: Optional[str] = None) -> Dict[str, Any]:
        return self.request("POST", endpoint, payload, content_type, accept)

    def put(self, endpoint: str, payload: Any, content_type: Optional[str] = None, accept: Optional[str] = None) -> Dict[str, Any]:
        return self.request("PUT", endpoint, payload, content_type, accept)

    def get(self, endpoint: str, content_type: Optional[str] = None, accept: Optional[str] = None) -> Dict[str, Any]:
        return self.request("GET", endpoint, None, content_type, accept)

    def list_campaigns(self) -> List[Dict[str, Any]]:
        data = self.post("/sp/campaigns/list", {"maxResults": 100}, content_type=SP_CONTENT_TYPES["/sp/campaigns/list"], accept=SP_CONTENT_TYPES["/sp/campaigns/list"])
        return data.get("campaigns", []) if isinstance(data, dict) else []

    def list_keywords(self, campaign_id: str) -> List[Dict[str, Any]]:
        data = self.post("/sp/keywords/list", {"maxResults": 100, "filters": {"campaignIdFilter": {"include": [str(campaign_id)]}}}, content_type=SP_CONTENT_TYPES["/sp/keywords/list"], accept=SP_CONTENT_TYPES["/sp/keywords/list"])
        return data.get("keywords", []) if isinstance(data, dict) else []

    def create_keywords(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.post("/sp/keywords", {"keywords": rows}, content_type=SP_CONTENT_TYPES["/sp/keywords"], accept=SP_CONTENT_TYPES["/sp/keywords"])

    def create_negative_keywords(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.post("/sp/campaignNegativeKeywords", {"campaignNegativeKeywords": rows}, content_type=SP_CONTENT_TYPES["/sp/campaignNegativeKeywords"], accept=SP_CONTENT_TYPES["/sp/campaignNegativeKeywords"])

    def request_report(self, body: Dict[str, Any]) -> str:
        data = self.post("/reporting/reports", body, content_type="application/vnd.createasyncreportrequest.v3+json", accept="application/json")
        report_id = data.get("reportId") or data.get("report_id")
        if not report_id:
            raise RuntimeError(f"Amazon report request did not return reportId: {data}")
        return str(report_id)

    def wait_for_report(self, report_id: str, timeout_seconds: int = 300) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            data = self.get(f"/reporting/reports/{report_id}", accept="application/json")
            status = str(data.get("status") or "").upper()
            if status in {"COMPLETED", "SUCCESS"}:
                url = data.get("url") or data.get("location")
                if not url:
                    raise RuntimeError(f"Report completed but no download URL returned: {data}")
                return str(url)
            if status in {"FAILURE", "FAILED", "CANCELLED"}:
                raise RuntimeError(f"Amazon report failed: {data}")
            time.sleep(10)
        raise TimeoutError(f"Timed out waiting for report {report_id}")

    def download_binary(self, url: str) -> bytes:
        response = self.session.get(url, timeout=120)
        response.raise_for_status()
        return response.content

    def get_bid_recommendation(self, campaign_id: str, ad_group_id: str, keyword: str, match_type: str = "PHRASE") -> Dict[str, float]:
        return self.get_ad_group_bid_recommendation(ad_group_id, campaign_id)

    @staticmethod
    def _normalize_bid_recommendation(data: Any) -> Dict[str, float]:
        candidates: List[Dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                candidates.append(value)
                for key in ("suggestedBid", "bidRecommendation", "recommendation", "bidRecommendations"):
                    nested = value.get(key)
                    if isinstance(nested, (dict, list)):
                        collect(nested)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(data)
        for item in candidates:
            low = _money(item.get("rangeStart", item.get("low", item.get("suggestedBidLow"))), 0.0)
            high = _money(item.get("rangeEnd", item.get("high", item.get("suggestedBidHigh"))), 0.0)
            suggested = _money(item.get("suggested", item.get("suggestedBid")), 0.0)
            if low > 0 and high > 0:
                return {"low": low, "high": high, "suggested": suggested or ((low + high) / 2)}
        return {}

    def get_ad_group_bid_recommendation(self, ad_group_id: str, campaign_id: str) -> Dict[str, float]:
        content_type = "application/vnd.spthemebasedbidrecommendation.v5+json"
        payload = {
            "recommendationType": "BIDS_FOR_EXISTING_AD_GROUP",
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "targetingExpressions": [
                {"type": expression_type}
                for expression_type in ("CLOSE_MATCH", "LOOSE_MATCH", "SUBSTITUTES", "COMPLEMENTS")
            ],
        }
        data = self.post(
            "/sp/targets/bid/recommendations",
            payload,
            content_type=content_type,
            accept=content_type,
        )
        lows: List[float] = []
        suggested: List[float] = []
        highs: List[float] = []
        for theme in data.get("bidRecommendations", []):
            for item in theme.get("bidRecommendationsForTargetingExpressions", []):
                values = [_money(row.get("suggestedBid"), 0.0) for row in item.get("bidValues", [])]
                values = [value for value in values if value > 0]
                if values:
                    lows.append(values[0])
                    suggested.append(values[len(values) // 2])
                    highs.append(values[-1])
        if not suggested:
            return self._normalize_bid_recommendation(data)
        return {
            "low": sum(lows) / len(lows),
            "suggested": sum(suggested) / len(suggested),
            "high": sum(highs) / len(highs),
        }

def _extract_id(resp: Dict[str, Any], batch_key: str, item_key: str, id_key: str) -> str:
    inner = resp.get(batch_key, {}) if isinstance(resp, dict) else {}
    if isinstance(inner, dict):
        success = inner.get("success", [])
        if success:
            item = success[0].get(item_key) or success[0]
            return str(item.get(id_key) or "")
    return str(resp.get(id_key) or "") if isinstance(resp, dict) else ""


def create_live_campaign_for_product(product: Dict[str, Any]) -> Dict[str, Any]:
    client = AmazonAdsClient()
    today = dt.date.today().isoformat()
    title = str(product.get("title") or product.get("Product_Name") or "Product")[:70]
    budget = float(product.get("suggested_budget") or 25.0)
    bid = float(product.get("suggested_bid") or DEFAULT_FALLBACK_BID)
    sku = str(product.get("sku") or product.get("SKU") or "")
    asin = str(product.get("asin") or product.get("ASIN") or "")
    campaign_resp = client.post("/sp/campaigns", {"campaigns": [{"name": f"{title} | MANUAL | {today}", "targetingType": "MANUAL", "state": "ENABLED", "budget": {"budget": round(budget, 2), "budgetType": "DAILY"}, "startDate": today}]}, content_type=SP_CONTENT_TYPES["/sp/campaigns"], accept=SP_CONTENT_TYPES["/sp/campaigns"])
    campaign_id = _extract_id(campaign_resp, "campaigns", "campaign", "campaignId")
    ad_group_resp = client.post("/sp/adGroups", {"adGroups": [{"name": "Default Ad Group", "campaignId": str(campaign_id), "state": "ENABLED", "defaultBid": round(bid, 2)}]}, content_type=SP_CONTENT_TYPES["/sp/adGroups"], accept=SP_CONTENT_TYPES["/sp/adGroups"])
    ad_group_id = _extract_id(ad_group_resp, "adGroups", "adGroup", "adGroupId")
    ad = {"campaignId": str(campaign_id), "adGroupId": str(ad_group_id), "state": "ENABLED"}
    if sku:
        ad["sku"] = sku
    if asin:
        ad["asin"] = asin
    client.post("/sp/productAds", {"productAds": [ad]}, content_type=SP_CONTENT_TYPES["/sp/productAds"], accept=SP_CONTENT_TYPES["/sp/productAds"])
    kws = generate_keywords_for_product(product, limit=20)
    if kws:
        client.create_keywords(keyword_rows(kws, campaign_id, ad_group_id, bid))
    return {"campaign_id": campaign_id, "ad_group_id": ad_group_id, "keywords_created": len(kws) * 3}


def run_optimizer(search_terms_path: Optional[str] = None, dry_run: bool = True) -> Dict[str, Any]:
    search_terms_df = None
    if search_terms_path:
        import pandas as pd
        path = Path(search_terms_path)
        search_terms_df = pd.read_excel(path) if path.suffix.lower() == ".xlsx" else pd.read_csv(path)
    if build_all_campaign_plans is None:
        raise RuntimeError("campaign_engine could not be imported")
    plans = build_all_campaign_plans(search_terms_df=search_terms_df)
    output_file = Path("campaign_plan.json")
    output_file.write_text(json.dumps(plans, indent=2, default=str), encoding="utf-8")
    return {"success": True, "dry_run": dry_run, "output_file": str(output_file), **plans}


def _build_search_term_report_body(lookback_days: int) -> Dict[str, Any]:
    return {
        "startDate": _days_ago_iso(lookback_days),
        "endDate": _today_iso(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["searchTerm"],
            "columns": ["campaignId", "adGroupId", "searchTerm", "clicks", "cost", "sales7d", "purchases7d"],
            "reportTypeId": "spSearchTerm",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }


def _request_pending_optimization_report(payload: Dict[str, Any], *, apply_negatives: bool, apply_winners: bool) -> Dict[str, Any]:
    lookback_days = int(payload.get("lookback_days", 14))
    winner_bid = float(payload.get("winner_bid", 0.90))
    client = AmazonAdsClient()
    report_body = _build_search_term_report_body(lookback_days)
    report_id = client.request_report(report_body)
    entry = {
        "report_id": report_id,
        "ts": time.time(),
        "settings": {
            "apply_negatives": apply_negatives,
            "apply_winners": apply_winners,
            "winner_bid": winner_bid,
            "lookback_days": lookback_days,
            "start_date": report_body["startDate"],
            "end_date": report_body["endDate"],
        },
    }
    _save_pending_report(entry)
    return {
        "success": True,
        "report_id": report_id,
        "message": "Report requested. Apply it after Amazon finishes generating it, usually 30-60 minutes.",
        "date_range": {"start": report_body["startDate"], "end": report_body["endDate"]},
        "settings": entry["settings"],
    }


def _apply_winner_keywords(client: AmazonAdsClient, classified: Dict[str, Any], winner_bid: float) -> List[Dict[str, Any]]:
    winners_applied: List[Dict[str, Any]] = []
    by_adgroup: Dict[int, Dict[str, Any]] = {}
    for item in classified.get("winners", []):
        ad_group_id = int(item.get("ad_group_id") or 0)
        campaign_id = int(item.get("campaign_id") or 0)
        term = normalize_text(item.get("term"))
        if not ad_group_id or not campaign_id or not term:
            continue
        by_adgroup.setdefault(ad_group_id, {"campaign_id": campaign_id, "terms": []})["terms"].append(term)

    for ad_group_id, data in by_adgroup.items():
        terms = list(dict.fromkeys(data["terms"]))
        rows = keyword_rows(terms, data["campaign_id"], ad_group_id, winner_bid)
        if not rows:
            continue
        client.create_keywords(rows)
        winners_applied.append({
            "ad_group_id": ad_group_id,
            "campaign_id": data["campaign_id"],
            "terms_count": len(terms),
            "keyword_rows_created": len(rows),
            "terms_sample": terms[:10],
        })
    return winners_applied


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "campaign-optimizer"}


@app.get("/api/products")
def api_products() -> JSONResponse:
    try:
        rows = load_products()
        return JSONResponse({"count": len(rows), "products": [normalized_product(row) for row in rows]})
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


# ---- 14-day dashboard performance summary (cached, non-blocking) ----
import threading as _threading
import logging as _logging

_dash_summary_log = _logging.getLogger("dashboard_summary")
_DASH_SUMMARY_TTL = 1800  # 30 minutes
_dash_summary_cache: Dict[str, Any] = {"summary": None, "per_campaign": {}, "ts": 0.0, "refreshing": False}


def _build_dashboard_report_body() -> Dict[str, Any]:
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=13)
    return {
        "name": "dashboard-14d-summary",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": ["campaignId", "impressions", "clicks", "cost", "sales14d", "purchases14d"],
            "reportTypeId": "spCampaigns",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }


def _load_dashboard_rows_from_bigquery() -> Optional[List[Dict[str, Any]]]:
    """Read daily synced metrics; return None so Amazon remains a fallback."""
    try:
        from google.cloud import bigquery
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "amazon-ppc-bid-optimizer")
        table = os.getenv("CAMPAIGN_PERFORMANCE_TABLE", f"{project}.amazon_ppc.sp_campaign_performance")
        query = f"""
            SELECT CAST(campaign_id AS STRING) AS campaign_id,
                   SUM(COALESCE(cost, 0)) AS cost,
                   SUM(COALESCE(sales, 0)) AS sales,
                   SUM(COALESCE(clicks, 0)) AS clicks,
                   SUM(COALESCE(purchases, 0)) AS purchases,
                   SUM(COALESCE(impressions, 0)) AS impressions
            FROM `{table}`
            WHERE date BETWEEN DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 14 DAY)
                           AND DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 1 DAY)
            GROUP BY campaign_id
        """
        rows = [dict(row.items()) for row in bigquery.Client(project=project).query(query).result()]
        if rows:
            return rows
        _dash_summary_log.warning("BigQuery dashboard summary returned no rows")
    except Exception as exc:
        _dash_summary_log.warning("BigQuery dashboard summary failed: %s", exc)
    return None

def _refresh_dashboard_summary() -> None:
    try:
        rows = _load_dashboard_rows_from_bigquery()
        if rows is None:
            client = AmazonAdsClient()
            report_id = client.request_report(_build_dashboard_report_body())
            url = client.wait_for_report(report_id, timeout_seconds=1200)
            rows = parse_report_json_bytes(client.download_binary(url))
        per: Dict[str, Any] = {}
        tot = {"spend": 0.0, "sales": 0.0, "clicks": 0, "orders": 0, "impressions": 0}
        for r in rows:
            cid = str(r.get("campaignId") or r.get("campaign_id") or "")
            cost = float(r.get("cost") or 0.0)
            sales = float(r.get("sales14d") or r.get("sales") or 0.0)
            clicks = int(r.get("clicks") or 0)
            orders = int(r.get("purchases14d") or r.get("purchases") or 0)
            impressions = int(r.get("impressions") or 0)
            per[cid] = {
                "spend": round(cost, 2), "sales": round(sales, 2),
                "clicks": clicks, "orders": orders, "impressions": impressions,
                "acos": (round(cost / sales, 4) if sales else None),
            }
            tot["spend"] += cost; tot["sales"] += sales; tot["clicks"] += clicks
            tot["orders"] += orders; tot["impressions"] += impressions
        summary = {
            "spend": round(tot["spend"], 2), "sales": round(tot["sales"], 2),
            "clicks": tot["clicks"], "orders": tot["orders"],
            "acos": (round(tot["spend"] / tot["sales"], 4) if tot["sales"] else None),
        }
        _dash_summary_cache.update({"summary": summary, "per_campaign": per, "ts": time.time()})
    except Exception as exc:  # noqa: BLE001
        _dash_summary_log.warning("dashboard summary refresh failed: %s", exc)
    finally:
        _dash_summary_cache["refreshing"] = False


def _get_cached_dashboard_summary():
    fresh = (
        _dash_summary_cache["summary"] is not None
        and (time.time() - _dash_summary_cache["ts"]) < _DASH_SUMMARY_TTL
    )
    if not fresh and not _dash_summary_cache["refreshing"]:
        _dash_summary_cache["refreshing"] = True
        _threading.Thread(target=_refresh_dashboard_summary, daemon=True).start()
    return _dash_summary_cache["summary"], _dash_summary_cache["per_campaign"]


@app.get("/api/dashboard-data")
def api_dashboard_data(authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        client = AmazonAdsClient()
        all_campaigns = client.list_campaigns()
        active_campaigns = [
            campaign for campaign in all_campaigns
            if str(campaign.get("state") or "").upper() == "ENABLED"
        ]

        summary, per_campaign = _get_cached_dashboard_summary()
        for c in active_campaigns:
            cid = str(c.get("campaignId") or c.get("campaign_id") or "")
            m = per_campaign.get(cid)
            if m:
                c["spend"] = m["spend"]; c["sales"] = m["sales"]
                c["clicks"] = m["clicks"]; c["orders"] = m["orders"]
                c["impressions"] = m["impressions"]; c["acos"] = m["acos"]

        bid_mode = "UNKNOWN"; budget_protection: Dict[str, Any] = {}; note = "Budget mode loaded."
        try:
            from budget_dayparting import get_budget_protection_mode, budget_protection_status
            bid_mode = get_budget_protection_mode()
            budget_protection = budget_protection_status() or {}
            note = budget_protection.get("note", note)
        except Exception:  # noqa: BLE001
            pass

        return JSONResponse({
            "success": True,
            "active_only": True,
            "campaign_count": len(active_campaigns),
            "active_campaign_count": len(active_campaigns),
            "total_campaign_count": len(all_campaigns),
            "paused_campaign_count": sum(1 for c in all_campaigns if str(c.get("state") or "").upper() == "PAUSED"),
            "archived_campaign_count": sum(1 for c in all_campaigns if str(c.get("state") or "").upper() == "ARCHIVED"),
            "campaigns": active_campaigns,
            "summary": summary or {"spend": 0, "sales": 0, "acos": None, "clicks": 0, "orders": 0},
            "bid_mode": bid_mode,
            "budget_protection": budget_protection,
            "note": note,
            "cache_rebuild_in_progress": summary is None,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/run-optimizer")
def api_run_optimizer(payload: Dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        # Default to the live daily optimization flow so the dashboard button and
        # old scheduler both create the pending Amazon report used by apply-optimization.
        if not payload.get("search_terms_path") and payload.get("mode", "daily") != "offline_plan":
            return JSONResponse(_request_pending_optimization_report(payload, apply_negatives=True, apply_winners=True))
        return JSONResponse(run_optimizer(payload.get("search_terms_path"), dry_run=bool(payload.get("dry_run", True))))
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/run-daily-optimization")
def api_run_daily_optimization(payload: Dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        return JSONResponse(_request_pending_optimization_report(payload, apply_negatives=bool(payload.get("apply_negatives_live", True)), apply_winners=bool(payload.get("apply_winners_live", True))))
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/apply-negatives")
def api_apply_negatives(payload: Dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        return JSONResponse(_request_pending_optimization_report(payload, apply_negatives=True, apply_winners=False))
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/apply-winners")
def api_apply_winners(payload: Dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        return JSONResponse(_request_pending_optimization_report(payload, apply_negatives=False, apply_winners=True))
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/apply-optimization")
def api_apply_optimization(payload: Dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        pending = _load_pending_report()
        report_id = pending.get("report_id") or payload.get("report_id")
        if not report_id:
            return JSONResponse({"error": True, "message": "No pending report found. Run optimization first."}, status_code=404)

        client = AmazonAdsClient()
        status_resp = client.get(f"/reporting/reports/{report_id}", accept="application/json")
        status = str(status_resp.get("status") or "").upper()

        if status in {"PENDING", "PROCESSING", "IN_PROGRESS"}:
            return JSONResponse({"success": False, "status": status, "message": f"Report not ready yet ({status})."}, status_code=202)
        if status in {"FAILURE", "FAILED", "CANCELLED"}:
            _save_pending_report({"report_id": None, "settings": {}, "ts": 0.0})
            return JSONResponse({"error": True, "status": status, "message": f"Amazon report failed: {status}"}, status_code=500)
        if status not in {"SUCCESS", "COMPLETED"}:
            return JSONResponse({"success": False, "status": status, "message": f"Report status: {status or 'UNKNOWN'}"}, status_code=202)

        download_url = status_resp.get("url") or status_resp.get("location")
        if not download_url:
            return JSONResponse({"error": True, "message": "Report completed but no download URL was returned."}, status_code=500)

        rows = parse_report_json_bytes(client.download_binary(str(download_url)))
        classified = classify_terms(rows)
        settings = pending.get("settings") or {}
        apply_negatives = bool(settings.get("apply_negatives", True))
        apply_winners = bool(settings.get("apply_winners", True))
        winner_bid = float(settings.get("winner_bid", payload.get("winner_bid", 0.90)))

        negatives_applied = apply_negatives_step(client, classified) if apply_negatives else []
        winners_applied = _apply_winner_keywords(client, classified, winner_bid) if apply_winners else []

        run_entry = {
            "timestamp": dt.datetime.utcnow().isoformat(),
            "report_id": report_id,
            "rows_analyzed": len(rows),
            "winners_found": len(classified.get("winners", [])),
            "negatives_found": len(classified.get("negatives", [])),
            "bid_down_found": len(classified.get("bid_down", [])),
            "winners_applied": len(winners_applied),
            "negatives_applied": len(negatives_applied),
        }
        _save_optimizer_history(run_entry)
        _save_pending_report({"report_id": None, "settings": {}, "ts": 0.0})

        return JSONResponse({
            "success": True,
            **run_entry,
            "negatives_applied_detail": negatives_applied,
            "winners_applied_detail": winners_applied,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.get("/api/optimizer-history")
def api_optimizer_history(authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    return JSONResponse({"history": _load_optimizer_history(), "pending_report": _load_pending_report()})


if __name__ == "__main__":
    print(json.dumps(run_optimizer(dry_run=True), indent=2, default=str))
