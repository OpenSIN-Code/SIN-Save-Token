#!/usr/bin/env bash
# Wipe sin-fleet vector+graph storage so embed dims can change safely.
#
# Why: Lance tables are fixed_size_list[N]. Switching
#   NIM (1024) ↔ fastembed (384) without wipe → broken search / embed errors.
#
# Does NOT call Boundless. Does NOT delete API keys or dataset registry rows.
# After wipe: restart Cognee, then re-remember short notes you care about.
#
# Usage:
#   bin/cognee-reindex-vectors.sh          # backup + wipe + restart
#   bin/cognee-reindex-vectors.sh --wipe-only
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIPE_ONLY=0
[ "${1:-}" = "--wipe-only" ] && WIPE_ONLY=1

# Default dataset path used by local plugin multi-user layout
# ownerId/datasetId from pilot: 16443e40-... / 6282f296-...
SYSTEM_DB_ROOT="${COGNEE_SYSTEM_DB_ROOT:-$HOME/.cognee-plugin/venv/lib/python3.12/site-packages/cognee/.cognee_system/databases}"
OWNER_GLOB="$SYSTEM_DB_ROOT"/*
BACKUP="$HOME/.cognee/backups/sin-fleet-reindex-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"

# Stop API if listening
if command -v lsof >/dev/null 2>&1; then
  OLD="$(lsof -tiTCP:8011 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$OLD" ]; then
    echo "stopping cognee PID $OLD"
    kill "$OLD" 2>/dev/null || true
    sleep 2
  fi
fi

found=0
for owner in $OWNER_GLOB; do
  [ -d "$owner" ] || continue
  for path in "$owner"/*.lance.db "$owner"/*.lbug; do
    [ -e "$path" ] || continue
    found=1
    echo "backup → $BACKUP/$(basename "$path")"
    cp -a "$path" "$BACKUP/"
    rm -rf "$path"
    echo "wiped $path"
  done
done

if [ "$found" -eq 0 ]; then
  echo "nothing to wipe under $SYSTEM_DB_ROOT (already clean?)"
else
  echo "backup at $BACKUP"
fi

if [ "$WIPE_ONLY" -eq 1 ]; then
  echo "wipe-only done — start with bin/cognee-start-omniroute.sh"
  exit 0
fi

# shellcheck disable=SC1091
source "$ROOT/bin/cognee-omniroute-env.sh"
"$ROOT/bin/cognee-start-omniroute.sh"
echo "reindex ready: vectors will be recreated on next remember (dims=$EMBEDDING_DIMENSIONS backend=${COGNEE_EMBED_BACKEND:-fastembed})"
echo "re-seed short durable notes with: cognee-remember 'your fact'"
