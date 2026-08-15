#!/usr/bin/env bash
# Ingest SIN fleet docs into Cognee dataset sin-fleet (sync cognify).
# Requires: Cognee healthy on :8011 and OmniRoute LLM+embed wired.
# COGNEE_API_KEY is optional for authenticated deployments; the canonical
# loopback single-user runtime does not require a local credential file.
set -euo pipefail

COGNEE_URL="${COGNEE_BASE_URL:-http://127.0.0.1:8011}"
DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
API_KEY="${COGNEE_API_KEY:-}"
AUTH_HEADER=()
if [ -n "$API_KEY" ]; then AUTH_HEADER=(-H "X-Api-Key: $API_KEY"); fi

DOCS=(
  "$HOME/dev/SIN-Save-Token/README.md"
  "$HOME/dev/SIN-Save-Token/docs/ECOSYSTEM.md"
  "$HOME/dev/SIN-Save-Token/docs/BEST-PRACTICES.md"
  "$HOME/dev/SIN-Save-Token/.planning/PLAN-cognee-L3.md"
  "$HOME/dev/wow-my-zsh/README.md"
  "$HOME/dev/wow-my-zsh/shared/AGENTS.md"
  "$HOME/dev/wow-my-zsh/docs/token-discipline-clis.md"
)

# Ensure dataset exists
curl -sS -m 15 -X POST "$COGNEE_URL/api/v1/datasets" \
  "${AUTH_HEADER[@]}" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$DATASET\"}" >/dev/null 2>&1 || true

ok=0
fail=0
TMP=$(mktemp -d)
for f in "${DOCS[@]}"; do
  [ -f "$f" ] || { echo "skip missing $f"; continue; }
  base=$(basename "$f")
  # Cap size for pilot cost
  python3 -c "
from pathlib import Path
src=Path('$f')
text=src.read_text(errors='replace')[:20000]
Path('$TMP/$base').write_text(f'# Source: {src}\\n\\n'+text)
"
  echo "=== remember $base ==="
  RESP=$(curl -sS -m 600 -X POST "$COGNEE_URL/api/v1/remember" \
    -H "X-Api-Key: $API_KEY" \
    -F "data=@$TMP/$base;type=text/plain" \
    -F "datasetName=$DATASET" \
    -F "run_in_background=false" \
    -w "|%{http_code}")
  CODE="${RESP##*|}"
  BODY="${RESP%|*}"
  echo "HTTP $CODE ${BODY:0:200}"
  if [ "$CODE" = "200" ]; then ok=$((ok + 1)); else fail=$((fail + 1)); fi
done
rm -rf "$TMP"
echo "INGEST ok=$ok fail=$fail"
[ "$fail" -eq 0 ]
