# GCP Cloud Run Deployment

## 🚀 One-Time Setup

### 1. Deploy to Cloud Run
```bash
./deploy-cloud-run.sh
```
This builds the Docker image and deploys to Cloud Run (~5 minutes)

### 2. Set up Daily Scheduling
```bash
./setup-scheduler.sh
```
This creates a Cloud Scheduler job that runs daily at 9 AM EST

## 🧪 Testing

### Authenticated Smoke Test
```bash
./smoke-test-cloud-run.sh
```

This verifies the private Cloud Run service with the same auth model used in production:
- `GET /` returns the authenticated landing page
- `GET /health` returns the health payload
- `GET /api/ops-status` returns ops metrics
- `POST /api/run-daily-optimization` can be exercised separately via `./trigger-optimizer.sh`

### Manual Trigger
```bash
./trigger-optimizer.sh
```

### Manual Authenticated Checks
```bash
SERVICE_URL=$(gcloud run services describe campaign-optimizer \
  --region us-central1 \
  --format='value(status.url)' \
  --project=amazon-ppc-bid-optimizer)

TOKEN=$(gcloud auth print-identity-token)

curl -H "Authorization: Bearer ${TOKEN}" "${SERVICE_URL}/"
curl -H "Authorization: Bearer ${TOKEN}" "${SERVICE_URL}/health"
curl -H "Authorization: Bearer ${TOKEN}" "${SERVICE_URL}/api/ops-status"
```

### View Logs
```bash
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=campaign-optimizer" \
  --limit 50 \
  --project=amazon-ppc-bid-optimizer
```

## 📅 Schedule

**Automatic runs:** Every day at 9:00 AM EST

The optimizer will:
1. Fetch fresh campaign IDs from Amazon Ads
2. Download latest campaign performance report
3. Apply Priority 1 optimizations automatically
4. Send notification with results

## ⚙️ Configuration

Edit environment variables in `deploy-cloud-run.sh`:
- `PRIORITY_FILTER=1` - Which priority level to auto-apply (1-5)
- `NOTIFICATION_EMAIL` - Email for notifications
- `SLACK_WEBHOOK_URL` - Slack webhook for alerts

## 🔒 Security

- Service is **not publicly accessible** (requires authentication)
- Browser hits to the service URL without a Google identity token will return `403` at the Cloud Run layer
- The root route `/` exists in the app, but you only reach it after Cloud Run authentication succeeds
- Uses GCP Secret Manager for Amazon Ads credentials
- Runs with minimal IAM permissions

## 💰 Cost

Estimated monthly cost:
- Cloud Run: ~$5/month (1 run/day, 5 min each)
- Cloud Scheduler: $0.10/month
- **Total: ~$5/month**

## 📊 Monitoring

View execution history in GCP Console:
https://console.cloud.google.com/run/detail/us-central1/campaign-optimizer
