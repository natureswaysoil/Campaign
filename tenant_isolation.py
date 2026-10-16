"""Safe, opt-in tenant-scoped storage key factory.

Never derive authorization from a request's tenant_id. The caller must have
already resolved the authenticated principal to a TenantContext.
"""
from dataclasses import dataclass
from tenant_context import TenantContext, TenantConfigurationError

_KINDS = frozenset({"state", "lock", "reports", "profit", "tokens"})
@dataclass(frozen=True)
class TenantNamespace:
    context: TenantContext

    def key(self, kind: str, name: str) -> str:
        if kind not in _KINDS:
            raise TenantConfigurationError("Unknown storage namespace")
        # Reject path traversal and nested arbitrary names. The base repo uses
        # fixed logical names for its state objects.
        if not name or len(name) > 96 or not all(c.isalnum() or c in "-_" for c in name):
            raise TenantConfigurationError("Invalid object name")
        return f"tenants/{self.context.tenant_id}/profiles/{self.context.ads_profile_id}/{kind}/{name}.json"

    def assert_profile(self, profile_id: str) -> None:
        self.context.assert_profile(profile_id)
