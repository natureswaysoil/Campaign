"""Passive per-account configuration; never authorizes Amazon writes.

An explicit environment-derived tenant context can be attached to future
services. Existing single-account operations are intentionally unchanged.
Every live mutation must still pass Campaign's profit_guard.Guard.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_REGIONS = frozenset({"NA", "EU", "FE"})

class TenantConfigurationError(ValueError):
    """Missing or invalid tenant isolation configuration."""

@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    ads_profile_id: str
    region: str = "NA"
    dataset_id: str | None = None

    def __post_init__(self):
        if not _ID.fullmatch(self.tenant_id):
            raise TenantConfigurationError("Invalid tenant identifier")
        if not self.ads_profile_id.isdecimal() or len(self.ads_profile_id) > 32:
            raise TenantConfigurationError("Invalid Amazon Ads profile identifier")
        if self.region not in _REGIONS:
            raise TenantConfigurationError("Unsupported Amazon region")
        if self.dataset_id is not None and not _ID.fullmatch(self.dataset_id):
            raise TenantConfigurationError("Invalid dataset identifier")

    def safe_log_context(self) -> dict[str, str]:
        return {"tenant_id": self.tenant_id, "region": self.region}

    def assert_profile(self, actual_profile_id: str) -> None:
        if str(actual_profile_id) != self.ads_profile_id:
            raise TenantConfigurationError("Cross-tenant Ads profile mismatch")

def tenant_from_env(environ=None) -> TenantContext:
    env = os.environ if environ is None else environ
    tenant_id = env.get("PPC_TENANT_ID")
    profile_id = env.get("AMAZON_ADS_PROFILE_ID")
    if not tenant_id or not profile_id:
        raise TenantConfigurationError("Tenant and profile identifiers are required")
    return TenantContext(
        tenant_id=tenant_id,
        ads_profile_id=profile_id,
        region=env.get("AMAZON_ADS_REGION", "NA").upper(),
        dataset_id=env.get("PPC_BQ_DATASET") or None,
    )
