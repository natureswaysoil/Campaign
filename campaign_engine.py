"""Structured campaign engine for Nature's Way Soil Amazon PPC.

This module keeps the campaign intelligence separate from the live Amazon Ads client.
It reads config/campaign_rules.json and optional product/keyword columns from the
Google Sheet, then returns a dry-run plan that the optimizer API can use before
pushing changes live.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests

BASE_DIR = Path(__file__).parent
RULES_PATH = BASE_DIR / "config" / "campaign_rules.json"
PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv",
)


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str) -> str:
    text = normalize(text).replace(" ", "_")
    return re.sub(r"_+", "_", text).strip("_")[:60] or "product"


def split_keywords(value: str) -> List[str]:
    if not value:
        return []
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
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()
        value = lower_map.get(name.lower())
        if value not in (None, ""):
            return str(value).strip()
    return default


def money(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        return default


def load_rules(path: Path = RULES_PATH) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def is_real_product_row(row: Dict[str, Any]) -> bool:
    """Ignore blank rows and accidental repeated header rows from the Google Sheet."""
    product_id = first(row, "Product_ID", "Product ID", default="")
    sku = first(row, "SKU", default="")
    asin = first(row, "ASIN", default="")
    title = first(row, "Title", "Product_Name", "Product Name", default="")

    lower_values = {product_id.lower(), sku.lower(), asin.lower(), title.lower()}
    if {"product_id", "sku", "asin", "title"} & lower_values:
        return False

    # At minimum, a usable campaign row needs a product identifier/title and either ASIN or SKU.
    if not any([product_id, sku, asin, title]):
        return False
    if not any([sku, asin]):
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


def merge_unique(*groups: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
    return out


def build_campaign_plan(row: Dict[str, Any], rules: Dict[str, Any] | None = None) -> Dict[str, Any]:
    rules = rules or load_rules()
    groups = keyword_groups(row)
    name = product_name(row)
    slug = slugify(name)
    price = money(first(row, "Price", "Selling_Price", "Selling Price"), 0.0)
    target_acos = money(first(row, "Target_ACOS", "Target ACOS"), rules["optimization_rules"].get("target_acos_fallback", 0.35))
    asin = first(row, "ASIN")
    sku = first(row, "SKU")

    # Route ingredient/problem terms into the right funnels even if the sheet puts them in separate columns.
    groups["EXACT_Core"] = merge_unique(groups["EXACT_Core"], groups["ingredient"])
    groups["PHRASE_Research"] = merge_unique(groups["PHRASE_Research"], groups["problem"], groups["ingredient"])

    campaigns: List[Dict[str, Any]] = []
    for campaign_type, config in rules["campaign_types"].items():
        keywords: List[str] = []
        negatives = {
            "negative_phrase": groups["negative_phrase"],
            "negative_exact": groups["negative_exact"],
        }

        if campaign_type == "AUTO_Discovery":
            keywords = []
        elif campaign_type == "EXACT_Core":
            keywords = groups["EXACT_Core"]
        elif campaign_type == "EXACT_Long_Tail":
            keywords = groups["EXACT_Long_Tail"]
        elif campaign_type == "PHRASE_Research":
            keywords = groups["PHRASE_Research"]
        elif campaign_type == "BROAD_Discovery":
            keywords = merge_unique(groups["EXACT_Core"], groups["PHRASE_Research"], groups["EXACT_Long_Tail"])
        elif campaign_type == "COMPETITOR":
            keywords = groups["COMPETITOR"]
        elif campaign_type == "PRODUCT_Targeting":
            keywords = []

        daily_budget = money(first(row, "Daily_Budget", "Daily Budget"), float(config.get("daily_budget", 5.0)))
        default_bid = money(first(row, "Default_Bid", "Default Bid"), float(config.get("default_bid", 0.55)))
        min_bid = money(first(row, "Min_Bid", "Min Bid"), float(config.get("min_bid", 0.25)))
        max_bid = money(first(row, "Max_Bid", "Max Bid"), float(config.get("max_bid", 1.0)))

        campaigns.append({
            "campaign_type": campaign_type,
            "campaign_name": rules.get("campaign_name_pattern", "SP_{campaign_type}_{product_slug}").format(
                campaign_type=campaign_type,
                product_slug=slug,
            ),
            "match_type": config.get("match_type"),
            "purpose": config.get("purpose"),
            "daily_budget": daily_budget,
            "default_bid": default_bid,
            "min_bid": min_bid,
            "max_bid": max_bid,
            "keywords": keywords,
            "keyword_count": len(keywords),
            "negatives": negatives,
        })

    return {
        "product_name": name,
        "product_slug": slug,
        "asin": asin,
        "sku": sku,
        "price": price,
        "target_acos": target_acos,
        "campaigns": campaigns,
        "total_keywords": sum(c["keyword_count"] for c in campaigns),
    }


def build_all_campaign_plans(rows: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    rules = load_rules()
    rows = clean_product_rows(rows) if rows is not None else load_products_from_sheet()
    plans = [build_campaign_plan(row, rules) for row in rows]
    return {
        "product_count": len(plans),
        "required_sheet_columns": rules.get("required_sheet_columns", []),
        "plans": plans,
    }
