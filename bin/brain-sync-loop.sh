#!/usr/bin/env bash
# Brain Sync Loop — runs controlled gbrain → Cognee export every 30 minutes.
# Usage: bin/brain-sync-loop.sh [interval_seconds]
# Default: 1800 (30 min)
#
# Start in background: nohup bin/brain-sync-loop.sh &
# Stop: kill $(cat /tmp/brain-sync-loop.pid)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERVAL="${1:-1800}"
PIDFILE="/tmp/brain-sync-loop.pid"

echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE; exit 0" INT TERM

echo "brain-sync-loop: starting (interval=${INTERVAL}s, pid=$$)"

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M')] brain-sync: running gbrain → Cognee export..."
  OPENAI_BASE_URL=http://127.0.0.1:8012/v1 python3 "$ROOT/bin/brain-sync.py" export 2>&1 | tail -3

  echo "[$(date '+%Y-%m-%d %H:%M')] brain-sync: sleeping ${INTERVAL}s..."
  sleep "$INTERVAL"
done
