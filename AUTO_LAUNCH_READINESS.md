# Automatic launch readiness

Implemented: scheduler-to-pipeline wiring, shared preflight before creation, guarded writes, durable launch identity/checkpoints, recovery by canonical IDs, independently verified provisioning and activation, and a disabled-by-default server opt-in.

Validation completed on October 16, 2026:
- Offline regression tests cover insufficient mature evidence, safe reductions, isolated outcomes, lost acknowledgements, absent/ambiguous readbacks and restart recovery.
- Real Amazon authorization, exact product lookup, sampled list response shapes and economic preflight passed.
- Real GCS persistence and cross-process locking passed under the runtime identity.
- A bounded future-dated campaign was created, provisioned, enabled, independently read back, and paused; a separate cleanup step confirmed PAUSED.
- The live test identified and corrected the AUTO-target enum/creation bug. Amazon's four automatically created target IDs are now reconciled, not re-created.

See LIVE_VALIDATION_20261016.md for scope and evidence.

Before production rollout:
1. Review the final patch and its GitHub CI results.
2. Resolve legacy empty campaigns and unverifiable economics before unattended selection; such campaigns continue to block automatic selection.
3. Recheck current saved costs, exact SKU/ASIN, inventory, conversion/exploration evidence, spend freshness, account timezone and aggregate budget reservations.
4. Separately approve merging, deployment and scheduler enablement. The controlled test did not authorize or perform those steps.

The lifecycle test used an explicitly selected product and a future start date. It validates API creation/activation, not ad delivery, conversion performance or every account campaign. Live fault injection and an unattended scheduler run were not performed.
