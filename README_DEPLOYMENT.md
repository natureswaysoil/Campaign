# Production deployment and operation

## Current production behavior (October 2026)

The supported entrypoint is `final_server:app` (Dockerfile). The dashboard stores
its access token using Set Token; `/login` belongs to the older app and is not a
production login endpoint. The GitHub deploy workflow is the canonical deploy.
`app.py`, `run_optimizer.py`, and the archived ZIP are legacy implementations.

Cloud Scheduler drives bids, harvesting, opportunities, and growth. The separate
GitHub daily campaign-plan workflow only generates a preview artifact.
Automatic growth and opportunity launches require complete product costs or an
explicit gross margin from the product sheet. Missing costs hold expansion for
review. The monitor checks all four workflows and reports missing outcomes.

With AUTOMATION_STATE_BUCKET configured, manual pending reports/history and
ad-group baselines are stored in GCS. Manual report/apply calls are serialized;
a killed request can require the lock recovery procedure in AUTOMATION_V2.md.
Old process-local pending reports must be requested again after this deployment.
Configured bid ranges on dashboard cards include enabled ad-group defaults and
explicit keyword/target bids; they are not actual auction prices. Failed partial
reads are labeled partial. Amazon recommendation ranges remain unavailable when
no verified recommendation was retrieved.

## Deployment

Pushes to main run the production safety gate and then deploy Cloud Run. The
runtime needs the Amazon Ads credentials, DAILY_OPTIMIZER_TOKEN, and
AUTOMATION_STATE_BUCKET. The dashboard HTML and health endpoint are public;
all /api/ endpoints require the optimizer token. Alternative deployment scripts
preserve this access model and existing environment settings.

Run `bash setup-scheduler.sh` once from an authorized Cloud Shell to provision
schedules and state storage. Routine deployment preserves them. The harvest tick
also drives opportunity and growth workflows; each has separate durable state.
See AUTOMATION_V2.md for lock recovery after an interrupted process.

## Verification

Run `DAILY_OPTIMIZER_TOKEN=secret-token python -m pytest -q --ignore=test_creds.py`.
Tests use mocked Amazon responses. `test_creds.py` is an explicit live credential
smoke script and must not be collected as an offline test.

Open the dashboard and use Set Token once in the browser. The monitor reads
saved outcomes; a green result verifies recent recorded workflow outcomes, not
profitability or future delivery. To request manual optimization use the
provided trigger script, then Apply Pending Report after Amazon finishes.
