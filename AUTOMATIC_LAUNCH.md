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

Do not call the legacy live launch endpoint
automatically. This branch changes neither scheduler nor production campaigns.

## Complete pipeline (review only)
`auto_launch_tick.py` now supplies the production adapters to `run_pipeline`.
The automatic pipeline creates one AUTO campaign per eligible ASIN. The existing
manual AUTO + EXACT endpoint uses shared preflight but remains a separate workflow;
do not schedule that endpoint for unattended launches.

All creates are checkpointed before sending. A restarted pipeline reconciles exact
launch names and canonical entity IDs, then resumes only verified stages. Missing
or ambiguous readbacks after a POST never trigger a replacement POST. An uncertain
activation is resolved only when the enabled campaign and provisioned entities are
independently observed; otherwise it remains held for review.

No deployment, scheduler configuration, policy change or campaign mutation is part
of this patch. See `AUTO_LAUNCH_READINESS.md` for the remaining live validation.
