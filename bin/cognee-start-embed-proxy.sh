#!/usr/bin/env bash
# Start Gemini→local embedding proxy on :8012 (OpenAI-compatible /v1/embeddings).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${COGNEE_EMBED_PROXY_HOST:-127.0.0.1}"
PORT="${COGNEE_EMBED_PROXY_PORT:-8012}"
VENV_PY="${COGNEE_VENV_PYTHON:-$HOME/.cognee-plugin/venv/bin/python}"
KEY_FILE="${GEMINI_API_KEY_FILE:-$HOME/.cognee-plugin/secrets/gemini_api_key}"
LOG_DIR="${COGNEE_EMBED_PROXY_LOG_DIR:-$HOME/.cognee-plugin/logs}"
mkdir -p "$LOG_DIR"

if [ ! -x "$VENV_PY" ]; then
  echo "error: cognee venv python missing: $VENV_PY" >&2
  exit 1
fi

if [ ! -f "$KEY_FILE" ]; then
  echo "warn: no Gemini key at $KEY_FILE — proxy will use local mxbai only" >&2
  echo "  store key:  umask 077; cat > $KEY_FILE  # paste key, Ctrl-D; chmod 600 $KEY_FILE" >&2
fi

# Free port if occupied by old proxy
if command -v lsof >/dev/null 2>&1; then
  OLD="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$OLD" ]; then
    echo "stopping old embed-proxy PID $OLD on :$PORT"
    kill "$OLD" 2>/dev/null || true
    sleep 1
  fi
fi

export COGNEE_EMBED_PROXY_HOST="$HOST"
export COGNEE_EMBED_PROXY_PORT="$PORT"
export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-1024}"
export GEMINI_EMBED_MODEL="${GEMINI_EMBED_MODEL:-gemini-embedding-001}"
export COGNEE_FALLBACK_EMBED_MODEL="${COGNEE_FALLBACK_EMBED_MODEL:-mixedbread-ai/mxbai-embed-large-v1}"
export GEMINI_API_KEY_FILE="$KEY_FILE"

nohup "$VENV_PY" "$ROOT/bin/cognee-embed-proxy.py" \
  >"$LOG_DIR/embed-proxy.log" 2>&1 &
echo "started embed-proxy pid=$! log=$LOG_DIR/embed-proxy.log"

for i in $(seq 1 30); do
  if curl -sS -m 2 "http://$HOST:$PORT/health" 2>/dev/null | grep -q '"status": "ok"\|"status":"ok"'; then
    curl -sS -m 2 "http://$HOST:$PORT/health"
    echo
    exit 0
  fi
  sleep 0.5
done
echo "error: embed-proxy did not become healthy" >&2
tail -40 "$LOG_DIR/embed-proxy.log" >&2 || true
exit 1
