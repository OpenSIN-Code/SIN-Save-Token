#!/usr/bin/env bash
# Non-polluting E2E gate for the token-minimal context and memory architecture.
# It never writes test facts to Cognee and never requires memory MCPs globally.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WOW_ROOT="${WOW_HOME:-$HOME/dev/wow-my-zsh}"
GLOBAL_BRAIN_ROOT="${GLOBAL_BRAIN_HOME:-$HOME/dev/global-brain}"
PASS=0
FAIL=0
TMP_HOME="$(mktemp -d -t sst-e2e.XXXXXX)"
trap 'rm -rf "$TMP_HOME"' EXIT

ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

echo "=== SIN token-minimal E2E gate ==="

# 1. Runtime services. These checks are read-only.
echo
echo "[1/7] Runtime services"
curl -sS -m 2 http://127.0.0.1:20128/ >/dev/null 2>&1 \
  && ok "OmniRoute reachable on :20128" \
  || fail "OmniRoute not reachable on :20128"
curl -sS -m 2 http://127.0.0.1:8012/health 2>/dev/null | grep -q '"status"' \
  && ok "NIM proxy reachable on :8012" \
  || fail "NIM proxy not reachable on :8012"
curl -sS -m 2 http://127.0.0.1:8011/health 2>/dev/null | grep -qi 'healthy\|"status"' \
  && ok "Cognee reachable on :8011" \
  || fail "Cognee not healthy on :8011"

# 2. Broker policy and runtime configuration.
echo
echo "[2/7] sin-context policy"
python3 - "$ROOT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
policy = json.loads((root / "config/context-policy.json").read_text())
runtime = json.loads((root / "config/provider-runtime.json").read_text())
routes = policy["routes"]
routed = {provider for route in routes for provider in route["providers"]}
configured = set(runtime["providers"])
assert int(policy["retrieval"]["maximum_provider_attempts"]) <= 2
symbol = next(route for route in routes if route["name"] == "code_symbol")
assert symbol["providers"][:2] == ["simone", "graphify"]
assert routed <= configured, sorted(routed - configured)
assert int(policy["budgets"]["maximum_tokens"]) <= 1600
PY
[ "$?" -eq 0 ] && ok "routes, budgets and runtime specs are consistent" || fail "sin-context policy drift"

# 3. One-way curated sync. Use an isolated HOME so even the local ledger is disposable.
echo
echo "[3/7] Memory boundary"
SYNC_STATUS="$(HOME="$TMP_HOME" python3 "$ROOT/bin/brain-sync.py" status 2>/dev/null)"
echo "$SYNC_STATUS" | grep -q '"direction": "gbrain -> cognee"' \
  && ok "gbrain export direction is one-way" \
  || fail "gbrain export direction is wrong"
echo "$SYNC_STATUS" | grep -q '"automatic_reverse_sync": false' \
  && ok "automatic reverse sync is disabled" \
  || fail "automatic reverse sync is enabled or undocumented"
HOME="$TMP_HOME" python3 "$ROOT/bin/sin-memory-write" \
  "Verified benchmark dry-run entry that must never reach durable memory." \
  --type verified_fact --scope test --source e2e --dry-run >/dev/null 2>&1 \
  && ok "curated memory writer validates in dry-run mode" \
  || fail "curated memory writer dry-run failed"

# 4. Port and profile consistency in wow-my-zsh.
echo
echo "[4/7] MCP registry"
python3 - "$WOW_ROOT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
registry = json.loads((root / "shared/mcp/servers.json").read_text())
profiles = json.loads((root / "shared/mcp/task-profiles.json").read_text())
assert registry["_budget"]["default_profile"] == "minimal"
assert registry["servers"]["cognee"]["url"] == "http://127.0.0.1:8011/mcp"
assert profiles["profiles"]["minimal"]["maximum_servers"] == 0
assert max(p["maximum_servers"] for p in profiles["profiles"].values()) <= 2
PY
[ "$?" -eq 0 ] && ok "minimal profile, Cognee port and task caps are consistent" || fail "wow MCP registry drift"

# 5. global-brain must remain an on-demand archive, not an automatic injector.
echo
echo "[5/7] global-brain isolation"
python3 - "$GLOBAL_BRAIN_ROOT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
config = json.loads((root / ".opencode/pcpm-config.json").read_text())
pcpm = config["pcpm"]
assert pcpm["autoInjectContext"] is False
assert pcpm["autoSync"] is False
assert pcpm["extractKnowledge"] is False
assert pcpm["canonicalMemoryProvider"] == "cognee"
before = (root / ".opencode/hooks/pcpm-before-run.sh").read_text()
after = (root / ".opencode/hooks/pcpm-after-run.sh").read_text()
assert "PCPM_AUTO_INJECT" in before
assert "PCPM_AUTO_EXTRACT" in after
assert "PCPM_AUTO_SYNC" in after
assert "sync-chat-turn" not in after
PY
[ "$?" -eq 0 ] && ok "global-brain hooks are opt-in and Cognee remains canonical" || fail "global-brain automatic injection/write loop detected"

# 6. Read-only retrieval smoke checks. No remember/write call is made.
echo
echo "[6/7] Retrieval smoke"
if command -v gbrain >/dev/null 2>&1; then
  gbrain stats >/dev/null 2>&1 && ok "gbrain stats" || fail "gbrain stats failed"
else
  fail "gbrain not on PATH"
fi
if command -v cognee-recall >/dev/null 2>&1; then
  cognee-recall "canonical durable memory" >/dev/null 2>&1 \
    && ok "Cognee recall" \
    || fail "Cognee recall failed"
else
  fail "cognee-recall not on PATH"
fi

# 7. Benchmark assets must exist and contain no fabricated measurements.
echo
echo "[7/7] Benchmark gate"
[ -f "$ROOT/bin/benchmark-context" ] && ok "benchmark harness exists" || fail "benchmark harness missing"
[ -f "$ROOT/config/benchmark-tasks.json" ] && ok "benchmark task set exists" || fail "benchmark task set missing"
grep -q 'claimable_abc_comparison' "$ROOT/bin/benchmark-context" \
  && ok "partial reports are marked non-claimable" \
  || fail "benchmark claimability guard missing"
python3 "$ROOT/bin/audit-token-architecture.py" \
  --sst "$ROOT" \
  --wow "$WOW_ROOT" \
  --global-brain "$GLOBAL_BRAIN_ROOT" >/dev/null 2>&1 \
  && ok "cross-repository architecture audit" \
  || fail "cross-repository architecture drift"

echo
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
[ "$FAIL" -eq 0 ] && echo "STATUS: PASS" && exit 0
echo "STATUS: FAIL"
exit 1
