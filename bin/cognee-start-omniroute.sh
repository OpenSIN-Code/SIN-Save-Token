#!/usr/bin/env bash
# Start local Cognee API on :8011.
# LLM: OmniRoute :20128 → vag/zai/glm-5.2
# Embed: NVIDIA NIM via :8012 proxy (default) with local mxbai fallback
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/bin/cognee-omniroute-env.sh"

# This fleet runtime is loopback-only and single-user. Cognee defaults newer
# releases to multi-tenant auth, which invalidates the local fleet CLI contract.
# Make the intended local posture explicit instead of minting/rotating API keys.
export ENABLE_BACKEND_ACCESS_CONTROL=false
export REQUIRE_AUTHENTICATION=false

# Require OmniRoute
if ! curl -sS -m 2 -o /dev/null "http://127.0.0.1:20128/" 2>/dev/null; then
  echo "error: OmniRoute not reachable on :20128 — start: omniroute serve" >&2
  exit 1
fi

# Embed proxy required when backend=gemini
if [ "${COGNEE_EMBED_BACKEND:-gemini}" = "gemini" ] || [ "${COGNEE_EMBED_BACKEND:-}" = "proxy" ]; then
  if ! curl -sS -m 2 http://127.0.0.1:8012/health 2>/dev/null | grep -q ok; then
    echo "starting embed-proxy (Gemini → local fallback)..."
    "$ROOT/bin/cognee-start-embed-proxy.sh"
  fi
fi

VENV_PY="${COGNEE_VENV_PYTHON:-$HOME/.cognee-plugin/venv/bin/python}"
if [ ! -x "$VENV_PY" ]; then
  echo "error: cognee venv missing at $VENV_PY (run plugin SessionStart once)" >&2
  exit 1
fi

# Free :8011 if occupied
if command -v lsof >/dev/null 2>&1; then
  OLD="$(lsof -tiTCP:8011 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$OLD" ]; then
    for PID in $OLD; do
      echo "stopping PID $PID on :8011"
      pkill -TERM -P "$PID" 2>/dev/null || true
      sleep 1
      kill "$PID" 2>/dev/null || true
      sleep 2
      if kill -0 "$PID" 2>/dev/null; then
        pkill -KILL -P "$PID" 2>/dev/null || true
        kill -KILL "$PID" 2>/dev/null || true
      fi
    done
  fi
fi

LOG_DIR="$HOME/.cognee-plugin/logs"
mkdir -p "$LOG_DIR"
nohup "$VENV_PY" -m uvicorn cognee.api.client:app --host 127.0.0.1 --port 8011 \
  >"$LOG_DIR/server.log" 2>&1 &
echo "started cognee pid=$! log=$LOG_DIR/server.log"

for i in $(seq 1 40); do
  if curl -sS -m 2 http://127.0.0.1:8011/health 2>/dev/null | grep -Eq '"health"[[:space:]]*:[[:space:]]*"healthy"'; then
    echo "healthy: $(curl -sS -m 2 http://127.0.0.1:8011/health)"
    exit 0
  fi
  sleep 1
done
echo "error: cognee did not become healthy" >&2
tail -30 "$LOG_DIR/server.log" >&2 || true
exit 1
