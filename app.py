from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import csv
import io
import os
import re
import requests
from typing import List, Dict, Any

app = FastAPI(title="Amazon Ads Dashboard")

PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv",
)

STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "our", "this", "that",
    "soil", "organic", "liquid", "natural", "plants", "plant", "garden", "lawn",
    "safe", "kids", "pets", "beneficial", "nature", "way"
}

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_products() -> List[Dict[str, str]]:
    r = requests.get(PRODUCTS_CSV_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]


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


def parse_keyword_cell(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[\n,;|]+", value)
    return [normalize(p) for p in parts if normalize(p)]


def title_ngrams(title: str) -> List[str]:
    clean = normalize(title)
    words = [w for w in clean.split() if w not in STOPWORDS and len(w) > 2]
    phrases = [clean] if clean else []
    for n in (2, 3, 4):
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

    p = find_product(key)

    return {
        "message": "Campaign ready (simulation)",
        "product_id": p["product_id"],
        "sku": p["sku"],
        "asin": p["asin"],
        "title": p["title"],
        "budget": p["suggested_budget"],
        "bid": p["suggested_bid"],
        "keywords": generate_keywords(p),
    }


@app.post("/api/bulk-create-campaigns")
def api_bulk_create(payload: Dict[str, Any]):
    launch_only_active = payload.get("launch_only_active", True)
    limit = payload.get("limit")

    products = [normalized_product(r) for r in load_products()]
    if launch_only_active:
        products = [p for p in products if p["active"]]
    if isinstance(limit, int):
        products = products[:limit]

    results = []
    for p in products:
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

    return {"requested": len(products), "results": results}


@app.post("/api/run-daily-optimization")
def api_run_optimizer(payload: Dict[str, Any]):
    return {
        "message": "Optimizer dry-run ready",
        "settings": {
            "apply_negatives_live": payload.get("apply_negatives_live", False),
            "apply_winners_live": payload.get("apply_winners_live", False),
            "winner_bid": payload.get("winner_bid", 0.9),
        }
    }
