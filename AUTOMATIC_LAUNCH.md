# Automated campaign launch — safe rollout

Auto launch candidate selection is implemented in `auto_launch_policy.py`.
It filters existing ASINs, verifies explicit per-product price, COGS,
shipping, fees and available inventory, and respects minimum profit reserve.
The default launch allowance is at most one candidate per pass.

**No recurring launcher is enabled or deployed.** Existing live launch entry
points and `profit_guard.Guard` must remain the final mandatory authority.
A candidate is *not* authorization to spend.

Before enabling hands-off launch:
1. Resolve candidate products from the verified seller catalog, including
   the exact ASIN and seller SKU. Reject stale or incomplete observations.
2. Acquire an account-level durable launch lock and record the ASIN intent
   before any Amazon create request; retry by reconciling Amazon results.
3. Detect existing product ads by ASIN, not merely matching campaign titles.
4. Create paused and verify each campaign/ad group/product-ad/target response.
5. Activate only after economic cap, inventory, account daily envelope and
   current spend checks pass through the production guard.
6. Cap account-wide daily spend (owner desired $75 must be entered/verified
   in the Profit dashboard); never infer from this note.
7. Wire a Cloud Scheduler job only after staging tests and an explicit
   deployment approval. Require per-account opt-in and a kill switch.
8. Record durable launch outcomes and alert on incomplete or failed creates.

Until those gates are implemented, do not call the legacy live launch endpoint
automatically. This branch changes neither scheduler nor production campaigns.

## Production adapter progress (PR #46)
- `auto_launch_production.py` uses the existing guarded Ads client and confirms writes by readback.
- `auto_launch_guard.py` validates stored policy, observed account spend, refreshed economics, and inventory velocity.
- Coordinated creation is still off by default, and account scheduler is **not configured**. An incomplete ad-group/product-ad/target provisioning sequence must be implemented before any automated activation.
- The reservation coordinator retains unresolved outcomes and blocks duplicate retry. Operator reconciliation is still required after ambiguity.
- Tests in CI are mocks, not a live Amazon staging verification.
