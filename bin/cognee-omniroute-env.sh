#!/usr/bin/env bash
# Source before starting Cognee (or use bin/cognee-start-omniroute.sh / cognee-fleet-up.sh).
#
# Architecture (correct split):
#   LLM (cognify/recall answers): OmniRoute → Boundless → gpt-5.6-terra
#                                 model id for litellm: openai/boundless/gpt-5.6-terra
#   Embeddings (vectors):         DEFAULT Gemini via local proxy :8012
#                                 (gemini-embedding-001 @ 1024-dim free tier)
#                                 auto-fallback → local mxbai-embed-large on rate limit
#                                 optional: COGNEE_EMBED_BACKEND=fastembed|nim
#
# Cost:
#   - Gemini embed free tier (rate-limited); local fallback free unlimited
#   - LLM Terra: Boundless credit only on remember/cognify/graph_completion
#   - Bulk: requires COGNEE_ALLOW_COSTLY=1 (see docs/COGNEE-COST-POLICY.md)
#
# Prerequisites:
#   - OmniRoute on :20128 (for LLM)
#   - Boundless key active on OmniRoute for terra
#   - Gemini key in ~/.cognee-plugin/secrets/gemini_api_key (chmod 600)
#   - embed-proxy on :8012 (bin/cognee-start-embed-proxy.sh)
#   - cognee venv with fastembed installed (plugin venv)

set -euo pipefail

if [ -z "${OMNIROUTE_MASTER_KEY:-}" ] && [ -f "$HOME/.omniroute/.env" ]; then
  OMNIROUTE_MASTER_KEY="$(
    python3 -c '
from pathlib import Path
for line in Path.home().joinpath(".omniroute/.env").read_text().splitlines():
    if line.startswith("OMNIROUTE_MASTER_KEY="):
        print(line.split("=", 1)[1].strip().strip("\"'\''"))
        break
'
  )"
fi

if [ -z "${OMNIROUTE_MASTER_KEY:-}" ]; then
  echo "error: OMNIROUTE_MASTER_KEY not set and not found in ~/.omniroute/.env" >&2
  return 1 2>/dev/null || exit 1
fi
export OMNIROUTE_MASTER_KEY
export OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}"

# ── LLM (Boundless via OmniRoute) ─────────────────────────────────────
export LLM_PROVIDER=openai
export LLM_MODEL="${LLM_MODEL:-openai/boundless/gpt-5.6-terra}"
export LLM_ENDPOINT="$OMNIROUTE_BASE_URL"
export LLM_API_KEY="$OMNIROUTE_MASTER_KEY"
export OPENAI_API_KEY="$OMNIROUTE_MASTER_KEY"
export OPENAI_API_BASE="$OMNIROUTE_BASE_URL"
export OPENAI_BASE_URL="$OMNIROUTE_BASE_URL"

# ── Embeddings ───────────────────────────────────────────────────────
# Default: Gemini (quality) via embed-proxy with local mxbai fallback.
# Dim MUST stay 1024 for both backends so Lance + fallback stay compatible.
# Switch: COGNEE_EMBED_BACKEND=gemini|fastembed|nim
COGNEE_EMBED_BACKEND="${COGNEE_EMBED_BACKEND:-gemini}"
COGNEE_EMBED_PROXY_URL="${COGNEE_EMBED_PROXY_URL:-http://127.0.0.1:8012/v1}"
case "$COGNEE_EMBED_BACKEND" in
  gemini|proxy)
    export EMBEDDING_PROVIDER=openai_compatible
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-gemini-embedding-001}"
    export EMBEDDING_ENDPOINT="$COGNEE_EMBED_PROXY_URL"
    # Proxy ignores the key; any non-empty value satisfies OpenAI client
    export EMBEDDING_API_KEY="${EMBEDDING_API_KEY:-local-embed-proxy}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
    ;;
  nim|nvidia)
    export EMBEDDING_PROVIDER=openai_compatible
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-nvidia/nv-embedqa-e5-v5}"
    export EMBEDDING_ENDPOINT="$OMNIROUTE_BASE_URL"
    export EMBEDDING_API_KEY="$OMNIROUTE_MASTER_KEY"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
    ;;
  fastembed|local|mxbai)
    export EMBEDDING_PROVIDER=fastembed
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-mixedbread-ai/mxbai-embed-large-v1}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
    unset EMBEDDING_ENDPOINT EMBEDDING_API_KEY 2>/dev/null || true
    ;;
  *)
    echo "error: unknown COGNEE_EMBED_BACKEND=$COGNEE_EMBED_BACKEND (gemini|fastembed|nim)" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

# Skip litellm connection tests that time out through OmniRoute
export COGNEE_SKIP_CONNECTION_TEST="${COGNEE_SKIP_CONNECTION_TEST:-true}"

export COGNEE_PLUGIN_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
export COGNEE_BASE_URL="${COGNEE_BASE_URL:-http://127.0.0.1:8011}"
export COGNEE_LOCAL_API_URL="${COGNEE_LOCAL_API_URL:-http://127.0.0.1:8011}"

echo "cognee-env: LLM=$LLM_MODEL via OmniRoute | EMBED=$COGNEE_EMBED_BACKEND model=$EMBEDDING_MODEL dims=$EMBEDDING_DIMENSIONS${EMBEDDING_ENDPOINT:+ endpoint=$EMBEDDING_ENDPOINT}"
