"""Shared, strict controls for production advertising mutations."""
from typing import Any, Dict
from fastapi import HTTPException

BOOLEAN_FLAGS = {"apply_live", "dry_run", "allow_campaign_launch", "force_relaunch",
                 "reset_baseline", "apply_negatives_live", "apply_winners_live"}


def validate_flags(payload: Dict[str, Any]) -> None:
    for name in BOOLEAN_FLAGS:
        if name in payload and type(payload[name]) is not bool:
            raise HTTPException(status_code=422, detail=f"{name} must be a JSON boolean")


def live_requested(payload: Dict[str, Any]) -> bool:
    validate_flags(payload)
    return payload.get("apply_live") is True and payload.get("dry_run") is not True
