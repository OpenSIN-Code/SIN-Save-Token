#!/usr/bin/env bash
set -euo pipefail

echo "=== Step 1: Test OPENAI_BASE_URL ==="
export OPENAI_BASE_URL=http://127.0.0.1:8012/v1
gbrain call put_page '{"slug":"test-env","content":"hello from env test"}' 2>&1 && echo "SUCCESS: OPENAI_BASE_URL works" || echo "FAIL: OPENAI_BASE_URL"

echo ""
echo "=== Step 2: Test OPENAI_API_BASE ==="
export OPENAI_API_BASE=http://127.0.0.1:8012/v1
gbrain call put_page '{"slug":"test-api-base","content":"hello from api base test"}' 2>&1 && echo "SUCCESS: OPENAI_API_BASE works" || echo "FAIL: OPENAI_API_BASE"

echo ""
echo "=== Step 3: gbrain stats ==="
gbrain stats 2>&1

echo ""
echo "=== Step 4: gbrain-rebuild ==="
cd /Users/jeremy/dev/global-brain
node src/cli.js gbrain-rebuild 2>&1 || echo "rebuild failed"

echo ""
echo "=== Step 5: gbrain-status ==="
node src/cli.js gbrain-status 2>&1 || echo "status failed"
