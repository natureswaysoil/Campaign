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

# Get auth token
TOKEN=$(gcloud auth print-identity-token)

# Trigger the /optimize endpoint
curl -X POST "${SERVICE_URL}/optimize" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json"

echo ""
echo "✓ Triggered! Check logs in ~5 minutes:"
echo "gcloud logging read \"resource.type=cloud_run_revision AND resource.labels.service_name=${SERVICE_NAME}\" --limit 50 --project=${PROJECT_ID}"
