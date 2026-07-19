#!/usr/bin/env bash
# Ensure OmniRoute Boundless node has gpt-5.6-terra (+ siblings) registered.
# Idempotent. Does NOT call Boundless chat by default (costs money).
#
# Cost policy:
#   - Default: register models only + free NIM embed smoke (no Boundless tokens)
#   - Costly Terra chat smoke: COGNEE_COSTLY_SMOKE=1
#
# Requires OmniRoute on :20128 and OMNIROUTE_MASTER_KEY (or ~/.omniroute/.env).
set -euo pipefail

if [ -z "${OMNIROUTE_MASTER_KEY:-}" ] && [ -f "$HOME/.omniroute/.env" ]; then
  OMNIROUTE_MASTER_KEY="$(
    python3 -c '
from pathlib import Path
for line in Path.home().joinpath(".omniroute/.env").read_text().splitlines():
    if line.startswith("OMNIROUTE_MASTER_KEY="):
        print(line.split("=", 1)[1].strip().strip("\"'\''"))
        break
'
  )"
  export OMNIROUTE_MASTER_KEY
fi
: "${OMNIROUTE_MASTER_KEY:?set OMNIROUTE_MASTER_KEY}"

OR="${OMNIROUTE_URL:-http://127.0.0.1:20128}"
AUTH="Authorization: Bearer $OMNIROUTE_MASTER_KEY"

NODE_ID="$(
  curl -sS -m 15 "$OR/api/provider-nodes" -H "$AUTH" | python3 -c '
import json,sys
d=json.load(sys.stdin)
nodes=d.get("nodes") or d
for n in nodes:
    if (n.get("prefix") or "") == "boundless" or "boundless" in (n.get("name") or "").lower():
        print(n["id"]); break
'
)"
if [ -z "$NODE_ID" ]; then
  echo "creating BoundlessAPI node..."
  NODE_ID="$(
    curl -sS -m 15 -X POST "$OR/api/provider-nodes" -H "$AUTH" -H 'Content-Type: application/json' \
      -d '{"type":"openai-compatible","name":"BoundlessAPI","prefix":"boundless","apiType":"chat","baseUrl":"https://api.boundlessapi.com/v1","modelsPath":"/v1/models"}' \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or d.get("node",{}).get("id") or "")'
  )"
fi
echo "boundless node: $NODE_ID"

for m in gpt-5.6-terra gpt-5.6-sol gpt-5.6-luna gpt-5.5 gpt-5.4; do
  curl -sS -m 15 -X POST "$OR/api/provider-models" -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"provider\":\"$NODE_ID\",\"modelId\":\"$m\"}" >/dev/null || true
  echo "  model registered: boundless/$m"
done

# Embed smoke: default = local fastembed (fleet default, free, no OmniRoute).
# Optional NIM: COGNEE_SMOKE_NIM=1 (short input only — nv-embedqa-e5-v5 hard-caps ~512 tokens).
VENV_PY="${COGNEE_VENV_PYTHON:-$HOME/.cognee-plugin/venv/bin/python}"
if [ -x "$VENV_PY" ]; then
  echo "smoke embed (local fastembed BAAI/bge-small-en-v1.5)..."
  "$VENV_PY" -c '
from fastembed import TextEmbedding
m = TextEmbedding("BAAI/bge-small-en-v1.5")
v = next(m.embed(["sin-fleet smoke"]))
print("fastembed OK dims", len(v))
' || echo "warn: fastembed smoke failed (install: pip install fastembed in cognee venv)"
else
  echo "warn: no cognee venv at $VENV_PY — skip local embed smoke"
fi

if [ "${COGNEE_SMOKE_NIM:-0}" = "1" ]; then
  echo "smoke embed (optional NIM) nvidia/nv-embedqa-e5-v5 short input..."
  curl -sS -m 45 "$OR/v1/embeddings" -H "$AUTH" -H 'Content-Type: application/json' \
    -d '{"model":"nvidia/nv-embedqa-e5-v5","input":"test"}' \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("nim dims", len(d["data"][0]["embedding"]) if d.get("data") else d)'
else
  echo "skip NIM embed smoke (set COGNEE_SMOKE_NIM=1 — optional; 512-token hard limit)"
fi

# Boundless chat burns quota — opt-in only
if [ "${COGNEE_COSTLY_SMOKE:-0}" = "1" ]; then
  echo "COGNEE_COSTLY_SMOKE=1 → one tiny Terra chat (costs Boundless credit)..."
  curl -sS -m 60 "$OR/v1/chat/completions" -H "$AUTH" -H 'Content-Type: application/json' \
    -d '{"model":"boundless/gpt-5.6-terra","messages":[{"role":"user","content":"ok"}],"max_tokens":1,"stream":false}' \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("choices",[{}])[0].get("message",{}).get("content") or d)'
else
  echo "skip Boundless chat smoke (set COGNEE_COSTLY_SMOKE=1 to enable — spends credit)"
fi

echo "done (no Boundless cognify/bulk)."
