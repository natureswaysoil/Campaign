#!/usr/bin/env bash
set -euo pipefail
: "${API_BASE_URL:?Set API_BASE_URL to the deployed dashboard URL}"
: "${DAILY_OPTIMIZER_TOKEN:?Set your optimizer token}"
: "${PRODUCT_ID:?Set PRODUCT_ID to the product sheet ID or SKU}"
# Preview is the default; APPLY_LIVE=true authorizes a real launch.
python -c 'import json,os; print(json.dumps({"product_id":os.environ["PRODUCT_ID"],"apply_live":os.getenv("APPLY_LIVE", "false").lower()=="true"}))' |
  curl --fail-with-body -sS "$API_BASE_URL/api/create-campaign-from-product" \
    -H "Authorization: Bearer $DAILY_OPTIMIZER_TOKEN" -H "Content-Type: application/json" --data-binary @-
