#!/usr/bin/env bash
# Source before starting Cognee (or use bin/cognee-start-omniroute.sh / cognee-fleet-up.sh).
#
# Architecture:
#   LLM (cognify/recall answers): OmniRoute :20128 → Vercel AI Gateway / OpenAI GPT-4.1
#   Embeddings (vectors):         NVIDIA NIM nv-embedqa-e5-v5 @ 1024-dim (free ~40 RPM)
#                                 fallback: COGNEE_EMBED_BACKEND=gemini|fastembed
#
# Why a pinned verified route for Cognee?
#   Cognee's structured-output extraction is sensitive to aliases that can drift
#   onto unavailable or empty-response models. Keep the provider behind OmniRoute,
#   but pin the upstream model to a freshly live-probed structured-chat-capable
#   route. Change this only after a non-empty chat + cognify probe.
#
# Cost:
#   - LLM usage follows the connected Vercel AI Gateway/OpenAI account quota
#   - NVIDIA NIM embed: free tier (~40 RPM)
#   - Bulk: requires COGNEE_ALLOW_COSTLY=1 (see docs/COGNEE-COST-POLICY.md)
#
# Prerequisites:
#   - OmniRoute on :20128 with at least one healthy chat provider in auto/best-free
#   - OMNIROUTE_MASTER_KEY injected by SIN-Infisical or set in the runtime
#   - NVIDIA_API_KEY only when COGNEE_EMBED_BACKEND=nim
#   - cognee venv (plugin venv)

set -euo pipefail

# Local memory/LLM sidecars must never traverse inherited HTTP proxy settings.
# Keep any existing bypass list and add all loopback spellings used by this stack.
_NO_PROXY_BASE="${NO_PROXY:-${no_proxy:-}}"
for _host in 127.0.0.1 localhost ::1; do
  case ",${_NO_PROXY_BASE}," in
    *",${_host},"*) ;;
    *) _NO_PROXY_BASE="${_NO_PROXY_BASE:+${_NO_PROXY_BASE},}${_host}" ;;
  esac
done
export NO_PROXY="$_NO_PROXY_BASE"
export no_proxy="$_NO_PROXY_BASE"
unset _NO_PROXY_BASE _host

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

# ── LLM (pinned verified route through OmniRoute) ────────────────────
# Live verification on 2026-08-23: `vercel-ai-gateway/openai/gpt-4.1`
# returned a real non-empty JSON completion through OmniRoute in ~2.2s.
# The previous NVIDIA `z-ai/glm-5.2` route reached EOL on 2026-08-21,
# and `auto/best-free` currently has no reliable bounded latency. Override only
# after a fresh non-empty chat + cognify probe.
export LLM_PROVIDER=openai
export LLM_MODEL="${LLM_MODEL:-openai/vercel-ai-gateway/openai/gpt-4.1}"
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
if { [ "$COGNEE_EMBED_BACKEND" = "nim" ] || [ "$COGNEE_EMBED_BACKEND" = "nvidia" ]; } \
  && [ -z "${NVIDIA_API_KEY:-}" ]; then
  echo "warn: NVIDIA_API_KEY not set; falling back to Gemini/local embedding proxy" >&2
  COGNEE_EMBED_BACKEND="gemini"
fi
case "$COGNEE_EMBED_BACKEND" in
  nim|nvidia)
    export EMBEDDING_PROVIDER=openai_compatible
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-nvidia/nemotron-3-embed-1b}"
    export EMBEDDING_ENDPOINT="${EMBEDDING_ENDPOINT:-http://127.0.0.1:8012/v1}"
    export EMBEDDING_API_KEY="${EMBEDDING_API_KEY:-local-nim-proxy}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
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
