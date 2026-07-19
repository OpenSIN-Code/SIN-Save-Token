#!/usr/bin/env bash
# Source this file before starting Cognee so LLM + embeddings go through OmniRoute.
#
#   source bin/cognee-omniroute-env.sh
#   # then start uvicorn / plugin session-start
#
# Routing:
#   LLM:   OmniRoute → BoundlessAPI → gpt-5.6-terra   (model id: boundless/gpt-5.6-terra)
#   Embed: OmniRoute → NVIDIA NIM  → nv-embedqa-e5-v5 (model id: nvidia/nv-embedqa-e5-v5)
#
# Prerequisites:
#   - OmniRoute running on http://127.0.0.1:20128
#   - Boundless node + key active; gpt-5.6-terra registered
#   - NVIDIA NIM connection active; embed model reachable

set -euo pipefail

# Master key from OmniRoute install (never commit this file's resolved values)
if [ -z "${OMNIROUTE_MASTER_KEY:-}" ] && [ -f "$HOME/.omniroute/.env" ]; then
  # shellcheck disable=SC1090
  set -a
  # only export the master key line
  OMNIROUTE_MASTER_KEY="$(
    python3 -c '
from pathlib import Path
for line in Path.home().joinpath(".omniroute/.env").read_text().splitlines():
    if line.startswith("OMNIROUTE_MASTER_KEY="):
        print(line.split("=", 1)[1].strip().strip("\"'\''"))
        break
'
  )"
  set +a
fi

if [ -z "${OMNIROUTE_MASTER_KEY:-}" ]; then
  echo "error: OMNIROUTE_MASTER_KEY not set and not found in ~/.omniroute/.env" >&2
  return 1 2>/dev/null || exit 1
fi

export OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}"

# ── LLM (chat / cognify) ─────────────────────────────────────────────
export LLM_PROVIDER=openai
export LLM_MODEL="${LLM_MODEL:-boundless/gpt-5.6-terra}"
export LLM_ENDPOINT="$OMNIROUTE_BASE_URL"
export LLM_API_KEY="$OMNIROUTE_MASTER_KEY"
# litellm / openai-sdk aliases used by some cognee paths
export OPENAI_API_KEY="$OMNIROUTE_MASTER_KEY"
export OPENAI_API_BASE="$OMNIROUTE_BASE_URL"
export OPENAI_BASE_URL="$OMNIROUTE_BASE_URL"

# ── Embeddings (NVIDIA NIM via OmniRoute) ────────────────────────────
# openai_compatible uses the AsyncOpenAI client against /v1/embeddings
export EMBEDDING_PROVIDER=openai_compatible
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-nvidia/nv-embedqa-e5-v5}"
export EMBEDDING_ENDPOINT="$OMNIROUTE_BASE_URL"
export EMBEDDING_API_KEY="$OMNIROUTE_MASTER_KEY"
export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"

# Connection test to embed endpoint can be slow on cold NIM — optional skip
export COGNEE_SKIP_CONNECTION_TEST="${COGNEE_SKIP_CONNECTION_TEST:-false}"

# Pilot dataset default
export COGNEE_PLUGIN_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
export COGNEE_BASE_URL="${COGNEE_BASE_URL:-http://127.0.0.1:8011}"
export COGNEE_LOCAL_API_URL="${COGNEE_LOCAL_API_URL:-http://127.0.0.1:8011}"

echo "cognee-omniroute-env: LLM=$LLM_MODEL  EMBED=$EMBEDDING_MODEL  via $OMNIROUTE_BASE_URL"
