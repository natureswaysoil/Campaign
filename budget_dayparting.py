"""Budget-protecting dayparting for Nature's Way Soil Amazon PPC.

Purpose:
- Spend less before the strongest buying window so daily budgets are not burned
  too early.
- Reserve roughly 65% of daily ad budget for prime time by keeping pre-prime bid
  pressure near 35% of normal.
- Use Amazon suggested bid ranges, but do not blindly take the high end all day.
- Keep exact/phrase/broad bids inside a safe floor/ceiling.

Default Eastern schedule:
- PROTECT: 12:00am-9:59am   -> very conservative bids, about 35% pressure
- PRIME:   10:00am-8:59pm   -> strongest bids
- TAPER:   9:00pm-11:59pm   -> conservative bids again

Environment overrides:
- PRIME_TIME_START=10
- PRIME_TIME_END=20
- PROTECT_BID_MULTIPLIER=0.35
- TAPER_BID_MULTIPLIER=0.45
- PRIME_BID_POSITION=0.85
- NORMAL_BID_POSITION=0.55
"""
from __future__ import annotations

import datetime
import os
from typing import Any, Dict, Optional, Tuple

from hourly_dayparting import load_schedule


PRIME_TIME_START = int(os.getenv("PRIME_TIME_START", "10"))
PRIME_TIME_END = int(os.getenv("PRIME_TIME_END", "20"))

# To reserve about 65% of the daily budget for prime time, the pre-prime window
# should not run at normal bids. This does not literally create a second Amazon
# budget bucket; it protects the budget by reducing bid pressure before prime.
PROTECT_BID_MULTIPLIER = float(os.getenv("PROTECT_BID_MULTIPLIER", "0.35"))
TAPER_BID_MULTIPLIER = float(os.getenv("TAPER_BID_MULTIPLIER", "0.45"))

PRIME_BID_POSITION = float(os.getenv("PRIME_BID_POSITION", "0.85"))
NORMAL_BID_POSITION = float(os.getenv("NORMAL_BID_POSITION", "0.55"))
MIN_BID = float(os.getenv("MIN_DAYPART_BID", "0.10"))
MAX_BID = float(os.getenv("MAX_DAYPART_BID", "2.50"))


def _clamp(value: float, low: float = MIN_BID, high: float = MAX_BID) -> float:
    return max(low, min(high, value))


def current_hour_eastern() -> int:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = datetime.timezone(datetime.timedelta(hours=-5))
    return datetime.datetime.now(tz).hour


def get_budget_protection_mode(hour: Optional[int] = None) -> str:
    h = current_hour_eastern() if hour is None else int(hour)
    prime_hours = load_schedule(PRIME_TIME_START, PRIME_TIME_END)["prime_hours"]
    if h in prime_hours:
        return "PRIME"
    if h < min(prime_hours):
        return "PROTECT"
    return "TAPER"


def budget_protection_status(hour: Optional[int] = None) -> Dict[str, Any]:
    h = current_hour_eastern() if hour is None else int(hour)
    schedule = load_schedule(PRIME_TIME_START, PRIME_TIME_END)
    mode = get_budget_protection_mode(h)
    if mode == "PRIME":
        note = "Prime buying window: bids can compete harder."
        multiplier = 1.0
        reserve_target = 0.65
    elif mode == "PROTECT":
        note = "Budget protection: pre-prime bids are held near 35% to preserve about 65% of budget for prime time."
        multiplier = PROTECT_BID_MULTIPLIER
        reserve_target = 0.65
    else:
        note = "Evening taper: bids are reduced after prime time."
        multiplier = TAPER_BID_MULTIPLIER
        reserve_target = 0.0
    return {
        "bid_mode": mode,
        "hour_eastern": h,
        "prime_time_label": "Prime hours ET: " + ", ".join(f"{hour}:00" for hour in schedule["prime_hours"]),
        "schedule_source": schedule["source"],
        "schedule_lookback_days": schedule["lookback_days"],
        "schedule_clicks": schedule["clicks"],
        "schedule_orders": schedule["orders"],
        "budget_protection_multiplier": multiplier,
        "prime_budget_reserve_target": reserve_target,
        "note": note,
    }


def choose_budget_protected_bid(rec: Dict[str, float], fallback: float) -> Tuple[float, float, float]:
    """Return Amazon low/high plus protected applied bid.

    During PROTECT/TAPER, use the low end of Amazon's range and multiply it down.
    During PRIME, use most of the range but not necessarily the absolute high end.
    """
    mode = get_budget_protection_mode()
    low = float(rec.get("low") or 0.0)
    high = float(rec.get("high") or 0.0)
    fallback = float(fallback or 0.75)

    if low > 0 and high > 0:
        if mode == "PRIME":
            applied = low + ((high - low) * PRIME_BID_POSITION)
        elif mode == "PROTECT":
            applied = low * PROTECT_BID_MULTIPLIER
        else:
            applied = low * TAPER_BID_MULTIPLIER
        return round(low, 2), round(high, 2), round(_clamp(applied), 2)

    if mode == "PRIME":
        applied = fallback * 1.15
    elif mode == "PROTECT":
        applied = fallback * PROTECT_BID_MULTIPLIER
    else:
        applied = fallback * TAPER_BID_MULTIPLIER
    return round(low, 2), round(high, 2), round(_clamp(applied), 2)


def choose_budget_protected_campaign_bid(
    low: float,
    high: float,
    mode: str,
    acos: Optional[float],
    clicks: int,
) -> float:
    """Campaign-level version used when estimated bid windows are available."""
    low = float(low or 0)
    high = float(high or 0)
    if low <= 0 or high <= 0:
        return 0.0

    protection_mode = get_budget_protection_mode()
    if protection_mode == "PROTECT":
        return round(_clamp(low * PROTECT_BID_MULTIPLIER), 2)
    if protection_mode == "TAPER":
        return round(_clamp(low * TAPER_BID_MULTIPLIER), 2)

    # Prime time: let good campaigns move closer to high, but weak campaigns stay restrained.
    if acos is None:
        position = NORMAL_BID_POSITION
    elif acos <= 0.25:
        position = 0.90
    elif acos <= 0.35:
        position = 0.80
    elif acos <= 0.50:
        position = 0.65
    else:
        position = 0.45
    confidence = max(0.0, min(float(clicks or 0) / 40.0, 1.0))
    position = NORMAL_BID_POSITION + ((position - NORMAL_BID_POSITION) * confidence)
    return round(_clamp(low + ((high - low) * position)), 2)
