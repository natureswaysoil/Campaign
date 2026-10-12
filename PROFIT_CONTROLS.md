# Unified profit controls

## What changes

The supported production client (`optimize_campaigns.AmazonAdsClient`) now guards
all Sponsored Products campaign, ad-group, keyword, target and product-ad writes.
`final_server:app` serves `/profit`, linked from the existing dashboard. Legacy
`app.py` and `amazon_ads_client.py` spending writes are disabled to prevent a
second, unguarded optimizer from running. Legacy read/report calls remain usable.

This is an intentional fail-closed rollout: **until policy, product economics,
inventory, mature metrics and current account spend are available, spending
increases and activations are blocked.** Pure pauses/archives still work without
state storage. Verified bid reductions remain possible with missing economics
when the state service and Amazon entity lookup are available. Do not merge and
assume that existing automatic expansion will continue before initialization.

## Scope and limits

- One Amazon Ads profile, USD, Sponsored Products. The account envelope covers
  **all SP campaigns in that profile**, not only campaigns launched by Campaign.
  It does not cover Sponsored Brands, Display, other profiles, or external tools.
- The envelope is the sum of enabled/reserved daily budgets, plus observed spend
  from campaigns outside that envelope. A separate current-spend stop pauses
  enabled campaigns at `daily_limit * (1 - spend_buffer)` (default buffer 15%).
  **This is not an Amazon billing hard cap.** Amazon overdelivery, delayed reports,
  external edits, and in-flight clicks can exceed the configured amount.
- A shared GCS lock serializes mutations. Budget reservations are persisted before
  Amazon writes and retained for the account day even on failed/ambiguous writes.
  A killed process can leave the lock; follow the existing confirmed-dead-writer
  recovery procedure. Never delete reservations to make room for another launch.
- Campaigns must be created paused. Activation validates all product mappings,
  default and explicit bids, inventory and effective CPC. All ASINs in mixed
  campaigns must be viable; the smallest ceiling wins. No fuzzy title mapping.
- Maximum CPC is `min(configured cap, conservative AOV * allowed ACOS * smoothed CVR)`
  multiplied by inventory availability pressure. Allowed ACOS cannot exceed
  per-unit contribution margin less the profit reserve. AOV is capped at the
  advertised ASIN's unit price so cross-product/basket sales cannot inflate it.
  Product prices, COGS, fees, shipping, and other per-unit costs are required.
- Dynamic up/down uplift and placement modifiers are included in effective CPC.
  Unknown bidding modifiers/strategies fail closed. Ceilings round down. A ceiling
  below the minimum bid pauses the entity instead of rounding up through the cap.
- A Beta(1,19) prior tempers low-volume CVR. Default minimum is 30 clicks. No
  conversion evidence means no expansion unless an explicit exploration CVR
  (maximum 3%, default off; exploration campaign budgets default to at most $5/day) is configured. Exploration is an assumption, not a
  prediction of actual performance.

## Activation

1. Deploy the reviewed branch through the existing safety-gated Cloud Run workflow.
2. Keep `AUTOMATION_STATE_BUCKET` and the Ads profile credentials configured.
   Set `AMAZON_ADS_TIMEZONE` to the **actual Ads profile timezone** (default
   `America/New_York`). The connector verifies profile timezone and USD before
   ingesting reports; it never silently combines differently dated account days.
3. Open `/profit`, enter the existing dashboard token, and save a daily account
   limit and desired margin policy. No limit is silently inferred from an old
   conversation, sheet, or campaign budget.
4. Enter complete per-ASIN costs and verified inventory/velocity in the product
   form or authenticated `/api/profit/data`. Costs expire after 30 days by default;
   price/fees and sales velocity after 36 hours; inventory after 24 hours.
   Explicit zeros are allowed; missing/negative/nonfinite inputs are rejected.
5. Configure **separate** SP-API credentials in Secret Manager and bind them to the
   service as `SP_API_CLIENT_ID`, `SP_API_CLIENT_SECRET`, `SP_API_REFRESH_TOKEN`.
   Set `SP_API_SELLER_ID` and `SP_API_MARKETPLACE_ID=ATVPDKIKX0DER`. The authorized
   application needs roles for Listings Items, Product Fees, Sales and Traffic
   reports, and FBA inventory if used. Ads credentials alone do not provide these.
   Never put tokens/credentials into source control or an import body.
6. Run `bash setup-scheduler.sh` from the authorized Cloud Shell, or dispatch the
   existing deployment workflow with its scheduler-provisioning option. This adds
   `ppc-profit-tick` every 15 minutes alongside existing schedules.
7. Use Sync Amazon data, inspect sync outcomes, then Preview protective actions.
   Reports are asynchronous. Retail velocity needs seven completed calendar days;
   initial backfill can require many ticks. Apply only after reviewing data and
   verify accepted Amazon responses and the actual Ads console before relying on it.

Source changes alone do not install credentials, grant Amazon roles, or provision
Cloud Scheduler. This implementation has offline mocked integration coverage;
live SP-API authorization and Amazon report acceptance must be validated at setup.

## Data, attribution and history

`decision_sync.py` requests daily campaign, targeting and placement Ads reports
for the previous 30 days, refreshing every six hours, plus today's full campaign
report every 15 minutes. It persists pending report IDs across ticks. Failed
reports are retried by requesting a replacement on a later tick. Current-spend
freshness uses report **request** time and account date, not the time an old
report was finally downloaded.

Profitability uses rows at least 14 full days old, observed after that window
elapsed. Recent no-sale clicks therefore do not become mature-loss evidence.
The most recent matching feed observation must also be fresh. Multiple data
grains are separate: campaign, keyword/target, placement, hourly, retail. They
are never added together to calculate account spend. Targeting rows without an
ID are skipped rather than assigned to unrelated keywords.

