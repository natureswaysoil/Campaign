#!/bin/bash
# Manual trigger for testing

PROJECT_ID="amazon-ppc-bid-optimizer"
REGION="us-central1"
SERVICE_NAME="campaign-optimizer"

echo "Triggering campaign optimizer manually..."

SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} \
  --region ${REGION} \
  --format 'value(status.url)' \
  --project=${PROJECT_ID})

OIDC_TOKEN=$(gcloud auth print-identity-token)
DAILY_OPTIMIZER_TOKEN=$(gcloud secrets versions access latest \
  --secret=DAILY_OPTIMIZER_TOKEN \
  --project=${PROJECT_ID})

# Trigger the daily optimization endpoint
curl -X POST "${SERVICE_URL}/api/run-daily-optimization" \
  -H "Authorization: Bearer ${OIDC_TOKEN}" \
  -H "X-Daily-Optimizer-Token: ${DAILY_OPTIMIZER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"apply_campaign_pauses_live":true,"apply_negatives_live":true,"apply_winners_live":true,"pause_no_sales_campaigns":true,"min_clicks_for_no_sales_pause":10,"lookback_days":14,"pause_acos_threshold":0.40,"prime_high_bid_multiplier":1.25,"off_prime_bid_multiplier":0.35}'

echo ""
echo "✓ Triggered! Check logs in ~5 minutes:"
echo "gcloud logging read \"resource.type=cloud_run_revision AND resource.labels.service_name=${SERVICE_NAME}\" --limit 50 --project=${PROJECT_ID}"
