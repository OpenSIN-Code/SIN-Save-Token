#!/usr/bin/env bash
# Source before starting Cognee (or use bin/cognee-start-omniroute.sh / cognee-fleet-up.sh).
#
# Architecture:
#   LLM (cognify/recall answers): OmniRoute :20128 → vag/zai/glm-5.2
#   Embeddings (vectors):         NVIDIA NIM nv-embedqa-e5-v5 @ 1024-dim (free ~40 RPM)
#                                 fallback: COGNEE_EMBED_BACKEND=gemini|fastembed
#
# Cost:
#   - GLM 5.2 via Vercel AI Gateway (free tier or OmniRoute pool)
#   - NVIDIA NIM embed: free tier (~40 RPM)
#   - Bulk: requires COGNEE_ALLOW_COSTLY=1 (see docs/COGNEE-COST-POLICY.md)
#
# Prerequisites:
#   - OmniRoute on :20128 with Vercel AI Gateway provider
#   - OMNIROUTE_MASTER_KEY set or in ~/.omniroute/.env
#   - NVIDIA_API_KEY in env (free from build.nvidia.com)
#   - cognee venv (plugin venv)

set -euo pipefail

# ── Load OmniRoute Master Key ──────────────────────────────────────────
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

OMNIROUTE_URL="${OMNIROUTE_URL:-http://127.0.0.1:20128}"

# ── LLM (GLM 5.2 via OmniRoute) ───────────────────────────────────────
export LLM_PROVIDER=openai
export LLM_MODEL="${LLM_MODEL:-openai/vag/zai/glm-5.2}"
export LLM_ENDPOINT="$OMNIROUTE_URL/v1"
export LLM_API_KEY="$OMNIROUTE_MASTER_KEY"
export OPENAI_API_KEY="$OMNIROUTE_MASTER_KEY"
export OPENAI_API_BASE="$OMNIROUTE_URL/v1"
export OPENAI_BASE_URL="$OMNIROUTE_URL/v1"

# ── Embeddings ─────────────────────────────────────────────────────────
# Dim MUST stay 1024 so Lance stays valid across backend switches.
# Default: NVIDIA NIM (free, reliable, no local proxy needed).
# Switch: COGNEE_EMBED_BACKEND=nim|gemini|fastembed
COGNEE_EMBED_BACKEND="${COGNEE_EMBED_BACKEND:-nim}"
case "$COGNEE_EMBED_BACKEND" in
  nim|nvidia)
    export EMBEDDING_PROVIDER=openai_compatible
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-nvidia/nemotron-3-embed-1b}"
    export EMBEDDING_ENDPOINT="${EMBEDDING_ENDPOINT:-http://127.0.0.1:8012/v1}"
    export EMBEDDING_API_KEY="${EMBEDDING_API_KEY:-local-nim-proxy}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-2048}"
    ;;
  gemini|proxy)
    export EMBEDDING_PROVIDER=openai_compatible
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-gemini-embedding-001}"
    export EMBEDDING_ENDPOINT="${COGNEE_EMBED_PROXY_URL:-http://127.0.0.1:8012/v1}"
    export EMBEDDING_API_KEY="${EMBEDDING_API_KEY:-local-embed-proxy}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
    ;;
  fastembed|local|mxbai)
    export EMBEDDING_PROVIDER=fastembed
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-mixedbread-ai/mxbai-embed-large-v1}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
    unset EMBEDDING_ENDPOINT EMBEDDING_API_KEY 2>/dev/null || true
    ;;
  *)
    echo "error: unknown COGNEE_EMBED_BACKEND=$COGNEE_EMBED_BACKEND (nim|gemini|fastembed)" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

export COGNEE_SKIP_CONNECTION_TEST="${COGNEE_SKIP_CONNECTION_TEST:-true}"

export COGNEE_PLUGIN_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
export COGNEE_BASE_URL="${COGNEE_BASE_URL:-http://127.0.0.1:8011}"
export COGNEE_LOCAL_API_URL="${COGNEE_LOCAL_API_URL:-http://127.0.0.1:8011}"

echo "cognee-env: LLM=$LLM_MODEL via OmniRoute :20128 | EMBED=$COGNEE_EMBED_BACKEND model=$EMBEDDING_MODEL dims=$EMBEDDING_DIMENSIONS${EMBEDDING_ENDPOINT:+ endpoint=$EMBEDDING_ENDPOINT}"
