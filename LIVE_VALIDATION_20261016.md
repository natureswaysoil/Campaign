# Controlled live validation — October 16, 2026

The owner approved controlled live validation and publication of its results in public PR #47. Later approval explicitly covered testing live campaign creation and activation. No merge, deployment or scheduler enablement was performed.

## Successful live checks

| Validation | Result | Evidence |
|---|---|---|
| Amazon authorization, product lookup, sampled list shapes and bid preflight | Passed | https://github.com/natureswaysoil/Campaign/actions/runs/38979715218 |
| GCS cross-process persistence, lock contention, release/reacquisition and disposable-object cleanup under the runtime identity | Passed after the owner granted scoped impersonation access | Same read-only validation run |
| Campaign creation, ad group, product ad and four automatic targets | Passed against Amazon | https://github.com/natureswaysoil/Campaign/actions/runs/38980751999 |
| Campaign activation | Amazon ENABLED state independently read back | Same lifecycle run |
| Cleanup | PAUSED state verified in the main test and a separate cleanup step | Same lifecycle run |

The lifecycle test used one explicit verified product and a durable test intent. Its campaign was future-dated, capped at $2/day with a $0.07 maximum bid, enabled only to verify the API state transition, then paused. The test does not claim auction delivery, conversions or measured spend results. The persistent intent prevents subsequent reruns from creating additional test campaigns.

## Bug found and corrected by the live test

The first lifecycle attempt created the campaign, ad group and product ad but Amazon rejected the attempted AUTO target POST with HTTP 400 / INVALID_ARGUMENT. Cleanup confirmed the campaign remained PAUSED. The implementation had confused the recommendation enum CLOSE_MATCH with a targeting-clause expression.

Amazon creates four AUTO clauses when the ad group is created: QUERY_HIGH_REL_MATCHES, QUERY_BROAD_REL_MATCHES, ASIN_ACCESSORY_RELATED and ASIN_SUBSTITUTE_RELATED. The corrected provisioner reconciles all four canonical target IDs, their campaign/group ownership, enabled state and bid inheritance instead of POSTing duplicate AUTO targets. The successful retry reused the same campaign and independently verified provisioning before activation.

The creation adapter now supplies a startDate, defaulting to the configured Ads-profile calendar date and honoring an explicit future date for the controlled test.

## Scope and remaining rollout work

The successful read-only observation included 16 verified products, 635 metric records, a $75 saved daily account limit, 333 campaign records and 426 ad groups. Three campaigns were sampled for bid eligibility; two were held and one was unverifiable. These observations do not certify every campaign's economics or bid eligibility.

Live validation exercised the shared production creation/provisioning/activation adapters for an explicitly selected test product. It did not enable unattended product selection or a recurring launch scheduler. Offline recovery tests cover lost acknowledgements and missing/ambiguous readbacks; no network failure was intentionally injected into the live account.

No existing campaign budgets, account policy, product costs, deployment or scheduler settings were changed by the lifecycle probe. The standard guard retained its daily budget reservation for the test, as designed. The newly created test campaign is PAUSED. PR #47 remains a review branch; production rollout remains a separate approval.

Final local offline validation: 297 tests and 3 subtests passed.
