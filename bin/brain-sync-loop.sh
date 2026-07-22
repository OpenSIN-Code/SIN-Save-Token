#!/usr/bin/env bash
# Brain Sync Loop — runs gbrain ↔ Cognee sync every 30 minutes.
# Usage: bin/brain-sync-loop.sh [interval_seconds]
# Default: 1800 (30 min)
#
# Start in background: nohup bin/brain-sync-loop.sh &
# Stop: kill $(cat /tmp/brain-sync-loop.pid)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERVAL="${1:-1800}"
PIDFILE="/tmp/brain-sync-loop.pid"

# Write PID for stop
echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE; exit 0" INT TERM

echo "brain-sync-loop: starting (interval=${INTERVAL}s, pid=$$)"
echo "$$" > "$PIDFILE"

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M')] brain-sync: running gbrain2cognee..."
  OPENAI_BASE_URL=http://127.0.0.1:8012/v1 python3 "$ROOT/bin/brain-sync.py" gbrain2cognee 2>&1 | tail -3

  echo "[$(date '+%Y-%m-%d %H:%M')] brain-sync: running cognee2gbrain..."
  OPENAI_BASE_URL=http://127.0.0.1:8012/v1 python3 "$ROOT/bin/brain-sync.py" cognee2gbrain 2>&1 | tail -3

  echo "[$(date '+%Y-%m-%d %H:%M')] brain-sync: sleeping ${INTERVAL}s..."
  sleep "$INTERVAL"
done
