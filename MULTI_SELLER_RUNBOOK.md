# Multi-seller runtime prerequisites — not enabled

The server-side registry binds **verified** principals to tenant IDs, Ads
profile IDs, BigQuery datasets and fixed Secret Manager resources. The
credentials adapter rejects profile mismatch. It intentionally has no HTTP
route, no scheduler and no production feature flag. Authentication must be
validated at the ingress by trusted workload identity / IAM; a string header
by itself is never proof of principal identity.

Existing `GCSState` continues to use legacy profile-based keys and locks.
Do not introduce tenant execution via it. Migration requires:
1. Stop the old scheduler and freeze writes for the affected profile.
2. Inventory current generation IDs for state, profit, reports, token caches,
   reservations, locks and archives; resolve any outstanding lock manually.
3. Verify owner and profile against the approved server-side binding.
4. Copy versioned objects into the dedicated tenant prefix under a **new**
   IAM-restricted bucket using generation preconditions, preserving originals.
5. Reconcile hashes, generation and ledger/reservations; keep the old writer
   paused until new code operates solely on tenant keys.
6. Exercise crash recovery, concurrent tenants, incorrect profile, stale
   inventory, credential revocation and denied IAM in staging.
7. Only then explicitly enable a single tenant through controlled rollout.

Token material must not be copied to state objects: store access only in
tenant-specific Secret Manager entries. Never log secrets or tokens.
All ad mutations must continue through Campaign's profit guard.
This PR does not deploy, migrate data, or enable any additional seller.
