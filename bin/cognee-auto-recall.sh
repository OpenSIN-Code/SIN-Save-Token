#!/usr/bin/env bash
# Cognee Auto-Recall Hook — injects relevant fleet memory at session start.
# Called by agent SessionStart hooks (Claude Code, Codex, etc.)
#
# Usage:
#   bin/cognee-auto-recall.sh [project-context]
#
# Reads the project context (optional) and recalls relevant knowledge from Cognee.
# Outputs a brief summary suitable for injection into agent context.
set -euo pipefail

PROJECT_CTX="${1:-}"
COGNEE_BASE="${COGNEE_BASE_URL:-http://127.0.0.1:8011}"
COGNEE_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"

# Read API key
API_KEY=""
if [ -f "$HOME/.cognee-plugin/api_key.json" ]; then
  API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.cognee-plugin/api_key.json')).get('api_key',''))" 2>/dev/null || true)
fi

if [ -z "$API_KEY" ]; then
  echo "[cognee] No API key configured — skipping auto-recall."
  exit 0
fi

# Check if Cognee is running
if ! curl -sS -m 2 "$COGNEE_BASE/health" 2>/dev/null | grep -q "healthy"; then
  echo "[cognee] Cognee API not healthy — skipping auto-recall."
  exit 0
fi

# Build query from project context or use generic query
QUERY="${PROJECT_CTX:-SIN-Save-Token project rules and best practices}"

# Recall relevant knowledge
RESULT=$(curl -sS -m 60 "$COGNEE_BASE/api/v1/recall" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"$QUERY\",\"datasets\":[\"$COGNEE_DATASET\"],\"top_k\":3}" 2>/dev/null || echo "[]")

# Extract and format results
ITEMS=$(echo "$RESULT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if not isinstance(data, list):
        data = [data]
    for item in data[:3]:
        text = item.get('text') or item.get('raw', {}).get('value') or ''
        if text:
            short = text[:200] + '...' if len(text) > 200 else text
            print(f'  - {short}')
except Exception:
    pass
" 2>/dev/null)

if [ -n "$ITEMS" ]; then
  echo "[cognee] Relevant fleet memory:"
  echo "$ITEMS"
else
  echo "[cognee] No relevant fleet memory found."
fi
