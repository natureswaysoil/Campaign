"""Cloud Run entrypoint with live dayparting bid controls.

Imports server.py first so all dashboard, campaign-launch, search-term, and optimizer
routes are registered, then adds the live bid-retune route that server.py expected.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, Header
from fastapi.responses import JSONResponse

from server import app  # noqa: F401 - importing server registers the main app/routes
from optimize_campaigns import AmazonAdsClient, verify_internal_token
from budget_dayparting import budget_protection_status, get_budget_protection_mode


BASELINE_BIDS_FILE = Path(os.getenv("DAYPARTING_BASELINE_BIDS_FILE", "/tmp/dayparting_baseline_bids.json"))
MIN_BID = float(os.getenv("MIN_DAYPART_BID", "0.10"))
MAX_BID = float(os.getenv("MAX_DAYPART_BID", "2.50"))
PROTECT_MULTIPLIER = float(os.getenv("PROTECT_BID_MULTIPLIER", "0.35"))
TAPER_MULTIPLIER = float(os.getenv("TAPER_BID_MULTIPLIER", "0.45"))
PRIME_MULTIPLIER = float(os.getenv("PRIME_BID_MULTIPLIER", "1.15"))


def _clamp_bid(value: float) -> float:
    return round(max(MIN_BID, min(MAX_BID, float(value))), 2)


def _load_baseline_bids() -> Dict[str, float]:
    try:
        if BASELINE_BIDS_FILE.exists():
            data = json.loads(BASELINE_BIDS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items() if v not in (None, "")}
    except Exception:
        pass
    return {}


def _save_baseline_bids(data: Dict[str, float]) -> None:
    try:
        BASELINE_BIDS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        # Non-fatal: Cloud Run /tmp is best-effort. The route still returns its preview.
        pass


def _target_bid_from_baseline(base_bid: float, mode: str) -> float:
    if mode == "PROTECT":
        return _clamp_bid(base_bid * PROTECT_MULTIPLIER)
    if mode == "TAPER":
        return _clamp_bid(base_bid * TAPER_MULTIPLIER)
    return _clamp_bid(base_bid * PRIME_MULTIPLIER)


@app.post("/api/retune-existing-bids")
def api_retune_existing_bids(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Apply dayparting to active ad group default bids.

    - Before 10am ET: sets ad group bids to about 35% of their saved baseline.
    - 10am-8:59pm ET: restores/raises to prime-time pressure.
    - After 9pm ET: tapers to about 45% of baseline.

    The first live run saves the current ad group bids as the baseline so repeated
    scheduler runs do not keep compounding bids lower and lower.
    """
    verify_internal_token(authorization, x_daily_optimizer_token)
    apply_live = bool(payload.get("apply_live", True))
    reset_baseline = bool(payload.get("reset_baseline", False))
    max_results = int(payload.get("max_results", 100))
    max_results = max(1, min(max_results, 100))

    try:
        client = AmazonAdsClient()
        status = budget_protection_status()
        mode = get_budget_protection_mode()

        response = client.post(
            "/sp/adGroups/list",
            {"maxResults": max_results, "filters": {"stateFilter": {"include": ["ENABLED"]}}},
            content_type="application/vnd.spadgroup.v3+json",
            accept="application/vnd.spadgroup.v3+json",
        )
        ad_groups = response.get("adGroups", []) if isinstance(response, dict) else []
        baseline = {} if reset_baseline else _load_baseline_bids()

        preview = []
        updates = []
        for ad_group in ad_groups:
            ad_group_id = str(ad_group.get("adGroupId") or "")
            if not ad_group_id:
                continue
            current_bid = float(ad_group.get("defaultBid") or payload.get("fallback_bid", 0.75))
            if reset_baseline or ad_group_id not in baseline:
                baseline[ad_group_id] = current_bid
            base_bid = float(baseline.get(ad_group_id) or current_bid)
            new_bid = _target_bid_from_baseline(base_bid, mode)
            campaign_id = str(ad_group.get("campaignId") or "")
            row = {
                "adGroupId": ad_group_id,
                "campaignId": campaign_id,
                "currentBid": round(current_bid, 2),
                "baselineBid": round(base_bid, 2),
                "newBid": new_bid,
                "mode": mode,
            }
            preview.append(row)
            if abs(new_bid - current_bid) >= 0.01:
                update_row = {"adGroupId": ad_group_id, "defaultBid": new_bid}
                if campaign_id:
                    update_row["campaignId"] = campaign_id
                updates.append(update_row)

        _save_baseline_bids(baseline)

        api_response: Dict[str, Any] = {}
        if apply_live and updates:
            api_response = client.put(
                "/sp/adGroups",
                {"adGroups": updates},
                content_type="application/vnd.spadgroup.v3+json",
                accept="application/vnd.spadgroup.v3+json",
            )

        return JSONResponse({
            "success": True,
            "dry_run": not apply_live,
            "mode": mode,
            "budget_protection": status,
            "ad_groups_seen": len(ad_groups),
            "updates_needed": len(updates),
            "updates_applied": len(updates) if apply_live else 0,
            "baseline_count": len(baseline),
            "reset_baseline": reset_baseline,
            "preview": preview[:25],
            "amazon_response": api_response,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
