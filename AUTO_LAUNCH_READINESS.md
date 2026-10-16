# Automatic launch readiness

The scheduler hook now invokes `ProductionPipeline`, which connects selection,
paused campaign creation, provisioning, independent readback and guarded activation.
It remains off unless `AUTO_LAUNCH_ENABLED=true` and an authenticated request supplies
`apply_live: true`. No launch scheduler is created by this patch.

Implemented and covered by offline integration tests:
- Profile-scoped launch lock, unique persisted launch name and seller SKU.
- Preflight before creation using stored economics, inventory, current spend and
  aggregate enabled/reserved budgets.
- Durable write-ahead markers for campaign, group, product ad, target and activation.
- Restart reconciliation by exact launch name and canonical entity IDs. Unknown
  absent or duplicate objects fail closed; POSTs are never replayed on absence.
- Final profit guard on every live mutation, including activation.
- Kill switch tested without factory, credentials or network access.

Still required before deployment/enablement approval:
1. Review this patch and its GitHub CI results.
2. Validate Amazon API payload/readback shapes in a controlled account and GCS
   persistence/locking against the deployment identity. Offline adapters and the
   GCS contract double are not proof of live service behavior.
3. Verify current product costs, exact SKU/ASIN, policy, account timezone, account
   budget reservations and approved per-campaign exploration allowance.
4. Separately approve deployment and scheduler enablement. Do not create a launch
   schedule until the controlled checks pass.

Historical launches lacking a durable launch key are not adopted automatically.
An existing campaign without active product ads blocks new automatic selection until reviewed. Inspect these separately; campaign-title matches are not recovery authorization.
