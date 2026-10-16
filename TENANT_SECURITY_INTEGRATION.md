# Security-first tenant foundation (not deployed)

This change introduces only a passive, validated per-account identity model.
It does NOT enable multi-tenant execution, modify scheduler settings, create
campaigns, or bypass the existing `profit_guard.Guard`.

## Mandatory security gate
- A prior review found what appear to be Amazon Ads OAuth credentials checked
  into `Amazon-PPC-Automation-System-SAAS/setup_checklist.md`. The owner must
  **revoke/rotate** the exposed client secret and refresh token through Amazon
  and update the corresponding cloud secrets. Branch changes cannot revoke tokens.
- Audit repository history and remove leaked credentials using an authorized
  coordinated history-rewrite procedure. A current-file deletion is insufficient.
- Verify all Cloud Run task endpoints require authenticated service identity.
- Review any historic `key.json` exposure. The Campaign tracked blob removed by
  this branch is empty, but history and cloud key status still require review.

## Safe future integration
1. Make tenant identity an authenticated server-side derivation, never trust a
   caller-supplied tenant ID or Ads profile identifier.
2. Store per-tenant secrets in Secret Manager and never log them.
3. Isolate reports, BigQuery datasets, GCS locks, profit policies, reserves,
   product costs and token refresh state by validated tenant identity.
4. Confirm the Amazon Ads profile matches the authenticated tenant **before**
   all reads and writes.
5. Route *all* writes through the production profit guard, preserving account
   cap, inventory, margin and stale-data fail-closed logic.
6. Add integration tests for hostile cross-tenant attempts and concurrent locks
   before enabling any tenant-aware scheduler.

Run offline regression checks:
`python -m unittest -v test_tenant_context.py`

This PR is intentionally a foundation only; no deployment or merge is authorized.
