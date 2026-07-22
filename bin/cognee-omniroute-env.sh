#!/usr/bin/env bash
# Source before starting Cognee (or use bin/cognee-start-omniroute.sh / cognee-fleet-up.sh).
#
# Architecture:
#   LLM (cognify/recall answers): qoder-proxy :8013 → qodercli → Qwen 3.8
#   Embeddings (vectors):         NVIDIA NIM nv-embedqa-e5-v5 @ 1024-dim (free ~40 RPM)
#                                 fallback: COGNEE_EMBED_BACKEND=gemini|fastembed
#
# Cost:
#   - Qoder subscription (Qwen 3.8) for LLM cognify/recall
#   - NVIDIA NIM embed: free tier (~40 RPM)
#   - Bulk: requires COGNEE_ALLOW_COSTLY=1 (see docs/COGNEE-COST-POLICY.md)
#
# Prerequisites:
#   - qoder-proxy on :8013 (cd ~/dev/qoder-proxy && npm start)
#   - qodercli logged in + PAT in qoder-proxy/.env
#   - NVIDIA_API_KEY in env (free from build.nvidia.com)
#   - cognee venv (plugin venv)

set -euo pipefail

QODER_PROXY_URL="${QODER_PROXY_URL:-http://127.0.0.1:8013/v1}"

# ── LLM (Qwen 3.8 via qoder-proxy) ──────────────────────────────────
export LLM_PROVIDER=openai
export LLM_MODEL="${LLM_MODEL:-openai/qwen3.8-max-preview}"
export LLM_ENDPOINT="$QODER_PROXY_URL"
export LLM_API_KEY="${QODER_PROXY_API_KEY:-local-qoder-proxy}"
export OPENAI_API_KEY="$LLM_API_KEY"
export OPENAI_API_BASE="$QODER_PROXY_URL"
export OPENAI_BASE_URL="$QODER_PROXY_URL"

# ── Embeddings ───────────────────────────────────────────────────────
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

echo "cognee-env: LLM=$LLM_MODEL via qoder-proxy :8013 | EMBED=$COGNEE_EMBED_BACKEND model=$EMBEDDING_MODEL dims=$EMBEDDING_DIMENSIONS${EMBEDDING_ENDPOINT:+ endpoint=$EMBEDDING_ENDPOINT}"
