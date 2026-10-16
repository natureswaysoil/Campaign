# Tenant BigQuery + isolation integration (offline, opt-in)

This PR implements reusable identity-bound dataset and object namespace
adapters. It **does not enable multi-seller automation** or alter existing
single-profile storage, schedulers, credentials, API routing, or PPC writes.

The adapter requires a validated `TenantContext` with an explicit dataset ID,
constructs validated BigQuery identifiers, uses a parameterized row limit,
and rejects cross-account profile references. `TenantNamespace` generates
isolated logical GCS keys for reports, profit, tokens, locks and state.

Run: `python -m unittest -v test_tenant_context.py test_tenant_isolation.py`.

**Security prerequisites before runtime wiring**
- Resolve tenant identity from the authenticated principal; never accept a
  supplied tenant identifier as authorization.
- Grant each runtime identity access only to that tenant's BigQuery dataset,
  GCS objects and individual secrets. Namespace separation is not IAM.
- Update all GCSState consumers and all report/sync/guard paths atomically,
  including locks, reservations, histories and token caches.
- Verify bound Amazon Ads profile and SP-API seller/marketplace authorization
  before reads or writes. Isolate all token refresh and connection states.
- Preserve `profit_guard.Guard` as the only write path and its fail-closed
  economic and account-spend gates.
- Test concurrent distinct-tenant requests, impersonation, stale data,
  mismatched sellers and recovery from interrupted writes.
- Do not use old `AUTOMATION_STATE_BUCKET` profile-based storage as a
  shortcut to a multi-account tenancy guarantee.
- Provision BigQuery tables and tenant IAM through approved infrastructure
  migration separately. This PR does not create cloud resources.

No deployment or merge is authorized in this PR.
