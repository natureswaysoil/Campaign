#!/bin/bash
set -e

# Configuration
PROJECT_ID="amazon-ppc-bid-optimizer"
REGION="us-central1"
SERVICE_NAME="campaign-optimizer"
IMAGE_URI="us-central1-docker.pkg.dev/${PROJECT_ID}/amazon-ads-autopilot/app:latest"

echo "Deploying Campaign Optimizer to Cloud Run..."

# Build and deploy
gcloud builds submit --tag "${IMAGE_URI}" \
  --project=${PROJECT_ID}

gcloud run deploy ${SERVICE_NAME} \
  --image "${IMAGE_URI}" \
  --platform managed \
  --region ${REGION} \
  --no-allow-unauthenticated \
  --set-env-vars GCP_PROJECT_ID=${PROJECT_ID},PRIORITY_FILTER=1 \
  --set-secrets AMAZON_ADS_CLIENT_ID=AMAZON_ADS_CLIENT_ID:latest,AMAZON_ADS_CLIENT_SECRET=AMAZON_ADS_CLIENT_SECRET:latest,AMAZON_ADS_REFRESH_TOKEN=AMAZON_ADS_REFRESH_TOKEN:latest,AMAZON_ADS_PROFILE_ID=AMAZON_ADS_PROFILE_ID:latest,AMAZON_ADS_REGION=AMAZON_ADS_REGION:latest,DAILY_OPTIMIZER_TOKEN=DAILY_OPTIMIZER_TOKEN:latest \
  --timeout 1800 \
  --memory 1Gi \
  --project=${PROJECT_ID}

echo "✓ Deployed to Cloud Run"

# Get the service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} --region ${REGION} --format 'value(status.url)' --project=${PROJECT_ID})

echo "Service URL: ${SERVICE_URL}"
echo ""
echo "Next: Set up Cloud Scheduler to trigger this URL daily"
