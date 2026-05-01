#!/usr/bin/env bash
set -euo pipefail

# Codespaces / local helper for the REAL optimized campaign launch endpoint.
#
# SAFETY:
# - Uses confirm=true, so this WILL create Amazon Ads campaigns if your app is running
#   and your Amazon Ads credentials are valid.
# - Replace ASIN before running. Do not use B0XXXX.
# - Keep budget low for first live test.
#
# Required env vars:
#   DAILY_OPTIMIZER_TOKEN  Your optimizer API token
# Optional env vars:
#   API_BASE_URL           Default: http://localhost:8080
#   PRODUCT_TITLE          Default: Liquid Bone Meal
#   PRODUCT_ASIN           Required; must not be B0XXXX
#   PRODUCT_SKU            Optional
#   DAILY_BUDGET           Default: 10

API_BASE_URL="${API_BASE_URL:-http://localhost:8080}"
PRODUCT_TITLE="${PRODUCT_TITLE:-Liquid Bone Meal}"
PRODUCT_ASIN="${PRODUCT_ASIN:-}"
PRODUCT_SKU="${PRODUCT_SKU:-}"
DAILY_BUDGET="${DAILY_BUDGET:-10}"

if [[ -z "${DAILY_OPTIMIZER_TOKEN:-}" ]]; then
  echo "ERROR: DAILY_OPTIMIZER_TOKEN is not set."
  echo "Run: export DAILY_OPTIMIZER_TOKEN='your-token'"
  exit 1
fi

if [[ -z "$PRODUCT_ASIN" || "$PRODUCT_ASIN" == "B0XXXX" ]]; then
  echo "ERROR: PRODUCT_ASIN is required and must be your real Amazon ASIN."
  echo "Example: export PRODUCT_ASIN='B0YOURASIN'"
  exit 1
fi

echo "Launching optimized campaigns for: $PRODUCT_TITLE / $PRODUCT_ASIN"
echo "Endpoint: $API_BASE_URL/api/launch-optimized"
echo "Budget: $DAILY_BUDGET"
echo

curl -sS -X POST "$API_BASE_URL/api/launch-optimized" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DAILY_OPTIMIZER_TOKEN" \
  -d "{
    \"confirm\": true,
    \"product\": {
      \"title\": \"$PRODUCT_TITLE\",
      \"asin\": \"$PRODUCT_ASIN\",
      \"sku\": \"$PRODUCT_SKU\"
    },
    \"keywords\": [
      \"liquid bone meal\",
      \"bone meal fertilizer\",
      \"phosphorus fertilizer\",
      \"calcium fertilizer for plants\"
    ],
    \"budget\": $DAILY_BUDGET
  }" | python -m json.tool
