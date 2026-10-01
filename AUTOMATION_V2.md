# Scheduled PPC automation

This adds two production endpoints to `final_server:app`. Both require the
optimizer token and the JSON boolean `apply_live: true`. Preview calls do not
modify ads or durable state.

## Operation

- `/api/automation/retune-tick`: request/poll a 14-day campaign-performance report,
  persist completed metrics, then retune ad-group defaults AND explicit keyword
  bids for enabled campaigns/ad groups/keywords. It blocks live changes until
  metrics are available. Metrics older than 30 minutes trigger a fresh report.
- `/api/automation/harvest-tick`: request/poll a search-term report, then move
  profitable AUTO DISCOVERY terms into the corresponding enabled MANUAL EXACT
  campaign. It applies one completed daily report, up to 10 new terms per product
  and 100 products per run. Campaign pairing requires the existing launch naming
  convention. Missing pairs are reported as skipped, never guessed.
- Both jobs tick every 15 minutes, using America/New_York. Report generation can
  delay bid changes: these are not guaranteed to happen exactly at a time boundary.
- Default keyword multipliers are 0.35 before 10 AM, 1.15 from 10 AM through 8:59 PM,
  and 0.45 from 9 PM. ACOS safeguards may keep bids lower. Keyword-specific
  baselines are persisted BEFORE Amazon writes, preventing compounded reductions
  after a retry or restart. Initial baselines use each keyword's current bid;
  initialize during your normal bidding window and inspect the initial snapshot.
  Later manual bid edits do not automatically rewrite these baselines.
- Proven core buyer phrases now qualify at 8+ clicks, 2+ orders, and ACOS <=35%.
  Unproven core phrases remain protected against automatic negatives; generic
  broad roots remain excluded from promotion. These thresholds retain env overrides.
- Pagination covers all campaign, ad-group, and keyword pages. Changes are batched
  in groups of 100. Only explicit per-item success responses count as accepted.
  Rejected or ambiguous inserts yield failure; retries re-list existing terms to
  avoid resubmitting already visible exact keywords. Paused exact keywords are
  not re-enabled. This is API acceptance verification, not a promise of delivery.

The new scheduled report/apply workflow is for winner harvesting. It does not
schedule automatic campaign launches, pauses, budget increases, or negative
keywords. The manual report/negative controls remain available separately.

## Activation after merging and deploying the code

Run `./setup-scheduler.sh` from an authenticated Google Cloud shell. It creates or
updates a private GCS state bucket, grants the Cloud Run runtime service account
object access, sets `AUTOMATION_STATE_BUCKET`, and creates/updates both scheduler
jobs with OIDC and the optimizer token. It pauses the known incomplete legacy
`daily-campaign-optimizer` job. Inventory separately created retune/harvest jobs
and retire overlapping jobs before enabling this schedule. The setup script has
not been run as part of code review.

State objects are namespaced by Amazon profile ID:

- `keyword-baselines.json`: original keyword bids.
- `bid-workflow.json`: pending performance report, completed metrics, timestamp.
- `harvest-workflow.json`: pending report, completed day, latest apply results.

The process-local `/tmp` pending-report file used by older dashboard controls is
not used by these jobs. Other deployment commands preserve the bucket setting.
The scheduler service account needs Cloud Run Invoker; the runtime service
account needs object read/write/delete on this one bucket. Existing Amazon secret
access remains required. No secrets belong in source control.

## Failure and recovery

202 means the report is pending; the next tick polls it. 502/503 means a failed
report, partial Amazon update, unavailable storage/configuration, or another
error; the scheduler retries. Check the response and Cloud Run logs. A failed
report is discarded so a subsequent tick requests another. Partial keyword
failure retains the report for retry. Jobs already completed for the Eastern
calendar day return without requesting another report.

A generation-checked GCS `.lock` prevents concurrent runs. Locks are released on
ordinary errors. A process forcibly killed while holding a lock leaves it in
place and returns 409 on later ticks. Locks never expire automatically because
another live writer could still be active. Pause the affected job, confirm the
old request has terminated, inspect its JSON state and Amazon results, then
remove ONLY that workflow's `.lock` object and resume the job. Never delete the
baseline/report JSON to recover a lock.

Cloud Run rollout and real Amazon acceptance remain unverified until activation.
Neither this PR nor tests change real ads. Tests use mocked Amazon and storage.
