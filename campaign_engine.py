"""FINAL Campaign Engine - Robust Harvesting + Debug Prints + All Helpers Included"""

from __future__ import annotations
import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

BASE_DIR = Path(__file__).parent
RULES_PATH = BASE_DIR / "config" / "campaign_rules.json"
PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv",
)

# ====================== HELPERS ======================
def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def slugify(text: str) -> str:
    text = normalize(text).replace(" ", "_")
    return re.sub(r"_+", "_", text).strip("_")[:60] or "product"

def split_keywords(value: str) -> List[str]:
    if not value: return []
    parts = re.split(r"[\n,;|]+", str(value))
    out: List[str] = []
    seen = set()
    for part in parts:
        kw = normalize(part)
        if kw and kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out

def first(row: Dict[str, Any], *names: str, default: str = "") -> str:
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = row.get(name) or lower_map.get(name.lower())
        if value not in (None, ""):
            return str(value).strip()
    return default

def money(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except:
        return default

def load_rules(path: Path = RULES_PATH) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

def is_real_product_row(row: Dict[str, Any]) -> bool:
    product_id = first(row, "Product_ID", "Product ID", default="")
    sku = first(row, "SKU", default="")
    asin = first(row, "ASIN", default="")
    title = first(row, "Title", "Product_Name", "Product Name", default="")
    lower_values = {product_id.lower(), sku.lower(), asin.lower(), title.lower()}
    if {"product_id", "sku", "asin", "title"} & lower_values:
        return False
    if not any([product_id, sku, asin, title]) or not any([sku, asin]):
        return False
    return True

def clean_product_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if is_real_product_row(row)]

