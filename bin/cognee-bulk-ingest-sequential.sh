#!/usr/bin/env bash
# Sequential fleet-doc ingest. **COSTS OmniRoute credits** (GLM 5.2 cognify per doc).
#
# Refuses to run unless:
#   COGNEE_ALLOW_COSTLY=1
# Optional cap:
#   COGNEE_BULK_MAX_DOCS=3   (default 3 — never dump whole fleet by accident)
#   COGNEE_BULK_MAX_CHARS=4000  (default 4k chars/doc for cheap re-ingest)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export COGNEE_PLUGIN_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
export COGNEE_BASE_URL="${COGNEE_BASE_URL:-http://127.0.0.1:8011}"

if [ "${COGNEE_ALLOW_COSTLY:-0}" != "1" ]; then
  cat >&2 <<'EOF'
REFUSED: bulk cognify spends OmniRoute (GLM 5.2) credits.

  export COGNEE_ALLOW_COSTLY=1
  # optional safety caps:
  export COGNEE_BULK_MAX_DOCS=2
  export COGNEE_BULK_MAX_CHARS=3000
  bin/cognee-bulk-ingest-sequential.sh

Prefer: cognee-recall (cheap/free-ish read) for everyday use.
Only remember/cognify when you intentionally add durable knowledge.
EOF
  exit 2
fi

if [ -z "${COGNEE_API_KEY:-}" ] && [ -f "$HOME/.cognee-plugin/api_key.json" ]; then
  COGNEE_API_KEY="$(python3 -c 'import json;from pathlib import Path;d=json.loads(Path.home().joinpath(".cognee-plugin/api_key.json").read_text());print(d.get("api_key")or d.get("key")or"")')"
  export COGNEE_API_KEY
fi
: "${COGNEE_API_KEY:?set COGNEE_API_KEY or api_key.json}"

MAX_DOCS="${COGNEE_BULK_MAX_DOCS:-3}"
MAX_CHARS="${COGNEE_BULK_MAX_CHARS:-4000}"

DOCS=(
  "$ROOT/docs/ECOSYSTEM.md"
  "$ROOT/.planning/cognee-omniroute-results.md"
  "$HOME/dev/wow-my-zsh/shared/AGENTS.md"
  "$ROOT/README.md"
  "$ROOT/docs/BEST-PRACTICES.md"
  "$HOME/dev/wow-my-zsh/docs/token-discipline-clis.md"
)

echo "COST WARNING: each remember ≈ 1+ Terra cognify pass. max_docs=$MAX_DOCS max_chars=$MAX_CHARS"
echo "Press Ctrl-C within 3s to abort..."
sleep 3

ok=0
fail=0
n=0
for f in "${DOCS[@]}"; do
  [ -f "$f" ] || continue
  n=$((n + 1))
  if [ "$n" -gt "$MAX_DOCS" ]; then
    echo "stop at COGNEE_BULK_MAX_DOCS=$MAX_DOCS"
    break
  fi
  echo "======== ($n/$MAX_DOCS) $(basename "$f") ========"
  tmp=$(mktemp)
  python3 -c "
from pathlib import Path
src=Path(r'''$f''')
cap=int('''$MAX_CHARS''')
Path(r'''$tmp''').write_text('# Source: '+str(src)+'\n\n'+src.read_text(errors='replace')[:cap])
"
  if python3 "$ROOT/bin/cognee-fleet-cli.py" remember --file "$tmp" -d "$COGNEE_PLUGIN_DATASET"; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1))
  fi
  rm -f "$tmp"
  sleep 2
done

echo "BULK done ok=$ok fail=$fail (OmniRoute credit used for cognify)"
# Recall only if already have data — still may use GLM 5.2 for graph_completion
if [ "${COGNEE_SKIP_RECALL_AFTER_BULK:-0}" = "1" ]; then
  echo "skip post-bulk recall"
else
  echo "post-bulk recall (uses Terra for answer generation — set COGNEE_SKIP_RECALL_AFTER_BULK=1 to skip):"
  python3 "$ROOT/bin/cognee-fleet-cli.py" recall "What are the L2 core always_on MCP servers?" || true
fi
