from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import csv
from datetime import date, datetime, timezone
import gzip
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
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

# These are the same Sponsored Products paths your project has been using.
ENDPOINTS = {
    "campaigns": "/sp/campaigns",
    "ad_groups": "/sp/adGroups",
    "product_ads": "/sp/productAds",
    "keywords": "/sp/keywords",
    "negative_keywords": "/sp/negativeKeywords",
    "reports": "/reporting/reports",
}

STRING_FIELDS = {
    "campaignId",
    "adGroupId",
    "keywordId",
    "targetId",
    "adId",
    "id",
    "asin",
    "sku",
    "startDate",
    "endDate",
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "our", "this", "that",
    "soil", "organic", "liquid", "natural", "plants", "plant", "garden", "lawn",
    "safe", "kids", "pets", "beneficial", "nature", "way"
}

DATA_DIR = Path("data")
LAUNCH_LOG_PATH = DATA_DIR / "campaign_launches.jsonl"
OPTIMIZER_LOG_PATH = DATA_DIR / "optimizer_runs.jsonl"

OPTIMIZATION_CHECKLIST = {
    "negatives": [
        "Add obvious mismatch negatives from the first search term report.",
        "Add low-intent modifiers as phrase or exact negatives where irrelevant.",
        "Maintain a shared negative list for repeated waste terms.",
        "Review weekly and promote recurring waste queries to permanent negatives.",
    ],
    "bid_tiers": [
        "Tier A: core high-intent terms, bid above baseline.",
        "Tier B: category terms, keep at baseline bid.",
        "Tier C: exploratory long-tail terms, bid below baseline.",
        "Promote winners up one tier and demote costly non-converters.",
    ],
    "search_term_harvesting": [
        "Pull search term reports every 3 to 7 days.",
        "Promote converting queries into exact-match terms.",
        "Reduce bid or negate queries beyond no-sale spend threshold.",
        "Use broad and phrase for discovery, exact for efficiency.",
    ],
}

BUDGET_GUARDRAILS = {
    "min_daily_budget": 10.0,
    "max_step_pct": 0.25,
    "cooldown_hours": 48,
    "weekly_change_cap_pct": 0.5,
}

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def auto_open_dashboard_enabled() -> bool:
    # Keep browser auto-open to local dev only and allow opt-out.
    if os.getenv("K_SERVICE"):
        return False
    flag = str(os.getenv("AUTO_OPEN_DASHBOARD", "true")).strip().lower()
    return flag in {"1", "true", "yes", "y"}


