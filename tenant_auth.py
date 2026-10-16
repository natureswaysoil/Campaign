"""Server-side tenant identity resolution for isolated future worker calls.

Principals are authenticated by the upstream identity gateway. Caller-provided
tenant/profile parameters are never authoritative. Bindings originate only
from a server-controlled registry. Do not expose this as an HTTP route.
"""
from __future__ import annotations
from dataclasses import dataclass
from tenant_context import TenantContext, TenantConfigurationError

@dataclass(frozen=True)
class TenantBinding:
    principal: str
    context: TenantContext
    secret_resource: str

class TenantRegistry:
    def __init__(self, bindings):
        self._bindings = {}
        profiles = set()
        for binding in bindings:
            if not isinstance(binding, TenantBinding) or not binding.principal:
                raise TenantConfigurationError("Invalid tenant binding")
            if binding.principal in self._bindings:
                raise TenantConfigurationError("Duplicate principal")
            key = (binding.context.region, binding.context.ads_profile_id)
            if key in profiles:
                raise TenantConfigurationError("Duplicate Ads profile binding")
            if not binding.secret_resource.startswith("projects/") or "/secrets/" not in binding.secret_resource or not binding.secret_resource.endswith("/versions/latest"):
                raise TenantConfigurationError("Invalid secret resource")
            self._bindings[binding.principal] = binding
            profiles.add(key)

    def resolve(self, verified_principal, requested_profile=None):
        if not verified_principal:
            raise TenantConfigurationError("Authenticated principal required")
        binding = self._bindings.get(verified_principal)
        if binding is None:
            raise TenantConfigurationError("Unrecognized principal")
        if requested_profile is not None:
            binding.context.assert_profile(requested_profile)
        return binding