def load_products_from_sheet(url: str = PRODUCTS_CSV_URL) -> List[Dict[str, str]]:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    rows = [{str(k).strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    return clean_product_rows(rows)

def product_name(row: Dict[str, Any]) -> str:
    return first(row, "Product_Name", "Product Name", "Title", "SKU", "ASIN", default="Product")

def keyword_groups(row: Dict[str, Any]) -> Dict[str, List[str]]:
    return {
        "EXACT_Core": split_keywords(first(row, "Keywords", "Core_Keywords", "Core Keywords")),
        "PHRASE_Research": split_keywords(first(row, "Research_Keywords", "Research Keywords", "Problem_Keywords", "Problem Keywords")),
        "EXACT_Long_Tail": split_keywords(first(row, "Long_Tail_Keywords", "Long Tail Keywords")),
        "COMPETITOR": split_keywords(first(row, "Competitor_Keywords", "Competitor Keywords")),
        "negative_phrase": split_keywords(first(row, "Negative_Phrase", "Negative Phrase")),
        "negative_exact": split_keywords(first(row, "Negative_Exact", "Negative Exact")),
        "ingredient": split_keywords(first(row, "Ingredient_Keywords", "Ingredient Keywords")),
        "problem": split_keywords(first(row, "Problem_Keywords", "Problem Keywords")),
    }

def merge_unique(*groups: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
    return out

# ====================== ROBUST HARVESTING ENGINE ======================
class CampaignEngine:
    def __init__(self, target_acos: float = 0.35, product_margin: float = 0.40,
                 max_bid: float = 3.50, min_bid: float = 0.30):
        self.target_acos = target_acos
        self.product_margin = product_margin
        self.max_bid = max_bid
        self.min_bid = min_bid

    def generate_long_tail_keywords(self, product: Dict[str, str], num_variations: int = 12) -> List[str]:
        title = product.get("title", "") or product.get("Product_Name", "") or product.get("Title", "")
        title = title.lower()
        patterns = [
            f"{title.split()[0]} for raised beds",
            f"best {title.split()[0]} for vegetables",
            f"organic living {title.split()[0]}",
            f"{title.split()[0]} with beneficial microbes",
        ]
        return list(set(patterns))[:num_variations]

    def build_broad_match_campaign(self, product: Dict[str, Any], search_terms_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        short_name = slugify(product_name(product))[:40]
        campaign_name = f"BROAD_Discovery_{short_name}"
        keywords = self.generate_long_tail_keywords(product)
        return {
            "campaign_type": "BROAD_Discovery",
            "campaign_name": campaign_name,
            "match_type": "broad",
            "purpose": "Discovery + volume ramp",
            "daily_budget": 20.0,
            "default_bid": 0.75,
            "min_bid": self.min_bid,
            "max_bid": self.max_bid,
            "keywords": [{"keywordText": kw, "matchType": "broad"} for kw in keywords],
            "negative_keywords": [],
            "bidding_strategy": "dynamicBidsUpAndDown",
            "keyword_count": len(keywords)
        }

    def harvest_search_terms_to_exact_phrase(self, search_terms_df: pd.DataFrame, min_orders: int = 1, max_acos: float = 0.50) -> List[Dict]:
        if search_terms_df is None or len(search_terms_df) == 0:
            print("❌ No search term data loaded")
            return []

        print(f"🔍 Search term report columns found: {list(search_terms_df.columns)}")
        print(f"📊 Total rows in report: {len(search_terms_df)}")

        df = search_terms_df.copy()

        # ROBUST COLUMN MAPPING
        col_map = {
            "Customer Search Term": "search_term",
            "Search Term": "search_term",
            "search_term": "search_term",
            "7 Day Total Orders (#)": "orders",
            "Orders": "orders",
            "orders": "orders",
            "Total Advertising Cost of Sales (ACOS)": "acos",
            "ACOS": "acos",
            "acos": "acos",
            "Clicks": "clicks",
            "clicks": "clicks",
        }

        for old, new in col_map.items():
            if old in df.columns:
                df = df.rename(columns={old: new})

        print(f"✅ Mapped columns → search_term: {'search_term' in df.columns}, orders: {'orders' in df.columns}, acos: {'acos' in df.columns}")

        winners = df[
            (df.get("orders", 0) >= min_orders) &
            (df.get("acos", 999) <= max_acos) &
            (df.get("clicks", 0) >= 3)
        ].copy()

        print(f"🏆 Found {len(winners)} winning search terms to harvest")

        harvested = []
        for _, row in winners.iterrows():
            term = str(row.get("search_term") or row.get("Customer Search Term", "")).strip()
            if not term:
                continue
            match_type = "exact" if len(term.split()) > 5 else "phrase"
            harvested.append({
                "keywordText": term,
                "matchType": match_type,
                "bid": round(1.2 * 1.15, 2),
                "reason": f"{row.get('orders',0)} orders @ ACOS {row.get('acos',0):.1%}"
            })

        return harvested[:60]

    def decide_bid(self, row: Dict[str, Any], current_bid: float) -> Dict[str, Any]:
        spend = money(row.get("spend") or row.get("cost"), 0)
        orders = int(row.get("orders", 0) or row.get("purchases7d", 0))
        acos = money(row.get("acos"), 999)
        clicks = int(row.get("clicks", 0))
        conv_rate = (orders / clicks) if clicks > 0 else 0.0

        if spend > 120 and orders < 2:
            new_bid = round(max(current_bid * 0.60, self.min_bid), 2)
            return {"action": "decrease", "new_bid": new_bid, "reason": "CASH PROTECTION - high spend, low orders"}

        if orders >= 3 and acos <= self.target_acos * 0.90:
            multiplier = 1.32 if conv_rate >= 0.13 else 1.24
            new_bid = round(min(current_bid * multiplier, self.max_bid), 2)
        elif orders >= 2 and acos <= self.target_acos * 0.95:
            new_bid = round(min(current_bid * 1.22, self.max_bid), 2)
        elif orders >= 1 and acos <= self.target_acos * 1.08:
            new_bid = round(min(current_bid * 1.15, self.max_bid), 2)
        elif acos > self.target_acos * 1.45 or (clicks > 15 and orders == 0):
            new_bid = round(max(current_bid * 0.75, self.min_bid), 2)
        else:
            new_bid = current_bid

        action = "increase" if new_bid > current_bid else "decrease" if new_bid < current_bid else "hold"
        return {"action": action, "new_bid": new_bid, "reason": f"Orders:{orders} ACOS:{acos:.1%} CR:{conv_rate:.1%}"}


# ====================== BUILD FUNCTION ======================
def build_campaign_plan(row: Dict[str, Any], rules: Dict[str, Any] | None = None, 
                       search_terms_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    rules = rules or load_rules()
    engine = CampaignEngine(target_acos=0.35, product_margin=0.40)
    
    groups = keyword_groups(row)
    name = product_name(row)
    slug = slugify(name)
    asin = first(row, "ASIN")
    sku = first(row, "SKU")

    groups["EXACT_Core"] = merge_unique(groups["EXACT_Core"], groups["ingredient"])
    groups["PHRASE_Research"] = merge_unique(groups["PHRASE_Research"], groups["problem"], groups["ingredient"])

    campaigns: List[Dict[str, Any]] = []

    for campaign_type, config in rules["campaign_types"].items():
        if campaign_type == "BROAD_Discovery":
            continue

        keywords: List[str] = []
        if campaign_type == "AUTO_Discovery":
            keywords = []
        elif campaign_type == "EXACT_Core":
            keywords = groups["EXACT_Core"]
        elif campaign_type == "EXACT_Long_Tail":
            keywords = groups["EXACT_Long_Tail"]
        elif campaign_type == "PHRASE_Research":
            keywords = groups["PHRASE_Research"]
        elif campaign_type == "COMPETITOR":
            keywords = groups["COMPETITOR"]
        elif campaign_type == "PRODUCT_Targeting":
            keywords = []

        daily_budget = money(first(row, "Daily_Budget", "Daily Budget"), float(config.get("daily_budget", 5.0)))
        default_bid = money(first(row, "Default_Bid", "Default Bid"), float(config.get("default_bid", 0.55)))

        campaigns.append({
            "campaign_type": campaign_type,
            "campaign_name": rules.get("campaign_name_pattern", "SP_{campaign_type}_{product_slug}").format(
                campaign_type=campaign_type, product_slug=slug),
            "match_type": config.get("match_type"),
            "purpose": config.get("purpose"),
            "daily_budget": daily_budget,
            "default_bid": default_bid,
            "min_bid": money(first(row, "Min_Bid", "Min Bid"), 0.25),
            "max_bid": money(first(row, "Max_Bid", "Max Bid"), 1.0),
            "keywords": [{"keywordText": kw, "matchType": config.get("match_type", "phrase")} for kw in keywords],
            "keyword_count": len(keywords),
            "negative_keywords": [],
            "bidding_strategy": "dynamicBidsUpAndDown"
        })

    # Enhanced Broad Discovery
    broad_camp = engine.build_broad_match_campaign(row, search_terms_df)
    campaigns.append(broad_camp)

    # Real harvesting
    harvested = engine.harvest_search_terms_to_exact_phrase(search_terms_df) if search_terms_df is not None else []

    return {
        "product_name": name,
        "product_slug": slug,
        "asin": asin,
        "sku": sku,
        "target_acos": 0.35,
        "campaigns": campaigns,
        "harvested_keywords": harvested,
        "total_keywords": sum(c.get("keyword_count", 0) for c in campaigns) + len(harvested),
        "engine": engine
    }


def build_all_campaign_plans(rows: List[Dict[str, Any]] | None = None, search_terms_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    rules = load_rules()
    rows = clean_product_rows(rows) if rows is not None else load_products_from_sheet()
    plans = [build_campaign_plan(row, rules, search_terms_df) for row in rows]
    return {
        "product_count": len(plans),
        "plans": plans,
    }


if __name__ == "__main__":
    print("✅ FINAL Campaign Engine loaded with ROBUST harvesting + debug")
