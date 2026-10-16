"""Per-tenant Secret Manager adapter; secret values are never logged.

Only server-controlled TenantBindings can select secret resources. Credentials
are returned in-memory for a single explicit worker invocation.
"""
from __future__ import annotations
import json
from tenant_auth import TenantBinding
from tenant_context import TenantConfigurationError

_REQUIRED = frozenset({"client_id", "client_secret", "refresh_token", "ads_profile_id"})

def load_tenant_ads_credentials(binding: TenantBinding, *, client):
    if not isinstance(binding, TenantBinding):
        raise TenantConfigurationError("Server-resolved tenant binding required")
    response = client.access_secret_version(request={"name": binding.secret_resource})
    values = json.loads(response.payload.data.decode("utf-8"))
    if not isinstance(values, dict) or not _REQUIRED.issubset(values):
        raise TenantConfigurationError("Incomplete tenant credentials")
    if any(not isinstance(values[k], str) or not values[k].strip() for k in _REQUIRED):
        raise TenantConfigurationError("Invalid tenant credentials")
    binding.context.assert_profile(values["ads_profile_id"])
    return {key: values[key] for key in _REQUIRED}