SP-API reads listing offer price and FBM availability; FBA uses fulfillable stock,
not total/reserved stock. Fee values are **estimates**, visibly labeled as such.
COGS and shipping remain locally verified inputs and are never invented or
refreshed merely because a fee lookup succeeds. Sales and Traffic reports are
requested one calendar day at a time; all completed days, including zero-sale
days, enter demand velocity. Retail `orders` is Amazon's `totalOrderItems`, not
unique customer orders. It is not presented as order-level AOV. This is estimated
contribution reporting, not settlement/refund reconciliation or causal ROAS.

Each validated import is archived under `profit-history/<sha256>.json` before
publishing `profit-data.json`. Natural-key replacement supports report restatement
and idempotent replay, including removal of disappeared rows in full report
windows. The materialized view retains 400 days; archives preserve versions.
No buyer names, addresses, or order PII are requested. Configure the private
bucket's lifecycle/retention policy to your authorized data retention requirements.

## Hourly pacing and placement controls

Pacing and placement changes are separate policy toggles, off by default.
Pacing uses measured current spend and available historical hourly revenue weights;
without sufficient hourly observations, it uses an explicitly uniform allocation.
Weekday/hour and placement lifts need minimum click/order/day samples and are
bounded to 0.5–1.25. Every result is subsequently capped by economic limits.
No sample is treated as proof of a causal time-of-day effect.

Hourly data is accepted through authenticated `/api/profit/data` with
`source: "authorized_hourly_adapter"`. Supply **complete cumulative hourly
buckets**, not raw Marketing Stream deltas. Each row includes grain `hourly`,
account-calendar `date`, `hour`, UTC offset (distinguishes the repeated DST hour),
campaign ID, clicks, orders, impressions, spend, sales and observed_at. The
upstream approved Amazon Marketing Stream adapter must merge traffic/conversion
updates and deduplicate deliveries before submission. Native AWS Stream
subscription/provisioning is not performed by this repo. Do not scrape Amazon
pages or invent hourly values by dividing daily reports.

## Inventory, alerts and experiments

Inventory cover is available units / daily units. Ads are constrained when cover
falls below lead time plus safety stock (or the later known restock date plus
safety stock). Zero stock/no ad margin pauses campaigns. A promised/past restock
date never creates available inventory. Recovery requires fresh stock and a
reviewed resume; campaigns are not automatically resumed at midnight or on an ETA.

Alerts include account spend stops, excessive budget envelopes, estimated margin
breaches, stock risk, stale/missing economics and conversion drops. Alerts are
deduplicated by cause/entity, can be acknowledged, and retain resolved history.
They appear in `/profit`; no email/SMS is sent. Correlation alerts do not claim
that reviews or competitors caused a conversion change.

Experiment tracking supports titles, main images, bullets, descriptions, A+ and
Brand Story. Running/completed records require recently verified Brand Registry
and ASIN eligibility plus the Amazon experiment ID; completed records require
Amazon result notes and a winner or "inconclusive". Eligibility is manually
verified in Seller Central. Campaign does **not** invent an experiments API,
start experiments, rotate live listings, infer a winner from simple before/after
sales, or publish listing changes. Experiment changes retain audit history.

## API and monitoring

All `/api/` routes retain optimizer-token authentication and explicit boolean
`apply_live: true`; `dry_run: true` overrides it.

| Route | Purpose |
| --- | --- |
| GET `/api/profit/status` | Policy, products, feed health, alerts, last controls, experiments |
| POST `/api/profit/policy` | Preview/save validated policy |
| POST `/api/profit/data` | Validate/import product or normalized metric observations |
| POST `/api/profit/control` | Preview/apply inventory, margin, spend and placement actions |
| POST `/api/automation/profit-tick` | Poll data feeds and apply protective controls |
| POST `/api/profit/experiments` | Preview/save experiment tracking; optional `id` updates |
| POST `/api/profit/alerts/acknowledge` | Acknowledge a specific alert |

`profit-sync-status.json` reports each feed/control outcome separately. A failure
returns non-2xx so Scheduler retries. Every guarded write has a durable
`profit-audit/<id>.json`; API responses include `_profit_guard` with the audit ID
and any capped/paused adjustments. These adjustments can differ from a legacy
endpoint's pre-guard proposal: the audit and Amazon state are authoritative.

## Validation and primary references

Run `DAILY_OPTIMIZER_TOKEN=secret-token python -m pytest -q --ignore=test_creds.py`.
Never run `test_creds.py` as an offline test. Search-term harvest, opportunity and manual negative reports now end 15 days ago.
Previously pending manual reports without mature dates must be requested again.
The automation monitor also checks the profit workflow for missing/stale/failing runs.

New tests cover per-ASIN ceilings,
keyword performance, sparse evidence, maturity, inventory, placement/dynamic
uplift, all-account reservations, ambiguous writes, midnight freshness, replay,
FBM sync, API authentication and experiment eligibility.

- https://advertising.amazon.com/API/docs/en-us/guides/reporting/v3/report-types
- https://advertising.amazon.com/API/docs/en-us/guides/reporting/v3/report-types/targeting
- https://advertising.amazon.com/solutions/products/amazon-marketing-stream
- https://developer-docs.amazon/sp-api/docs/report-type-values-analytics
- https://developer-docs.amazon/sp-api/reference/getlistingsitem
- https://developer-docs.amazon/sp-api/reference/getmyfeesestimateforasin
- https://developer-docs.amazon/sp-api/reference/getinventorysummaries
- https://sell.amazon.com/tools/manage-your-experiments