def open_dashboard_in_browser() -> None:
    browser = os.getenv("BROWSER")
    if not browser:
        return

    port = os.getenv("PORT", "8080")
    url = f"http://127.0.0.1:{port}/"

    try:
        subprocess.Popen([browser, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


@app.on_event("startup")
def auto_open_dashboard_on_startup() -> None:
    if auto_open_dashboard_enabled():
        timer = threading.Timer(1.0, open_dashboard_in_browser)
        timer.daemon = True
        timer.start()


# -----------------------------
# Secrets / config
# -----------------------------
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


# -----------------------------
# Helpers
# -----------------------------
def today_yyyymmdd() -> str:
    return time.strftime("%Y%m%d")


def yyyymmdd_days_ago(days: int) -> str:
    return time.strftime("%Y%m%d", time.localtime(time.time() - (days * 86400)))


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def maybe_fix_mojibake(text: str) -> str:
    """Repair common UTF-8-as-Latin-1 mojibake (for example: Natureâ€™s -> Nature's)."""
    if not text:
        return ""

    suspicious_markers = ("Ã", "â", "Â")
    if not any(marker in text for marker in suspicious_markers):
        return text

    try:
        fixed = text.encode("latin-1").decode("utf-8")
        return fixed
    except Exception:
        return text


def unique_in_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except json.JSONDecodeError:
                continue
    return rows


def count_container_errors(payload: Any, container_key: str) -> int:
    if not isinstance(payload, dict):
        return 0
    container = payload.get(container_key)
    if not isinstance(container, dict):
        return 0
    errors = container.get("error")
    return len(errors) if isinstance(errors, list) else 0


def dedupe_keywords(keywords: List[str]) -> List[str]:
    return unique_in_order([normalize(k) for k in keywords if normalize(k)])


def build_campaign_launch_summary(
    product: Dict[str, Any],
    campaign_id: str,
    ad_group_id: str,
    product_ad_resp: Any,
    seed_keywords: List[str],
    created_keyword_rows: int,
    campaign_resp: Any,
    ad_group_resp: Any,
    keywords_resp: Any,
) -> Dict[str, Any]:
    product_ad_id = ""
    try:
        product_ad_id = extract_first_id(product_ad_resp)
    except Exception:
        product_ad_id = ""

    title = maybe_fix_mojibake(product.get("title", ""))
    return {
        "event_type": "campaign_launch",
        "status": "success",
        "launched_at_utc": now_iso_utc(),
        "account": {
            "product_id": product.get("product_id", ""),
            "sku": product.get("sku", ""),
            "asin": product.get("asin", ""),
        },
        "campaign": {
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "name": title,
            "daily_budget": round(float(product.get("suggested_budget", 0.0)), 2),
            "default_bid": round(float(product.get("suggested_bid", 0.0)), 2),
            "state": "enabled",
        },
        "creation_results": {
            "campaign_errors": count_container_errors(campaign_resp, "campaigns"),
            "ad_group_errors": count_container_errors(ad_group_resp, "adGroups"),
            "product_ad_errors": count_container_errors(product_ad_resp, "productAds"),
            "keyword_errors": count_container_errors(keywords_resp, "keywords"),
            "keyword_success_count": created_keyword_rows,
        },
        "keyword_inputs": {
            "seed_count": len(seed_keywords),
            "deduplicated_seed_count": len(dedupe_keywords(seed_keywords)),
        },
        "ids": {
            "product_ad_id": product_ad_id,
        },
        "quality_flags": {
            "title_encoding_cleaned": title != product.get("title", ""),
            "keyword_expansion_detected": created_keyword_rows > len(seed_keywords),
            "expansion_note": f"{len(seed_keywords)} seed terms produced {created_keyword_rows} created keyword entries",
        },
    }


def parse_iso_utc(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def budget_adjustment_recommendation(
    current_budget: float,
    target_acos: float,
    acos: float,
    budget_utilization: float,
    clicks: int,
    orders: int,
    spend: float,
    last_adjusted_at: Optional[str],
    weekly_change_pct: float,
    no_sale_click_threshold: int = 20,
) -> Dict[str, Any]:
    min_budget = float(BUDGET_GUARDRAILS["min_daily_budget"])
    max_step_pct = float(BUDGET_GUARDRAILS["max_step_pct"])
    cooldown_hours = int(BUDGET_GUARDRAILS["cooldown_hours"])
    weekly_cap_pct = float(BUDGET_GUARDRAILS["weekly_change_cap_pct"])

    if current_budget <= 0:
        current_budget = min_budget

    reason = "hold"
    action = "hold"
    requested_step_pct = 0.0
    now = datetime.now(timezone.utc)

    if last_adjusted_at:
        dt = parse_iso_utc(last_adjusted_at)
        if dt is not None:
            hours_since = (now - dt).total_seconds() / 3600
            if hours_since < cooldown_hours:
                return {
                    "action": "hold",
                    "reason": f"cooldown_active_{cooldown_hours}h",
                    "recommended_budget": round(current_budget, 2),
                    "step_pct": 0.0,
                    "guardrails": BUDGET_GUARDRAILS,
                }

    if budget_utilization >= 0.9 and target_acos > 0 and acos <= target_acos and clicks >= 20:
        action = "increase"
        reason = "budget_capped_and_acos_on_target"
        requested_step_pct = 0.15
    elif target_acos > 0 and acos > (1.25 * target_acos) and spend >= current_budget and clicks >= 20:
        action = "decrease"
        reason = "acos_above_threshold"
        requested_step_pct = -0.10
    elif orders == 0 and clicks >= no_sale_click_threshold:
        action = "decrease"
        reason = "no_orders_after_click_threshold"
        requested_step_pct = -0.15

    capped_step_pct = max(-max_step_pct, min(max_step_pct, requested_step_pct))

    if action != "hold" and abs(weekly_change_pct + capped_step_pct) > weekly_cap_pct:
        return {
            "action": "hold",
            "reason": "weekly_change_cap_reached",
            "recommended_budget": round(current_budget, 2),
            "step_pct": 0.0,
            "guardrails": BUDGET_GUARDRAILS,
        }

    new_budget = current_budget * (1.0 + capped_step_pct)
    new_budget = max(min_budget, new_budget)

    return {
        "action": action,
        "reason": reason,
        "recommended_budget": round(new_budget, 2),
        "step_pct": round(capped_step_pct, 4),
        "guardrails": BUDGET_GUARDRAILS,
    }


def campaign_budget_history(campaign_id: str) -> List[Dict[str, Any]]:
    events = read_jsonl(LAUNCH_LOG_PATH)
    out: List[Dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "budget_adjustment":
            continue
        if str(event.get("campaign_id", "")) != str(campaign_id):
            continue
        out.append(event)
    return out


def last_budget_adjusted_at(campaign_id: str) -> Optional[str]:
    history = campaign_budget_history(campaign_id)
    if not history:
        return None
    return str(history[-1].get("adjusted_at_utc") or "") or None


def weekly_change_pct_so_far(campaign_id: str) -> float:
    now = datetime.now(timezone.utc)
    total = 0.0
    for event in campaign_budget_history(campaign_id):
        dt = parse_iso_utc(str(event.get("adjusted_at_utc", "")))
        if dt is None:
            continue
        if (now - dt).total_seconds() > 7 * 86400:
            continue
        try:
            total += float(event.get("step_pct", 0.0))
        except Exception:
            continue
    return total


def launch_log_export_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("event_type") != "campaign_launch":
            continue
        campaign = row.get("campaign", {}) if isinstance(row.get("campaign"), dict) else {}
        account = row.get("account", {}) if isinstance(row.get("account"), dict) else {}
        creation = row.get("creation_results", {}) if isinstance(row.get("creation_results"), dict) else {}
        out.append({
            "launched_at_utc": row.get("launched_at_utc", ""),
            "product_id": account.get("product_id", ""),
            "sku": account.get("sku", ""),
            "asin": account.get("asin", ""),
            "campaign_id": campaign.get("campaign_id", ""),
            "ad_group_id": campaign.get("ad_group_id", ""),
            "daily_budget": campaign.get("daily_budget", ""),
            "default_bid": campaign.get("default_bid", ""),
            "keyword_success_count": creation.get("keyword_success_count", 0),
            "keyword_errors": creation.get("keyword_errors", 0),
        })
    return out


def optimizer_log_export_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        summary = row.get("summary_counts", {}) if isinstance(row.get("summary_counts"), dict) else {}
        settings = row.get("settings", {}) if isinstance(row.get("settings"), dict) else {}
        out.append({
            "ran_at_utc": row.get("ran_at_utc", ""),
            "report_id": row.get("report_id", ""),
            "start_date": row.get("report_window", {}).get("start_date", "") if isinstance(row.get("report_window"), dict) else "",
            "end_date": row.get("report_window", {}).get("end_date", "") if isinstance(row.get("report_window"), dict) else "",
            "rows": summary.get("rows", 0),
            "winners": summary.get("winners", 0),
            "negatives": summary.get("negatives", 0),
            "hold": summary.get("hold", 0),
            "apply_negatives_live": settings.get("apply_negatives_live", False),
            "apply_winners_live": settings.get("apply_winners_live", False),
        })
    return out


def rows_to_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    headers = list(rows[0].keys())
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def parse_filter_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None

    candidates = [value]
    if "T" in value:
        candidates.append(value.split("T", 1)[0])

    for candidate in candidates:
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").date()
        except ValueError:
            pass
        try:
            return datetime.strptime(candidate, "%Y%m%d").date()
        except ValueError:
            pass
    return None


def parse_event_date(value: str) -> Optional[date]:
    dt = parse_iso_utc(value)
    if dt is not None:
        return dt.date()
    return parse_filter_date(value)


def in_date_range(event_date: Optional[date], start_date: Optional[date], end_date: Optional[date]) -> bool:
    if event_date is None:
        return False
    if start_date and event_date < start_date:
        return False
    if end_date and event_date > end_date:
        return False
    return True


def filter_launch_log_rows(
    rows: List[Dict[str, Any]],
    start_date_value: str,
    end_date_value: str,
    campaign_id: str,
) -> List[Dict[str, Any]]:
    start_date = parse_filter_date(start_date_value)
    end_date = parse_filter_date(end_date_value)
    campaign_filter = (campaign_id or "").strip()

    out: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("event_type") != "campaign_launch":
            continue
        event_date = parse_event_date(str(row.get("launched_at_utc", "")))
        if (start_date or end_date) and not in_date_range(event_date, start_date, end_date):
            continue
        this_campaign_id = str(row.get("campaign", {}).get("campaign_id", "")) if isinstance(row.get("campaign"), dict) else ""
        if campaign_filter and this_campaign_id != campaign_filter:
            continue
        out.append(row)
    return out


def optimizer_run_campaign_ids(row: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    campaign_filter = str(settings.get("campaign_filter", "")).strip() if isinstance(settings, dict) else ""
    if campaign_filter:
        ids.append(campaign_filter)

    live_actions = row.get("live_actions") if isinstance(row.get("live_actions"), dict) else {}
    negatives = live_actions.get("negative_terms_added") if isinstance(live_actions, dict) else []
    if isinstance(negatives, list):
        for item in negatives:
            if not isinstance(item, dict):
                continue
            campaign_id = str(item.get("campaign_id", "")).strip()
            if campaign_id:
                ids.append(campaign_id)
    return unique_in_order(ids)


def filter_optimizer_rows(
    rows: List[Dict[str, Any]],
    start_date_value: str,
    end_date_value: str,
    campaign_id: str,
) -> List[Dict[str, Any]]:
    start_date = parse_filter_date(start_date_value)
    end_date = parse_filter_date(end_date_value)
    campaign_filter = (campaign_id or "").strip()

    out: List[Dict[str, Any]] = []
    for row in rows:
        event_date = parse_event_date(str(row.get("ran_at_utc", "")))
        if (start_date or end_date) and not in_date_range(event_date, start_date, end_date):
            continue
        if campaign_filter:
            campaign_ids = optimizer_run_campaign_ids(row)
            if campaign_filter not in campaign_ids:
                continue
        out.append(row)
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
    return [{k.strip(): maybe_fix_mojibake((v or "").strip()) for k, v in row.items()} for row in reader]


def normalized_product(product: Dict[str, str]) -> Dict[str, Any]:
    return {
        "product_id": maybe_fix_mojibake(product.get("Product_ID", "")),
        "sku": maybe_fix_mojibake(product.get("SKU", "")),
        "asin": maybe_fix_mojibake(product.get("ASIN", "")),
        "title": maybe_fix_mojibake(product.get("Title", "")),
        "price": maybe_fix_mojibake(product.get("Selling_Price", "")),
        "active": truthy(product.get("Active", "TRUE")),
        "category": maybe_fix_mojibake(product.get("Category", "")),
        "keywords": maybe_fix_mojibake(product.get("Keywords", "")),
        "research_keywords": maybe_fix_mojibake(product.get("Research_Keywords", "")),
        "priority_level": maybe_fix_mojibake(product.get("Priority_Level", "")),
        "priority_score": maybe_fix_mojibake(product.get("Priority_Score", "")),
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


def extract_first_id(payload: Any) -> str:
    id_keys = ("campaignId", "adGroupId", "keywordId", "adId", "id")

    if isinstance(payload, list) and payload:
        row = payload[0]
        if isinstance(row, dict):
            for key in id_keys:
                if key in row and row[key] not in (None, ""):
                    return str(row[key]).strip()

    if isinstance(payload, dict):
        grouped_keys = ("campaigns", "adGroups", "productAds", "keywords", "targets")
        for group_key in grouped_keys:
            group = payload.get(group_key)
            if not isinstance(group, dict):
                continue
            success = group.get("success")
            if not isinstance(success, list) or not success:
                continue
            row = success[0]
            if not isinstance(row, dict):
                continue

            for key in id_keys:
                if key in row and row[key] not in (None, ""):
                    return str(row[key]).strip()

            for nested_value in row.values():
                if not isinstance(nested_value, dict):
                    continue
                for key in id_keys:
                    if key in nested_value and nested_value[key] not in (None, ""):
                        return str(nested_value[key]).strip()

    raise RuntimeError(f"No ID found in payload: {payload}")


def extract_success_rows(payload: Any, container_key: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if isinstance(payload, dict):
        container = payload.get(container_key)
        if isinstance(container, dict):
            success = container.get("success")
            if isinstance(success, list):
                return [row for row in success if isinstance(row, dict)]

    return []


def coerce_string_fields(payload: Any) -> Any:
    if isinstance(payload, list):
        return [coerce_string_fields(item) for item in payload]
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key, value in payload.items():
            if key in STRING_FIELDS and value not in (None, ""):
                out[key] = str(value).strip()
            else:
                out[key] = coerce_string_fields(value)
        return out
    return payload


def keyword_rows(keywords: List[str], ad_group_id: str, bid: float) -> List[Dict[str, Any]]:
    rows = []
    for kw in dedupe_keywords(keywords):
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


def negative_keyword_rows(negatives: List[str], campaign_id: str, ad_group_id: Optional[str] = None) -> List[Dict[str, Any]]:
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


def id_text(row: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            value = str(row[key]).strip()
            if value:
                return value
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
        campaign_id = id_text(row, ["Campaign Id", "campaignId"], "")
        ad_group_id = id_text(row, ["Ad Group Id", "adGroupId"], "")
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
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.replace("Bearer ", "", 1).strip()
    if supplied != required:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


# -----------------------------
# Amazon Ads live client
# -----------------------------
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
        resp = self.session.post(
            url,
            headers=self.headers(),
            json=coerce_string_fields(body),
            timeout=60,
        )
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
    generated_keywords = dedupe_keywords(generate_keywords(product))
    keyword_payload = keyword_rows(generated_keywords, "__pending_ad_group__", product["suggested_bid"])

    campaign_payload = [{
        "name": f"{product['title']} | MANUAL | {start_date}",
        "campaignType": "sponsoredProducts",
        "targetingType": "manual",
        "state": "enabled",
        "dailyBudget": round(product["suggested_budget"], 2),
        "startDate": start_date,
    }]
    campaign_resp = client.post(ENDPOINTS["campaigns"], campaign_payload)
    campaign_id = extract_first_id(campaign_resp)

    ad_group_payload = [{
        "name": "Main Ad Group",
        "campaignId": campaign_id,
        "state": "enabled",
        "defaultBid": round(product["suggested_bid"], 2),
    }]
    ad_group_resp = client.post(ENDPOINTS["ad_groups"], ad_group_payload)
    ad_group_id = extract_first_id(ad_group_resp)

    product_ad_payload = [{
        "campaignId": campaign_id,
        "adGroupId": ad_group_id,
        "asin": product["asin"],
        "sku": product["sku"],
        "state": "enabled",
    }]
    product_ad_resp = client.post(ENDPOINTS["product_ads"], product_ad_payload)

    keywords_resp = []
    if generated_keywords:
        keyword_payload = keyword_rows(generated_keywords, ad_group_id, product["suggested_bid"])
        keywords_resp = client.post(
            ENDPOINTS["keywords"],
            keyword_payload
        )

    keyword_success_rows = extract_success_rows(keywords_resp, "keywords")
    keyword_errors = []
    if isinstance(keywords_resp, dict):
        container = keywords_resp.get("keywords")
        if isinstance(container, dict) and isinstance(container.get("error"), list):
            keyword_errors = container.get("error")

    match_type_breakdown = {
        "exact": len(generated_keywords),
        "phrase": len(generated_keywords),
        "broad": len(generated_keywords),
    }

    launch_summary = build_campaign_launch_summary(
        product=product,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        product_ad_resp=product_ad_resp,
        seed_keywords=generated_keywords,
        created_keyword_rows=len(keyword_success_rows),
        campaign_resp=campaign_resp,
        ad_group_resp=ad_group_resp,
        keywords_resp=keywords_resp,
    )
    append_jsonl(LAUNCH_LOG_PATH, launch_summary)

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
        "keyword_summary": {
            "base_keywords_count": len(generated_keywords),
            "rows_submitted": len(keyword_payload),
            "rows_created": len(keyword_success_rows),
            "rows_failed": len(keyword_errors),
            "match_type_breakdown": match_type_breakdown,
        },
        "campaign_response": campaign_resp,
        "ad_group_response": ad_group_resp,
        "product_ad_response": product_ad_resp,
        "keywords_response": keywords_resp,
        "launch_summary": launch_summary,
    }


# -----------------------------
# Routes
# -----------------------------
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


@app.get("/api/optimization-checklist")
def api_optimization_checklist():
    return {
        "message": "Post-launch optimization checklist",
        "sections": OPTIMIZATION_CHECKLIST,
        "budget_guardrails": BUDGET_GUARDRAILS,
    }


@app.get("/api/launch-logs")
def api_launch_logs(
    limit: int = 25,
    start_date: str = "",
    end_date: str = "",
    campaign_id: str = "",
):
    rows = read_jsonl(LAUNCH_LOG_PATH)
    campaign_rows = filter_launch_log_rows(rows, start_date, end_date, campaign_id)
    if limit > 0:
        campaign_rows = campaign_rows[-limit:]
    return {
        "count": len(campaign_rows),
        "logs": list(reversed(campaign_rows)),
    }


@app.get("/api/launch-logs/latest")
def api_latest_launch_log(campaign_id: str = ""):
    rows = read_jsonl(LAUNCH_LOG_PATH)
    campaign_rows = filter_launch_log_rows(rows, "", "", campaign_id)
    if not campaign_rows:
        return {"found": False, "log": None}
    return {"found": True, "log": campaign_rows[-1]}


@app.get("/api/export/launch-logs.json")
def api_export_launch_logs_json(
    limit: int = 200,
    start_date: str = "",
    end_date: str = "",
    campaign_id: str = "",
):
    rows = read_jsonl(LAUNCH_LOG_PATH)
    campaign_rows = filter_launch_log_rows(rows, start_date, end_date, campaign_id)
    if limit > 0:
        campaign_rows = campaign_rows[-limit:]
    payload = json.dumps({"count": len(campaign_rows), "logs": campaign_rows}, ensure_ascii=False, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=launch_logs.json"},
    )


@app.get("/api/export/launch-logs.csv")
def api_export_launch_logs_csv(
    limit: int = 200,
    start_date: str = "",
    end_date: str = "",
    campaign_id: str = "",
):
    rows = read_jsonl(LAUNCH_LOG_PATH)
    campaign_rows = filter_launch_log_rows(rows, start_date, end_date, campaign_id)
    if limit > 0:
        campaign_rows = campaign_rows[-limit:]
    csv_rows = launch_log_export_rows(campaign_rows)
    return Response(
        content=rows_to_csv_bytes(csv_rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=launch_logs.csv"},
    )


@app.get("/api/optimizer-runs")
def api_optimizer_runs(
    limit: int = 25,
    start_date: str = "",
    end_date: str = "",
    campaign_id: str = "",
):
    rows = read_jsonl(OPTIMIZER_LOG_PATH)
    rows = filter_optimizer_rows(rows, start_date, end_date, campaign_id)
    if limit > 0:
        rows = rows[-limit:]
    return {
        "count": len(rows),
        "runs": list(reversed(rows)),
    }


@app.get("/api/export/optimizer-runs.json")
def api_export_optimizer_runs_json(
    limit: int = 200,
    start_date: str = "",
    end_date: str = "",
    campaign_id: str = "",
):
    rows = read_jsonl(OPTIMIZER_LOG_PATH)
    rows = filter_optimizer_rows(rows, start_date, end_date, campaign_id)
    if limit > 0:
        rows = rows[-limit:]
    payload = json.dumps({"count": len(rows), "runs": rows}, ensure_ascii=False, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=optimizer_runs.json"},
    )


@app.get("/api/export/optimizer-runs.csv")
def api_export_optimizer_runs_csv(
    limit: int = 200,
    start_date: str = "",
    end_date: str = "",
    campaign_id: str = "",
):
    rows = read_jsonl(OPTIMIZER_LOG_PATH)
    rows = filter_optimizer_rows(rows, start_date, end_date, campaign_id)
    if limit > 0:
        rows = rows[-limit:]
    csv_rows = optimizer_log_export_rows(rows)
    return Response(
        content=rows_to_csv_bytes(csv_rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=optimizer_runs.csv"},
    )


@app.post("/api/recommend-budget-adjustment")
def api_recommend_budget_adjustment(payload: Dict[str, Any]):
    campaign_id = str(payload.get("campaign_id", "")).strip()
    if not campaign_id:
        raise HTTPException(status_code=400, detail="Provide campaign_id")

    try:
        current_budget = float(payload.get("current_budget"))
        target_acos = float(payload.get("target_acos"))
        acos = float(payload.get("acos"))
        budget_utilization = float(payload.get("budget_utilization"))
        clicks = int(payload.get("clicks", 0))
        orders = int(payload.get("orders", 0))
        spend = float(payload.get("spend", 0.0))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid numeric input: {exc}")

    recommendation = budget_adjustment_recommendation(
        current_budget=current_budget,
        target_acos=target_acos,
        acos=acos,
        budget_utilization=budget_utilization,
        clicks=clicks,
        orders=orders,
        spend=spend,
        last_adjusted_at=last_budget_adjusted_at(campaign_id),
        weekly_change_pct=weekly_change_pct_so_far(campaign_id),
        no_sale_click_threshold=int(payload.get("no_sale_click_threshold", 20)),
    )

    return {
        "campaign_id": campaign_id,
        "inputs": {
            "current_budget": current_budget,
            "target_acos": target_acos,
            "acos": acos,
            "budget_utilization": budget_utilization,
            "clicks": clicks,
            "orders": orders,
            "spend": spend,
        },
        "recommendation": recommendation,
    }


@app.post("/api/adjust-campaign-budget")
def api_adjust_campaign_budget(payload: Dict[str, Any]):
    campaign_id = str(payload.get("campaign_id", "")).strip()
    if not campaign_id:
        raise HTTPException(status_code=400, detail="Provide campaign_id")

    apply_live = bool(payload.get("apply_live", False))

    recommendation_response = api_recommend_budget_adjustment(payload)
    recommendation = recommendation_response["recommendation"]
    current_budget = float(recommendation_response["inputs"]["current_budget"])
    new_budget = float(recommendation["recommended_budget"])
    step_pct = float(recommendation["step_pct"])

    applied = False
    api_response = None

    if apply_live and recommendation["action"] != "hold":
        client = AmazonAdsClient()
        body = [{
            "campaignId": campaign_id,
            "dailyBudget": round(new_budget, 2),
        }]
        api_response = client.post(ENDPOINTS["campaigns"], body)
        applied = True

    event = {
        "event_type": "budget_adjustment",
        "adjusted_at_utc": now_iso_utc(),
        "campaign_id": campaign_id,
        "old_budget": round(current_budget, 2),
        "new_budget": round(new_budget, 2),
        "step_pct": step_pct,
        "action": recommendation["action"],
        "reason": recommendation["reason"],
        "applied_live": applied,
    }
    append_jsonl(LAUNCH_LOG_PATH, event)

    return {
        "campaign_id": campaign_id,
        "recommendation": recommendation,
        "applied_live": applied,
        "budget_change": {
            "old_budget": round(current_budget, 2),
            "new_budget": round(new_budget, 2),
            "step_pct": step_pct,
        },
        "api_response": api_response,
    }


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
    # keep this safe by default
    verify_internal_token(authorization)

    apply_negatives_live = payload.get("apply_negatives_live", False)
    apply_winners_live = payload.get("apply_winners_live", False)
    winner_bid = float(payload.get("winner_bid", 0.9))

    min_clicks_for_negative = int(payload.get("min_clicks_for_negative", 20))
    min_orders_for_winner = int(payload.get("min_orders_for_winner", 2))
    max_acos_for_winner = float(payload.get("max_acos_for_winner", 0.35))
    min_clicks_for_winner = int(payload.get("min_clicks_for_winner", 8))
    campaign_filter = str(payload.get("campaign_id", "")).strip()

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

        if campaign_filter:
            summary["winners"] = [r for r in summary["winners"] if str(r.get("campaign_id", "")) == campaign_filter]
            summary["negatives"] = [r for r in summary["negatives"] if str(r.get("campaign_id", "")) == campaign_filter]
            summary["hold"] = [r for r in summary["hold"] if str(r.get("campaign_id", "")) == campaign_filter]

        live_actions = {"negative_terms_added": [], "winner_terms_promoted": []}

        if apply_negatives_live:
            grouped_negatives: Dict[tuple, List[str]] = {}
            for item in summary["negatives"]:
                key = (item["campaign_id"], item["ad_group_id"] or None)
                grouped_negatives.setdefault(key, []).append(item["term"])

            for (campaign_id, ad_group_id), terms in grouped_negatives.items():
                if not campaign_id:
                    continue
                rows_to_add = negative_keyword_rows(unique_in_order(terms), campaign_id, ad_group_id or None)
                client.post(ENDPOINTS["negative_keywords"], rows_to_add)
                live_actions["negative_terms_added"].append({
                    "campaign_id": campaign_id,
                    "ad_group_id": ad_group_id,
                    "terms": unique_in_order(terms),
                })

        if apply_winners_live:
            grouped_winners: Dict[str, List[str]] = {}
            for item in summary["winners"]:
                if not item["ad_group_id"]:
                    continue
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

        result = {
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
                "campaign_filter": campaign_filter,
            },
        }

        append_jsonl(OPTIMIZER_LOG_PATH, {
            "event_type": "optimizer_run",
            "ran_at_utc": now_iso_utc(),
            **result,
        })

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/quick-optimize-safe-negatives")
def api_quick_optimize_safe_negatives(payload: Dict[str, Any]):
    # Optional token passthrough for environments where DAILY_OPTIMIZER_TOKEN is set.
    token = str(payload.get("optimizer_token", "")).strip()
    authorization = f"Bearer {token}" if token else None

    optimizer_payload = {
        "apply_negatives_live": True,
        "apply_winners_live": False,
        "winner_bid": float(payload.get("winner_bid", 0.9)),
        "min_clicks_for_negative": int(payload.get("min_clicks_for_negative", 20)),
        "min_orders_for_winner": int(payload.get("min_orders_for_winner", 2)),
        "max_acos_for_winner": float(payload.get("max_acos_for_winner", 0.35)),
        "min_clicks_for_winner": int(payload.get("min_clicks_for_winner", 8)),
    }

    if payload.get("start_date"):
        optimizer_payload["start_date"] = payload.get("start_date")
    if payload.get("end_date"):
        optimizer_payload["end_date"] = payload.get("end_date")

    result = api_run_optimizer(optimizer_payload, authorization=authorization)
    return {
        "message": "Quick optimizer completed with safe negatives enabled",
        "result": result,
    }
