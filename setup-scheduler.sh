#!/bin/bash
set -e

# Configuration
PROJECT_ID="amazon-ppc-bid-optimizer"
REGION="us-central1"
SERVICE_NAME="campaign-optimizer"
SCHEDULER_JOB_NAME="daily-campaign-optimizer"

echo "Setting up Cloud Scheduler..."

DAILY_OPTIMIZER_TOKEN=$(gcloud secrets versions access latest \
  --secret=DAILY_OPTIMIZER_TOKEN \
  --project=${PROJECT_ID})

# Get the service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} \
  --region ${REGION} \
  --format 'value(status.url)' \
  --project=${PROJECT_ID})

echo "Service URL: ${SERVICE_URL}"

# Create service account for invoking Cloud Run
SA_NAME="campaign-optimizer-scheduler"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Check if service account exists
if ! gcloud iam service-accounts describe ${SA_EMAIL} --project=${PROJECT_ID} &>/dev/null; then
  echo "Creating service account..."
  gcloud iam service-accounts create ${SA_NAME} \
    --display-name="Campaign Optimizer Scheduler" \
    --project=${PROJECT_ID}
  
  # Grant Cloud Run Invoker role
  gcloud run services add-iam-policy-binding ${SERVICE_NAME} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/run.invoker" \
    --region=${REGION} \
    --project=${PROJECT_ID}
else
  echo "Service account already exists"
fi

# Delete existing job if it exists
if gcloud scheduler jobs describe ${SCHEDULER_JOB_NAME} --location=${REGION} --project=${PROJECT_ID} &>/dev/null; then
  echo "Deleting existing scheduler job..."
  gcloud scheduler jobs delete ${SCHEDULER_JOB_NAME} \
    --location=${REGION} \
    --project=${PROJECT_ID} \
    --quiet
fi

# Create Cloud Scheduler job (runs daily at 9 AM EST)
echo "Creating scheduler job..."
gcloud scheduler jobs create http ${SCHEDULER_JOB_NAME} \
  --location=${REGION} \
  --schedule="0 9 * * *" \
  --time-zone="America/New_York" \
  --uri="${SERVICE_URL}/api/run-daily-optimization" \
  --http-method=POST \
  --headers="Content-Type=application/json,X-Daily-Optimizer-Token=${DAILY_OPTIMIZER_TOKEN}" \
  --message-body='{"apply_campaign_pauses_live":true,"apply_negatives_live":true,"apply_winners_live":true,"pause_no_sales_campaigns":true,"min_clicks_for_no_sales_pause":10,"lookback_days":14,"pause_acos_threshold":0.40,"prime_high_bid_multiplier":1.25,"off_prime_bid_multiplier":0.35}' \
  --oidc-service-account-email=${SA_EMAIL} \
  --project=${PROJECT_ID}

echo ""
echo "✅ Cloud Scheduler configured!"
echo ""
echo "📅 Schedule: Daily at 9:00 AM EST"
echo "🎯 Action: Runs Priority 1 optimizations automatically"
echo ""
echo "To test manually:"
echo "  gcloud scheduler jobs run ${SCHEDULER_JOB_NAME} --location=${REGION} --project=${PROJECT_ID}"
echo ""
echo "To view logs:"
echo "  gcloud logging read \"resource.type=cloud_run_revision AND resource.labels.service_name=${SERVICE_NAME}\" --limit 50 --project=${PROJECT_ID}"
