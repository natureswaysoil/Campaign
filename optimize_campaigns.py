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
    supplied = ""
    if x_daily_optimizer_token:
        supplied = x_daily_optimizer_token.strip()
    elif authorization and authorization.startswith("Bearer "):
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
        if response.status_code in {425, 429}:
            time.sleep(2)
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
        return {}


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
        return JSONResponse({
            "success": True,
            "active_only": True,
            "campaign_count": len(active_campaigns),
            "active_campaign_count": len(active_campaigns),
            "total_campaign_count": len(all_campaigns),
            "paused_campaign_count": sum(1 for c in all_campaigns if str(c.get("state") or "").upper() == "PAUSED"),
            "archived_campaign_count": sum(1 for c in all_campaigns if str(c.get("state") or "").upper() == "ARCHIVED"),
            "campaigns": active_campaigns,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


@app.post("/api/run-optimizer")
def api_run_optimizer(payload: Dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None), x_daily_optimizer_token: Optional[str] = Header(default=None)) -> JSONResponse:
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        return JSONResponse(run_optimizer(payload.get("search_terms_path"), dry_run=bool(payload.get("dry_run", True))))
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)


if __name__ == "__main__":
    print(json.dumps(run_optimizer(dry_run=True), indent=2, default=str))
