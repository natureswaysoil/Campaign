# Amazon Ads Autopilot for Cloud Run

This package is ready to drop into a GitHub repo and deploy to Cloud Run.

## What it includes

- FastAPI app
- Google Secret Manager loading
- Sponsored Products campaign creation
- starter keyword generation
- report-based optimization from CSV
- daily optimizer route for Cloud Scheduler
- Dockerfile
- Cloud Build config
- GitHub Actions deploy workflow
- example request payloads

## Important

Amazon Ads API endpoint paths and report payload fields can vary by API version and account setup.
Before running live, verify the constants in `app.py`, especially:

- `/sp/campaigns`
- `/sp/adGroups`
- `/sp/productAds`
- `/sp/keywords`
- `/sp/negativeKeywords`
- `/reporting/reports`

## Secrets expected in Google Secret Manager

Store these with these exact names:

- AMAZON_ADS_CLIENT_ID
- AMAZON_ADS_CLIENT_SECRET
- AMAZON_ADS_REFRESH_TOKEN
- AMAZON_ADS_PROFILE_ID
- AMAZON_ADS_REGION
- DAILY_OPTIMIZER_TOKEN

Set this Cloud Run env var:

- GCP_PROJECT_ID

## Routes

### GET /health

Health check.

### POST /create-campaign

Create a Sponsored Products campaign.

### POST /optimize-from-report

Send CSV text from a search-term report and classify:

- winners
- negatives
- hold

Can optionally apply negatives and promote winners.

### POST /run-daily-optimization

Protected with `Authorization: Bearer <DAILY_OPTIMIZER_TOKEN>` when `DAILY_OPTIMIZER_TOKEN` is present.

This route:

1. requests a Sponsored Products search-term report
2. polls until complete
3. downloads and parses the report
4. classifies search terms
5. optionally adds negatives
6. optionally promotes winners

## Deploy manually

```bash
gcloud run deploy amazon-ads-autopilot \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars GCP_PROJECT_ID=YOUR_PROJECT_ID
```

## Schedule daily optimizer

Create a Cloud Scheduler job that POSTs to:

`https://YOUR_CLOUD_RUN_URL/run-daily-optimization`

with header:

`Authorization: Bearer YOUR_DAILY_OPTIMIZER_TOKEN`

Example JSON body:

```json
{
  "apply_negatives_live": true,
  "apply_winners_live": true,
  "winner_bid": 0.9
}
```

## Suggested GitHub secrets

For the included GitHub Action:

- GCP_SA_KEY
- GCP_PROJECT_ID

## Example curl

```bash
curl -X POST "https://YOUR_URL/create-campaign" \
  -H "Content-Type: application/json" \
  -d '{
    "sku":"YOUR-SKU",
    "asin":"YOURASIN",
    "product_name":"Nature\'s Way Soil Dog Urine Neutralizer",
    "daily_budget":25,
    "default_bid":0.85,
    "mode":"manual"
  }'
```
