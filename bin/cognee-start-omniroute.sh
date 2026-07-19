#!/usr/bin/env bash
# Start local Cognee API on :8011.
# LLM: OmniRoute → Boundless gpt-5.6-terra
# Embed: Gemini via :8012 proxy (default) with local mxbai fallback
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/bin/cognee-omniroute-env.sh"

# Require OmniRoute
if ! curl -sS -m 2 -o /dev/null -w '' "$OMNIROUTE_BASE_URL/models" \
  -H "Authorization: Bearer $OMNIROUTE_MASTER_KEY" 2>/dev/null; then
  # models needs GET with auth — just check TCP
  if ! curl -sS -m 2 -o /dev/null "http://127.0.0.1:20128/" 2>/dev/null; then
    echo "error: OmniRoute not reachable on :20128 — start: omniroute serve" >&2
    exit 1
  fi
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
    echo "stopping PID $OLD on :8011"
    kill "$OLD" 2>/dev/null || true
    sleep 2
  fi
fi

LOG_DIR="$HOME/.cognee-plugin/logs"
mkdir -p "$LOG_DIR"
nohup "$VENV_PY" -m uvicorn cognee.api.client:app --host 127.0.0.1 --port 8011 \
  >"$LOG_DIR/server.log" 2>&1 &
echo "started cognee pid=$! log=$LOG_DIR/server.log"

for i in $(seq 1 40); do
  if curl -sS -m 2 http://127.0.0.1:8011/health 2>/dev/null | grep -q healthy; then
    echo "healthy: $(curl -sS -m 2 http://127.0.0.1:8011/health)"
    exit 0
  fi
  sleep 1
done
echo "error: cognee did not become healthy" >&2
tail -30 "$LOG_DIR/server.log" >&2 || true
exit 1
