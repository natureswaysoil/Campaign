# Create a clean fixed version
cat > app.py << 'ENDOFFILE'
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import csv
import gzip
import io
import json
import os
import re
import time
from typing import List, Dict, Any, Optional

import requests

app = FastAPI(title="Amazon Ads Dashboard")

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
    "negative_keywords": "/sp/negativeKeywords",
    "reports": "/reporting/reports",
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "our", "this", "that",
    "soil", "organic", "liquid", "natural", "plants", "plant", "garden", "lawn",
    "safe", "kids", "pets", "beneficial", "nature", "way"
}

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_secret(project_id: str, secret_id: str) -> str:
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def load_env_or_secret(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name)
    if value:
        return value

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


def today_yyyymmdd() -> str:
    return time.strftime("%Y%m%d")


def yyyymmdd_days_ago(days: int) -> str:
    return time.strftime("%Y%m%d", time.localtime(time.time() - (days * 86400)))


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def unique_in_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def truthy(v: str) -> bool:
    return str(v).strip().lower() in {"true", "yes", "1", "y", "active"}


def budget_from_price(price_value: str) -> float:
    try:
        price = float(str(price_value).replace("$", "").replace(",", "").strip())
    except Exception:
        return 25.0

    if price < 15:
        return 12.0
    if price < 25:
        return 18.0
    if price < 40:
        return 25.0
    return 35.0


def bid_from_price(price_value: str) -> float:
    try:
        price = float(str(price_value).replace("$", "").replace(",", "").strip())
    except Exception:
        return 0.85

    if price < 15:
        return 0.55
    if price < 25:
        return 0.75
    if price < 40:
        return 0.95
    return 1.10


