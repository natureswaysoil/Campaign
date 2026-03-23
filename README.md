# Amazon Ads Sheet Autopilot

This service reads products from a Google Sheet CSV feed and can launch Sponsored Products campaigns from SKU or product ID.

## Implemented Features

- Structured campaign launch summaries stored in `data/campaign_launches.jsonl`
- Keyword deduplication before match-type expansion and submission
- Post-launch optimization checklist API for dashboard/log workflows
- Rule-based daily budget recommendation and optional live budget updates

## Key Endpoints

- `GET /api/products`
- `GET /api/generate-keywords/{sku_or_product_id}`
- `POST /api/create-campaign-from-product`
- `GET /api/launch-logs?limit=10`
- `GET /api/launch-logs/latest`
- `GET /api/optimization-checklist`
- `POST /api/recommend-budget-adjustment`
- `POST /api/adjust-campaign-budget`
- `POST /api/run-daily-optimization`
- `POST /api/quick-optimize-safe-negatives`
- `GET /api/optimizer-runs?limit=10`
- `GET /api/export/launch-logs.json`
- `GET /api/export/launch-logs.csv`
- `GET /api/export/optimizer-runs.json`
- `GET /api/export/optimizer-runs.csv`

## Filtering

These endpoints support optional query filters:

- `campaign_id`
- `start_date` (`YYYY-MM-DD` or `YYYYMMDD`)
- `end_date` (`YYYY-MM-DD` or `YYYYMMDD`)

Supported on:

- `GET /api/launch-logs`
- `GET /api/launch-logs/latest`
- `GET /api/optimizer-runs`
- `GET /api/export/launch-logs.json`
- `GET /api/export/launch-logs.csv`
- `GET /api/export/optimizer-runs.json`
- `GET /api/export/optimizer-runs.csv`

`POST /api/run-daily-optimization` and `POST /api/quick-optimize-safe-negatives` also accept optional `campaign_id`, `start_date`, and `end_date` in the JSON payload.

## Budget Adjustment Inputs

Example payload:

```json
{
  "campaign_id": "274716363056043",
  "current_budget": 25,
  "target_acos": 0.35,
  "acos": 0.31,
  "budget_utilization": 0.93,
  "clicks": 42,
  "orders": 4,
  "spend": 23.7,
  "apply_live": false
}
```

Guardrails are enforced in code:

- minimum daily budget floor: 10
- max step change per adjustment: 25%
- cooldown period: 48 hours
- rolling 7-day budget change cap: 50%
