#!/usr/bin/env bash
# E2E-Test: Unified Memory Layer — gbrain + Cognee + Agent Integration
# Usage: bin/e2e-memory-test.sh
# Exit codes: 0=pass, 1=fail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

echo "=== E2E Memory Test ==="
echo ""

# ── 1. Services ──────────────────────────────────────────────────────────
echo "[1/6] Services"

curl -sS -m 2 http://127.0.0.1:20128/ >/dev/null 2>&1 && ok "OmniRoute on :20128" || fail "OmniRoute not reachable"
curl -sS -m 2 http://127.0.0.1:8012/health 2>/dev/null | grep -q '"status"' && ok "NIM Proxy on :8012" || fail "NIM Proxy not reachable"
curl -sS -m 2 http://127.0.0.1:8011/health 2>/dev/null | grep -q healthy && ok "Cognee on :8011" || fail "Cognee not healthy"

# ── 2. gbrain Stats ──────────────────────────────────────────────────────
echo ""
echo "[2/6] gbrain"
GBRAIN=$(OPENAI_BASE_URL=http://127.0.0.1:8012/v1 gbrain stats 2>/dev/null)
echo "$GBRAIN" | grep -q "Pages:" && ok "gbrain stats OK ($(echo "$GBRAIN" | grep Pages | awk '{print $2}') pages)" || fail "gbrain stats failed"
echo "$GBRAIN" | grep -q "Links:" && { L=$(echo "$GBRAIN" | grep Links | awk '{print $2}'); [ "$L" -gt 0 ] && ok "gbrain links: $L" || fail "gbrain links: 0"; } || fail "gbrain links check"

# ── 3. gbrain Search ─────────────────────────────────────────────────────
echo ""
echo "[3/6] gbrain Search"
SEARCH=$(OPENAI_BASE_URL=http://127.0.0.1:8012/v1 gbrain search "credentials" 2>/dev/null | head -1)
echo "$SEARCH" | grep -q "credentials" && ok "gbrain search works" || fail "gbrain search failed"

# ── 4. Cognee Remember + Recall ───────────────────────────────────────────
echo ""
echo "[4/6] Cognee"
source "$ROOT/bin/cognee-omniroute-env.sh" 2>/dev/null
REM=$(COGNEE_QUIET_COST=1 cognee-remember "E2E test: memory verification" 2>&1)
echo "$REM" | grep -q "200" && ok "Cognee remember" || fail "Cognee remember failed"
REC=$(cognee-recall "E2E test" 2>&1 | head -3)
echo "$REC" | grep -qi "test\|memory\|E2E" && ok "Cognee recall" || fail "Cognee recall failed"

# ── 5. Brain Sync ────────────────────────────────────────────────────────
echo ""
echo "[5/6] Brain Sync"
[ -f "$ROOT/bin/brain-sync.py" ] && ok "brain-sync.py exists" || fail "brain-sync.py missing"
[ -f "$ROOT/bin/e2e-memory-test.sh" ] && ok "e2e-memory-test.sh exists" || fail "e2e-memory-test.sh missing"

# ── 6. Agent MCP Configs ─────────────────────────────────────────────────
echo ""
echo "[6/6] Agent Configs"
grep -q "sin-brain" ~/.config/opencode/opencode.json 2>/dev/null && ok "OpenCode: sin-brain MCP" || fail "OpenCode: sin-brain missing"
grep -q "sin-brain" ~/.claude/settings.json 2>/dev/null && ok "Claude Code: sin-brain MCP" || fail "Claude Code: sin-brain missing"
grep -q "sin-brain" ~/.config/mimocode/mimocode.json 2>/dev/null && ok "MiMo: sin-brain MCP" || fail "MiMo: sin-brain missing"
grep -q "gbrain" ~/.codex/hooks/sin-enforcer.js 2>/dev/null && ok "Codex: sin-enforcer + gbrain" || fail "Codex: sin-enforcer missing gbrain"

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo ""
[ "$FAIL" -eq 0 ] && echo "STATUS: PASS" && exit 0 || echo "STATUS: FAIL" && exit 1
