#!/usr/bin/env bash
# RETIRED: automatic periodic memory replication is incompatible with the
# OpenViking-first single-writer architecture.
set -euo pipefail

echo "brain-sync-loop: retired; automatic gbrain/Cognee replication is disabled." >&2
echo "Use an explicit curated export instead: python3 bin/brain-sync.py export [--dry-run]" >&2
exit 2
