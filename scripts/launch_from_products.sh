#!/usr/bin/env bash
set -euo pipefail

# Launch optimized campaigns from a local products JSON file so you do not have to
# type an ASIN every time.
#
# Expected JSON file format:
# [
#   {"title":"Nature's Way Soil Liquid Bone Meal 1 Gallon", "asin":"B0...", "sku":"..."},
#   {"title":"Nature's Way Soil Liquid Kelp 1 Gallon", "asin":"B0...", "sku":"..."}
# ]
#
# Required:
#   products.json or set PRODUCTS_FILE=/path/to/products.json
#
# Token behavior:
#   1) Uses DAILY_OPTIMIZER_TOKEN from shell if present.
#   2) If not present, reads from .env / .env.local without printing it.
#
# Examples:
#   PRODUCTS_FILE=products.json ./scripts/launch_from_products.sh
#   PRODUCT_INDEX=2 ./scripts/launch_from_products.sh
#   LAUNCH_ALL=true ./scripts/launch_from_products.sh

API_BASE_URL="${API_BASE_URL:-http://localhost:8080}"
PRODUCTS_FILE="${PRODUCTS_FILE:-products.json}"
DAILY_BUDGET="${DAILY_BUDGET:-5}"
PRODUCT_INDEX="${PRODUCT_INDEX:-}"
LAUNCH_ALL="${LAUNCH_ALL:-false}"
CONFIRM="${CONFIRM:-false}"

load_token_from_env_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    local line
    line=$(grep -E '^DAILY_OPTIMIZER_TOKEN=' "$file" | tail -n 1 || true)
    if [[ -n "$line" ]]; then
      DAILY_OPTIMIZER_TOKEN="${line#DAILY_OPTIMIZER_TOKEN=}"
      DAILY_OPTIMIZER_TOKEN="${DAILY_OPTIMIZER_TOKEN%\"}"
      DAILY_OPTIMIZER_TOKEN="${DAILY_OPTIMIZER_TOKEN#\"}"
      export DAILY_OPTIMIZER_TOKEN
    fi
  fi
}

if [[ -z "${DAILY_OPTIMIZER_TOKEN:-}" ]]; then
  load_token_from_env_file ".env"
fi
if [[ -z "${DAILY_OPTIMIZER_TOKEN:-}" ]]; then
  load_token_from_env_file ".env.local"
fi

if [[ -z "${DAILY_OPTIMIZER_TOKEN:-}" ]]; then
  echo "ERROR: DAILY_OPTIMIZER_TOKEN was not found in shell, .env, or .env.local."
  echo "Set it once in Codespaces secrets or add it to your local .env file."
  exit 1
fi

if [[ ! -f "$PRODUCTS_FILE" ]]; then
  echo "ERROR: Products file not found: $PRODUCTS_FILE"
  echo "Create products.json with title/asin/sku records."
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required. In Codespaces run: sudo apt-get update && sudo apt-get install -y jq"
  exit 1
fi

count=$(jq 'length' "$PRODUCTS_FILE")
if [[ "$count" -eq 0 ]]; then
  echo "ERROR: $PRODUCTS_FILE has no products."
  exit 1
fi

echo "Products found in $PRODUCTS_FILE:"
jq -r 'to_entries[] | "[\(.key)] \(.value.title // .value.name) | ASIN: \(.value.asin // .value.ASIN) | SKU: \(.value.sku // .value.SKU // .value.sellerSku // "")"' "$PRODUCTS_FILE"
echo

launch_one() {
  local idx="$1"
  local product
  product=$(jq -c ".[$idx]" "$PRODUCTS_FILE")
  local title asin
  title=$(echo "$product" | jq -r '.title // .name // .productName // "Untitled Product"')
  asin=$(echo "$product" | jq -r '.asin // .ASIN // ""')

  if [[ -z "$asin" || "$asin" == "null" ]]; then
    echo "Skipping [$idx] $title — missing ASIN"
    return 0
  fi

  echo "Preparing optimized launch for [$idx] $title / $asin"

  local confirm_json="false"
  if [[ "$CONFIRM" == "true" ]]; then
    confirm_json="true"
    echo "REAL LAUNCH ENABLED for [$idx]"
  else
    echo "Preview only for [$idx]. Set CONFIRM=true to create campaigns."
  fi

  jq -n \
    --argjson confirm "$confirm_json" \
    --argjson product "$product" \
    --argjson budget "$DAILY_BUDGET" \
    '{confirm:$confirm, product:$product, budget:$budget}' \
  | curl -sS -X POST "$API_BASE_URL/api/launch-optimized" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer '$DAILY_OPTIMIZER_TOKEN'" \
      --data-binary @- \
  | python -m json.tool

  echo
}

if [[ "$LAUNCH_ALL" == "true" ]]; then
  for ((i=0; i<count; i++)); do
    launch_one "$i"
  done
elif [[ -n "$PRODUCT_INDEX" ]]; then
  launch_one "$PRODUCT_INDEX"
else
  read -r -p "Enter product number to preview/launch: " selected
  launch_one "$selected"
fi