def parse_keyword_cell(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[\n,;|]+", value)
    return [normalize(p) for p in parts if normalize(p)]


def title_ngrams(title: str) -> List[str]:
    clean = normalize(title)
    words = [w for w in clean.split() if w not in STOPWORDS and len(w) > 2]

    phrases = []
    if clean:
        phrases.append(clean)

    for n in (2, 3):
        for i in range(0, max(0, len(words) - n + 1)):
            phrases.append(" ".join(words[i:i + n]))

    return phrases


def keyword_hints_from_category(category: str) -> List[str]:
    c = normalize(category)
    hints: List[str] = []

    if "dog" in c or "pet" in c:
        hints += [
            "dog urine neutralizer",
            "dog urine lawn repair",
            "pet urine grass treatment",
        ]

    if "pasture" in c or "hay" in c or "lawn" in c:
        hints += [
            "pasture fertilizer",
            "hay fertilizer",
            "liquid lawn fertilizer",
            "grass fertilizer",
        ]

    if "bone" in c or "bloom" in c:
        hints += [
            "liquid bone meal",
            "phosphorus fertilizer",
            "bloom fertilizer",
        ]

    return hints


def generate_keywords(product: Dict[str, Any]) -> List[str]:
    merged: List[str] = []

    merged.extend(parse_keyword_cell(product.get("keywords", "")))
    merged.extend(parse_keyword_cell(product.get("research_keywords", "")))
    merged.extend(title_ngrams(product.get("title", "")))
    merged.extend(keyword_hints_from_category(product.get("category", "")))

    clean_keywords = []
    seen = set()

    for kw in merged:
        kw = normalize(kw)

        if not kw or len(kw) < 3:
            continue

        if len(kw) > 40:
            continue

        if kw not in seen:
            seen.add(kw)
            clean_keywords.append(kw)

    return clean_keywords[:30]


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
        "priority_level": product.get("Priority_Level", ""),
        "priority_score": product.get("Priority_Score", ""),
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


def extract_first_id(payload: Any) -> int:
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Unexpected payload: {payload}")
    row = payload[0]
    for key in ("campaignId", "adGroupId", "keywordId", "adId", "id"):
        if key in row:
            return int(row[key])
    raise RuntimeError(f"No ID found in payload: {payload}")


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


def negative_keyword_rows(negatives: List[str], campaign_id: int, ad_group_id: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = []
    for term in negatives:
        row = {
            "campaignId": campaign_id,
            "keywordText": term,
            "state": "enabled",
            "matchType": "negativeExact",
        }
        if ad_group_id is not None:
            row["adGroupId"] = ad_group_id
        rows.append(row)
    return rows


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


def verify_internal_token(authorization: Optional[str]) -> None:
    required = optional_env_or_secret("DAILY_OPTIMIZER_TOKEN")
    if not required:
        raise HTTPException(status_code=500, detail="DAILY_OPTIMIZER_TOKEN must be configured")
    
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.replace("Bearer ", "", 1).strip()
    if supplied != required:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


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
        if not resp.ok:
            raise RuntimeError(f"Amazon Ads API error {resp.status_code}: {resp.text}")
        return resp.json() if resp.text.strip() else None

    def get(self, endpoint: str) -> Any:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.get(url, headers=self.headers(), timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Amazon Ads API error {resp.status_code}: {resp.text}")
        return resp.json() if resp.text.strip() else None

    def download_binary(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=120)
        if not resp.ok:
            raise RuntimeError(f"Report download failed {resp.status_code}: {resp.text}")
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


def create_live_campaign_for_product(product: Dict[str, Any]) -> Dict[str, Any]:
    client = AmazonAdsClient()
    start_date = today_yyyymmdd()
    generated_keywords = generate_keywords(product)

    campaign_payload = {
        "name": f"{product['title']} | MANUAL | {start_date}",
        "campaignType": "sponsoredProducts",
        "targetingType": "manual",
        "state": "enabled",
        "dailyBudget": round(product["suggested_budget"], 2),
        "startDate": start_date,
    }
    campaign_resp = client.post(ENDPOINTS["campaigns"], campaign_payload)
    
    if isinstance(campaign_resp, dict):
        campaign_id = int(campaign_resp.get("campaignId") or campaign_resp.get("id"))
    else:
        campaign_id = extract_first_id(campaign_resp)

    ad_group_payload = {
        "name": "Main Ad Group",
        "campaignId": campaign_id,
        "state": "enabled",
        "defaultBid": round(product["suggested_bid"], 2),
    }
    ad_group_resp = client.post(ENDPOINTS["ad_groups"], ad_group_payload)
    
    if isinstance(ad_group_resp, dict):
        ad_group_id = int(ad_group_resp.get("adGroupId") or ad_group_resp.get("id"))
    else:
        ad_group_id = extract_first_id(ad_group_resp)

    product_ad_payload = {
        "campaignId": campaign_id,
        "adGroupId": ad_group_id,
        "asin": product["asin"],
        "sku": product["sku"],
        "state": "enabled",
    }
    product_ad_resp = client.post(ENDPOINTS["product_ads"], product_ad_payload)

    keywords_resp = []
    if generated_keywords:
        keywords_resp = client.post(
            ENDPOINTS["keywords"],
            keyword_rows(generated_keywords, ad_group_id, product["suggested_bid"])
        )

    return {
        "message": "Live campaign created",
        "product_id": product["product_id"],
        "sku": product["sku"],
        "asin": product["asin"],
        "title": product["title"],
        "budget": product["suggested_budget"],
        "bid": product["suggested_bid"],
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "keywords": generated_keywords,
        "campaign_response": campaign_resp,
        "ad_group_response": ad_group_resp,
        "product_ad_response": product_ad_resp,
        "keywords_response": keywords_resp,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/products")
def api_products():
    products = [normalized_product(r) for r in load_products()]
    return {"count": len(products), "products": products}


@app.get("/api/product/{key}")
def api_product(key: str):
    return find_product(key)


@app.get("/api/generate-keywords/{key}")
def api_keywords(key: str):
    p = find_product(key)
    return {"product": p, "keywords": generate_keywords(p)}


@app.post("/api/create-campaign-from-product")
def api_create_campaign(payload: Dict[str, Any]):
    key = payload.get("product_id") or payload.get("sku")
    if not key:
        raise HTTPException(status_code=400, detail="Provide product_id or sku")

    product = find_product(key)

    if not product["sku"] or not product["asin"] or not product["title"]:
        raise HTTPException(status_code=400, detail="Product is missing SKU, ASIN, or Title")

    try:
        return create_live_campaign_for_product(product)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bulk-create-campaigns")
def api_bulk_create(payload: Dict[str, Any]):
    launch_only_active = payload.get("launch_only_active", True)
    limit = payload.get("limit", 10)
    dry_run = payload.get("dry_run", True)

    products = [normalized_product(r) for r in load_products()]
    if launch_only_active:
        products = [p for p in products if p["active"]]
    if isinstance(limit, int):
        products = products[:limit]

    results = []
    errors = []

    for p in products:
        if not p["sku"] or not p["asin"] or not p["title"]:
            errors.append({
                "product_id": p["product_id"],
                "sku": p["sku"],
                "error": "Missing SKU, ASIN, or Title",
            })
            continue

        if dry_run:
            results.append({
                "product_id": p["product_id"],
                "sku": p["sku"],
                "asin": p["asin"],
                "title": p["title"],
                "budget": p["suggested_budget"],
                "bid": p["suggested_bid"],
                "keywords_count": len(generate_keywords(p)),
                "status": "ready",
            })
        else:
            try:
                results.append(create_live_campaign_for_product(p))
            except Exception as e:
                errors.append({
                    "product_id": p["product_id"],
                    "sku": p["sku"],
                    "error": str(e),
                })

    return {
        "requested": len(products),
        "dry_run": dry_run,
        "created": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


@app.post("/api/run-daily-optimization")
def api_run_optimizer(
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(default=None),
):
    verify_internal_token(authorization)

    apply_negatives_live = payload.get("apply_negatives_live", False)
    apply_winners_live = payload.get("apply_winners_live", False)
    winner_bid = float(payload.get("winner_bid", 0.9))

    min_clicks_for_negative = int(payload.get("min_clicks_for_negative", 20))
    min_orders_for_winner = int(payload.get("min_orders_for_winner", 2))
    max_acos_for_winner = float(payload.get("max_acos_for_winner", 0.35))
    min_clicks_for_winner = int(payload.get("min_clicks_for_winner", 8))

    try:
        client = AmazonAdsClient()

        end_date = payload.get("end_date") or yyyymmdd_days_ago(1)
        start_date = payload.get("start_date") or yyyymmdd_days_ago(8)

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

        for _ in range(18):
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

            time.sleep(20)

        if not download_url:
            raise RuntimeError(f"Timed out waiting for report. Last status: {status_payload}")

        content = client.download_binary(download_url)
        rows = parse_report_json_bytes(content)

        summary = classify_terms(
            rows=rows,
            min_clicks_for_negative=min_clicks_for_negative,
            min_orders_for_winner=min_orders_for_winner,
            max_acos_for_winner=max_acos_for_winner,
            min_clicks_for_winner=min_clicks_for_winner,
        )

        live_actions = {"negative_terms_added": [], "winner_terms_promoted": []}

        if apply_negatives_live:
            grouped_negatives: Dict[tuple, List[str]] = {}
            for item in summary["negatives"]:
                key = (item["campaign_id"], item["ad_group_id"])
                grouped_negatives.setdefault(key, []).append(item["term"])

            for (campaign_id, ad_group_id), terms in grouped_negatives.items():
                rows_to_add = negative_keyword_rows(unique_in_order(terms), campaign_id, ad_group_id or None)
                client.post(ENDPOINTS["negative_keywords"], rows_to_add)
                live_actions["negative_terms_added"].append({
                    "campaign_id": campaign_id,
                    "ad_group_id": ad_group_id,
                    "terms": unique_in_order(terms),
                })

        if apply_winners_live:
            grouped_winners: Dict[int, List[str]] = {}
            for item in summary["winners"]:
                grouped_winners.setdefault(item["ad_group_id"], []).append(item["term"])

            for ad_group_id, terms in grouped_winners.items():
                rows_to_add = [{
                    "adGroupId": ad_group_id,
                    "keywordText": term,
                    "matchType": "exact",
                    "state": "enabled",
                    "bid": round(winner_bid, 2),
                } for term in unique_in_order(terms)]
                client.post(ENDPOINTS["keywords"], rows_to_add)
                live_actions["winner_terms_promoted"].append({
                    "ad_group_id": ad_group_id,
                    "terms": unique_in_order(terms),
                })

        return {
            "message": "Optimizer finished",
            "report_id": report_id,
            "report_window": {"start_date": start_date, "end_date": end_date},
            "summary_counts": {
                "rows": len(rows),
                "winners": len(summary["winners"]),
                "negatives": len(summary["negatives"]),
                "hold": len(summary["hold"]),
            },
            "winners": summary["winners"],
            "negatives": summary["negatives"],
            "hold": summary["hold"],
            "live_actions": live_actions,
            "settings": {
                "apply_negatives_live": apply_negatives_live,
                "apply_winners_live": apply_winners_live,
                "winner_bid": winner_bid,
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
ENDOFFILE

# Verify syntax
python3 -c "import ast; ast.parse(open('app.py').read())" && echo "✓ Syntax is valid!"

# Deploy
git add app.py
git commit -m "Complete fixed app.py with Amazon Ads API format fix"
gcloud builds submit --config=cloudbuild.yaml
