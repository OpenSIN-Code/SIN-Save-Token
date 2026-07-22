#!/usr/bin/env bash
# Idempotent rollout and verification of the token-minimal local stack.
# Run explicitly from a normal terminal; it modifies live agent configs through
# the canonical installers, never by hand.
set -euo pipefail

SST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WOW_ROOT="${WOW_HOME:-$HOME/dev/wow-my-zsh}"
GLOBAL_BRAIN_ROOT="${GLOBAL_BRAIN_HOME:-$HOME/dev/global-brain}"

require_file() {
  [ -f "$1" ] || { echo "missing required file: $1" >&2; exit 2; }
}

require_file "$WOW_ROOT/install.sh"
require_file "$WOW_ROOT/doctor.sh"
require_file "$GLOBAL_BRAIN_ROOT/package.json"
require_file "$SST_ROOT/bin/verify-tokens"

# The Mac filesystem bridge writes text as 0644. Repair all executable entry
# points before invoking them or allowing live symlinks to resolve to them.
chmod +x \
  "$SST_ROOT/bin/sin-context" \
  "$SST_ROOT/bin/verify-tokens" \
  "$SST_ROOT/bin/e2e-memory-test.sh" \
  "$SST_ROOT/bin/benchmark-context" \
  "$SST_ROOT/bin/audit-token-architecture.py" \
  "$SST_ROOT/bin/apply-token-minimal-local.sh" \
  "$WOW_ROOT/install.sh" \
  "$WOW_ROOT/doctor.sh" \
  "$WOW_ROOT/bin/mcp-audit" \
  "$GLOBAL_BRAIN_ROOT/.opencode/hooks/pcpm-before-run.sh" \
  "$GLOBAL_BRAIN_ROOT/.opencode/hooks/pcpm-after-run.sh"

case "$WOW_ROOT" in
  */.claude/worktrees/*)
    echo "refusing rollout from transient Claude worktree: $WOW_ROOT" >&2
    exit 2
    ;;
esac
case ":${PATH:-}:" in
  *"/.claude/worktrees/"*)
    echo "refusing rollout while PATH contains a transient Claude worktree" >&2
    exit 2
    ;;
esac

echo "[1/7] Static syntax, schema and test suites"
bash -n \
  "$SST_ROOT/bin/apply-token-minimal-local.sh" \
  "$SST_ROOT/bin/e2e-memory-test.sh" \
  "$SST_ROOT/bin/verify-tokens" \
  "$WOW_ROOT/install.sh" \
  "$WOW_ROOT/doctor.sh" \
  "$GLOBAL_BRAIN_ROOT/.opencode/hooks/pcpm-before-run.sh" \
  "$GLOBAL_BRAIN_ROOT/.opencode/hooks/pcpm-after-run.sh"
python3 -m py_compile \
  "$SST_ROOT/bin/sin-context" \
  "$SST_ROOT/bin/benchmark-context" \
  "$SST_ROOT/bin/audit-token-architecture.py" \
  "$SST_ROOT/lib/sin_context/provider_runtime.py" \
  "$WOW_ROOT/bin/mcp-audit"
python3 -m json.tool "$SST_ROOT/config/context-policy.json" >/dev/null
python3 -m json.tool "$SST_ROOT/config/provider-runtime.json" >/dev/null
python3 -m json.tool "$WOW_ROOT/shared/mcp/servers.json" >/dev/null
python3 -m json.tool "$GLOBAL_BRAIN_ROOT/.opencode/pcpm-config.json" >/dev/null
python3 "$SST_ROOT/bin/audit-token-architecture.py" \
  --sst "$SST_ROOT" \
  --wow "$WOW_ROOT" \
  --global-brain "$GLOBAL_BRAIN_ROOT"
python3 -m unittest discover -s "$SST_ROOT/tests" -p 'test_*.py'
python3 "$WOW_ROOT/scripts/test_gen_mcp.py"
(
  cd "$GLOBAL_BRAIN_ROOT"
  npm test
  node src/cli.js setup-hooks \
    --project-root "$GLOBAL_BRAIN_ROOT" \
    --project global-brain \
    --goal-id default-goal \
    --description "Continue development" >/dev/null
)

echo "[2/7] Install canonical SST binaries and hooks"
bash "$SST_ROOT/bin/install.sh" --heal

echo "[3/7] Converge all managed MCP configs to minimal"
WOW_MCP_PROFILE=minimal bash "$WOW_ROOT/install.sh" --profile minimal

echo "[4/7] Verify live config ownership and worktree shadowing"
WOW_MCP_PROFILE=minimal bash "$WOW_ROOT/doctor.sh"

echo "[5/7] Verify four-layer token policy"
WOW_MCP_PROFILE=minimal bash "$SST_ROOT/bin/verify-tokens"

echo "[6/7] Run non-polluting memory/context E2E gate"
bash "$SST_ROOT/bin/e2e-memory-test.sh"

echo "[7/7] Produce a non-claimable local SST smoke benchmark"
python3 "$SST_ROOT/bin/benchmark-context" --allow-partial

echo
echo "ROLLOUT COMPLETE"
echo "For a claimable A/B/C report, set SST_BENCH_BASELINE_COMMAND and"
echo "SST_BENCH_FULL_STACK_COMMAND, then rerun benchmark-context without --allow-partial."
