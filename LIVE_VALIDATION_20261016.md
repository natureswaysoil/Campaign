# Controlled live validation — October 16, 2026

The owner approved live validation and publication of these results in public PR #47. The latest controlled validation completed successfully after the owner granted runtime impersonation access and the IAM change propagated.

Successful run: https://github.com/natureswaysoil/Campaign/actions/runs/38979715218

| Check | Observed result |
|---|---|
| Production revision | campaign-optimizer-00330-pzd; automatic launching disabled |
| Amazon Ads authorization | Passed; configured USD profile and America/Los_Angeles reporting timezone match |
| Saved decision inputs | Read successfully: 16 products, 635 metric records, $75 daily policy limit |
| Current spend observation | 38.1 minutes old at the successful validation |
| Amazon object reads | 333 campaigns and 426 groups returned; product-ad, keyword and target read shapes sampled on 3 campaigns |
| Sampled bid preflight | 2 held samples and 1 unverifiable sample; expected holds remain distinct from execution failures. This sample does not certify account-wide bid eligibility. |
| Selling Partner lookup | Authorization and one exact product identity/fee/inventory lookup passed; refreshed values retained only in memory |
| Approved exploration preflight | Eligible at a $0.07 maximum bid and $2/day campaign budget; no campaign created |
| Real GCS cross-process persistence and lock | PASSED under the Cloud Run runtime identity: durable readback, contention from a separate process, release/reacquisition, and disposable-object cleanup |

Earlier validation attempts could not impersonate the runtime identity. The owner applied the scoped service-account permission. The first retry still saw an impersonation denial; the later successful run confirmed the access blocker was cleared. No additional IAM changes were made by the validation job.

The storage probe used a unique disposable `validation-live/<uuid>` namespace, never production workflow records. The validation adapters block Amazon advertising mutations. No merge, deployment, campaign creation/activation, budget/policy change, or scheduler enablement occurred.

Production safety CI passed 268 tests; local offline validation passed 295 tests plus 3 subtests. This live run validates Amazon read access, sampled response shapes, preflight against real observations, and real GCS behavior. It does not validate live campaign creation or activation and does not by itself establish full production readiness.
