"""Deterministic orchestration for the Amazon PPC specialist tools."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

from fastapi.responses import JSONResponse

Tool = Callable[[Dict[str, Any]], JSONResponse]


def response_payload(response: JSONResponse) -> Dict[str, Any]:
    body = json.loads(response.body.decode("utf-8"))
    return {"status_code": response.status_code, **body}


class AmazonPpcAgent:
    """Run approved PPC tools in a fixed, auditable order."""

    def __init__(self, tools: Dict[str, Tool]) -> None:
        self.tools = tools

    def run(self, request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = dict(request or {})
        apply_live = bool(config.get("apply_live", False))
        requested: Iterable[str] = config.get("actions") or (
            "refresh_dashboard", "audit_acos", "retune_bids"
        )
        allowed = {"refresh_dashboard", "audit_acos", "retune_bids", "harvest_keywords", "launch_campaign"}
        requested_actions = [str(item) for item in requested if str(item) in allowed]
        priority = ("retune_bids", "refresh_dashboard", "audit_acos", "harvest_keywords", "launch_campaign")
        actions = [name for name in priority if name in requested_actions]
        results: Dict[str, Any] = {}

        for action in actions:
            if action == "launch_campaign":
                product_id = str(config.get("product_id") or "").strip()
                if not product_id:
                    results[action] = {"status_code": 400, "error": True, "message": "product_id required"}
                    continue
                if apply_live and not bool(config.get("allow_campaign_launch", False)):
                    results[action] = {"status_code": 409, "error": True, "message": "Live launch requires allow_campaign_launch=true"}
                    continue
                payload = {"product_id": product_id, "apply_live": apply_live, "force_relaunch": bool(config.get("force_relaunch", False))}
            elif action == "harvest_keywords":
                payload = {
                    "apply_live": apply_live,
                    "lookback_days": int(config.get("lookback_days", 14)),
                    "max_terms_per_product": int(config.get("max_terms_per_product", 10)),
                    "max_products": int(config.get("max_products", 25)),
                }
            elif action == "retune_bids":
                payload = {"apply_live": apply_live, "max_results": int(config.get("max_results", 100))}
            elif action == "audit_acos":
                payload = {"apply_live": False, "min_spend": float(config.get("min_spend", 20.0)), "acos_ceiling": float(config.get("acos_ceiling", 0.38))}
            else:
                payload = {}

            result = response_payload(self.tools[action](payload))
            results[action] = result
            if result["status_code"] >= 500:
                break

        failures = [name for name, result in results.items() if result.get("status_code", 500) >= 400]
        return {
            "success": not failures,
            "agent": "amazon-ppc-optimizer",
            "mode": "LIVE" if apply_live else "DRY_RUN",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "actions_requested": actions,
            "actions_completed": list(results),
            "failures": failures,
            "results": results,
        }