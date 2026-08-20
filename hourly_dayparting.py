"""Data-driven Amazon Ads dayparting from hourly conversion performance."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable

LOOKBACK_DAYS = int(os.getenv("HOURLY_LOOKBACK_DAYS", "28"))
MIN_CLICKS_PER_HOUR = int(os.getenv("HOURLY_MIN_CLICKS", "20"))
MIN_TOTAL_ORDERS = int(os.getenv("HOURLY_MIN_ORDERS", "4"))
PRIME_HOUR_COUNT = int(os.getenv("HOURLY_PRIME_HOURS", "6"))
_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}


def fixed_fallback(start: int, end: int) -> Dict[str, Any]:
    return {
        "prime_hours": list(range(start, end + 1)),
        "source": "fixed_fallback",
        "lookback_days": None,
        "clicks": 0,
        "orders": 0,
        "reason": "Hourly Amazon conversion history is not ready.",
    }


def derive_schedule(rows: Iterable[Dict[str, Any]], fallback_start: int, fallback_end: int) -> Dict[str, Any]:
    candidates = []
    total_clicks = 0
    total_orders = 0
    for row in rows:
        hour = int(row.get("hour_eastern", row.get("hour", -1)))
        if not 0 <= hour <= 23:
            continue
        clicks = int(row.get("clicks") or 0)
        orders = int(row.get("orders", row.get("purchases", 0)) or 0)
        spend = float(row.get("spend", row.get("cost", 0)) or 0)
        sales = float(row.get("sales") or 0)
        total_clicks += clicks
        total_orders += orders
        if clicks < MIN_CLICKS_PER_HOUR:
            continue
        conversion_rate = orders / clicks if clicks else 0.0
        roas = sales / spend if spend else 0.0
        score = conversion_rate * 0.75 + min(roas, 8.0) / 8.0 * 0.25
        candidates.append((score, orders, clicks, hour))

    if total_clicks < MIN_CLICKS_PER_HOUR * 4 or total_orders < MIN_TOTAL_ORDERS:
        return fixed_fallback(fallback_start, fallback_end)
    winners = sorted(candidates, reverse=True)[: max(1, min(PRIME_HOUR_COUNT, 12))]
    prime_hours = sorted(item[3] for item in winners if item[1] > 0)
    if not prime_hours:
        return fixed_fallback(fallback_start, fallback_end)
    return {
        "prime_hours": prime_hours,
        "source": "amazon_hourly_conversion",
        "lookback_days": LOOKBACK_DAYS,
        "clicks": total_clicks,
        "orders": total_orders,
        "reason": "Selected from hourly conversion rate and ROAS.",
    }


def load_schedule(fallback_start: int, fallback_end: int) -> Dict[str, Any]:
    if _CACHE["value"] is not None and time.time() < _CACHE["expires"]:
        return _CACHE["value"]
    schedule = fixed_fallback(fallback_start, fallback_end)
    try:
        from google.cloud import bigquery
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "amazon-ppc-bid-optimizer")
        table = os.getenv("AMAZON_HOURLY_PERFORMANCE_TABLE", f"{project}.amazon_ppc.sp_hourly_performance")
        query = f"""
            SELECT hour_eastern, SUM(clicks) clicks, SUM(purchases) orders,
                   SUM(cost) spend, SUM(sales) sales
            FROM `{table}`
            WHERE event_date >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL {LOOKBACK_DAYS} DAY)
            GROUP BY hour_eastern
        """
        rows = [dict(row.items()) for row in bigquery.Client(project=project).query(query).result()]
        schedule = derive_schedule(rows, fallback_start, fallback_end)
    except Exception:
        pass
    _CACHE.update({"expires": time.time() + 900, "value": schedule})
    return schedule