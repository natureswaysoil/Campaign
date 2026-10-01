#!/usr/bin/env bash
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-amazon-ppc-bid-optimizer}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-campaign-optimizer}"
STATE_BUCKET="${AUTOMATION_STATE_BUCKET:-${PROJECT_ID}-ppc-automation-state}"
SA_EMAIL="campaign-optimizer-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')
RUNTIME_SA=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" --format='value(spec.template.spec.serviceAccountName)')
if [[ -z "$RUNTIME_SA" ]]; then
  echo 'Cloud Run runtime service account could not be resolved.' >&2
  exit 1
fi
TOKEN=$(gcloud secrets versions access latest --secret=DAILY_OPTIMIZER_TOKEN --project "$PROJECT_ID")
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then echo "::add-mask::${TOKEN}"; fi
[[ -n "$TOKEN" ]] || { echo 'Optimizer token is empty' >&2; exit 1; }
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" --location "$REGION" --uniform-bucket-level-access --project "$PROJECT_ID"
fi
gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" --member="serviceAccount:${RUNTIME_SA}" --role=roles/storage.objectAdmin --project "$PROJECT_ID"
gcloud run services update "$SERVICE_NAME" --update-env-vars="AUTOMATION_STATE_BUCKET=${STATE_BUCKET}" --region "$REGION" --project "$PROJECT_ID"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create campaign-optimizer-scheduler --project "$PROJECT_ID"
fi
gcloud run services add-iam-policy-binding "$SERVICE_NAME" --member="serviceAccount:${SA_EMAIL}" --role=roles/run.invoker --region "$REGION" --project "$PROJECT_ID"
for job in ppc-daypart-tick ppc-harvest-tick; do
  if [[ "$job" == ppc-daypart-tick ]]; then route=retune-tick; else route=harvest-tick; fi
  verb=create
  header_flag=--headers
  if gcloud scheduler jobs describe "$job" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then verb=update; header_flag=--update-headers; fi
  gcloud scheduler jobs "$verb" http "$job" --location "$REGION" --project "$PROJECT_ID" \
    --schedule='*/15 * * * *' --time-zone=America/New_York \
    --uri="${SERVICE_URL}/api/automation/${route}" --http-method=POST \
    "${header_flag}=Content-Type=application/json,X-Daily-Optimizer-Token=${TOKEN}" \
    --message-body='{"apply_live":true}' --oidc-service-account-email="$SA_EMAIL" \
    --oidc-token-audience="$SERVICE_URL" --attempt-deadline=1800s \
    --max-retry-attempts=3 --min-backoff=60s --max-backoff=300s --format="value(name)"
 done
# Supersede the incomplete report-only job without deleting its configuration.
if gcloud scheduler jobs describe daily-campaign-optimizer --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud scheduler jobs pause daily-campaign-optimizer --location "$REGION" --project "$PROJECT_ID"
fi
echo 'Bid and harvest ticks configured every 15 minutes Eastern. Harvesting completes once per day.'
