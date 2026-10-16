# Controlled live validation — October 16, 2026

The owner approved live validation and subsequently approved publishing these results in public PR #47. No deployment or advertising changes were authorized by that publication approval.

Latest live run: https://github.com/natureswaysoil/Campaign/actions/runs/38978458275

| Check | Observed result |
|---|---|
| Production revision | campaign-optimizer-00330-pzd; automatic launching disabled |
| Amazon Ads authorization | Passed; configured USD profile and America/Los_Angeles reporting timezone match |
| Saved decision inputs | Read successfully: 16 products, 635 metric records, $75 daily policy limit |
| Current spend observation | 24.7 minutes old at the latest validation |
| Amazon object reads | 333 campaigns and 426 groups returned; product-ad, keyword and target read shapes sampled on 3 campaigns |
| Sampled bid preflight | 2 held samples and 1 unverifiable sample; not an account-wide eligibility certification |
| Selling Partner lookup | Authorization and one exact product identity/fee/inventory lookup passed; refreshed values retained only in memory |
| Approved exploration preflight | Eligible at a $0.07 maximum bid and $2/day campaign budget; no campaign created |
| Real GCS cross-process lock | NOT VALIDATED: GitHub validation identity cannot impersonate the runtime service account (`runtime_impersonation_denied`) |

The first GCS probe under the GitHub identity received Forbidden. The subsequent probe requested the runtime identity and was denied token issuance. This is an access blocker for the validation job; it is not evidence that the application runtime cannot write GCS.

Completing the storage check requires an authorized identity with runtime impersonation permission (`iam.serviceAccounts.getAccessToken` on that runtime service account), or execution of the same probe by an operator already authorized as that runtime identity. No IAM changes were made or inferred from validation approval.

The storage probe targets a unique disposable `validation-live/<uuid>` namespace, never production workflow records. The validation adapters block Amazon advertising mutations. No merge, deployment, campaign creation/activation, budget/policy change, or scheduler enablement occurred.

Production safety CI passed 268 tests. Local offline validation passed 295 tests plus 3 subtests. These results do not substitute for the uncompleted real GCS test or live campaign creation/activation validation.
