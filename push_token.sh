#!/bin/bash
# Usage:
# ./push_token.sh YOUR_ACCESS_TOKEN

ACCESS_TOKEN="$1"

if [ -z "$ACCESS_TOKEN" ]; then
  echo "Error: no access token provided."
  echo "Usage: ./push_token.sh YOUR_ACCESS_TOKEN"
  exit 1
fi

curl -X POST https://hedge-ai.onrender.com/admin/set_token \
  -H "Content-Type: application/json" \
  -H "X-ADMIN-KEY: HedgeAI_Admin_2025!" \
  -d "{\"access_token\":\"$ACCESS_TOKEN\"}"