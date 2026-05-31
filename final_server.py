"""Final Cloud Run entrypoint.

Imports the extended server and adds the missing campaign pause/resume endpoint
used by the dashboard Pause and Resume buttons.
"""
from typing import Any, Dict, Optional

from fastapi import Body, Header
from fastapi.responses import JSONResponse

import extended_server  # noqa: F401 - registers routes and dashboard patch
from extended_server import app
from optimize_campaigns import AmazonAdsClient, verify_internal_token


@app.post("/api/update-campaign-state")
def api_update_campaign_state(
    payload: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    x_daily_optimizer_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Pause or resume one Sponsored Products campaign from the dashboard."""
    verify_internal_token(authorization, x_daily_optimizer_token)
    try:
        campaign_id = str(payload.get("campaign_id") or payload.get("campaignId") or "").strip()
        state = str(payload.get("state") or "").strip().upper()

        if not campaign_id:
            return JSONResponse({"error": True, "message": "campaign_id required"}, status_code=400)
        if state not in {"ENABLED", "PAUSED"}:
            return JSONResponse({"error": True, "message": "state must be ENABLED or PAUSED"}, status_code=400)

        client = AmazonAdsClient()
        result = client.put(
            "/sp/campaigns",
            {"campaigns": [{"campaignId": campaign_id, "state": state}]},
            content_type="application/vnd.spcampaign.v3+json",
            accept="application/vnd.spcampaign.v3+json",
        )
        return JSONResponse({
            "success": True,
            "campaign_id": campaign_id,
            "state": state,
            "amazon_response": result,
        })
    except Exception as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
