#!/usr/bin/env bash
# Everyday multi-agent Cognee stack for Claude / Codex / OpenCode / MiMo / Cline / Orca.
# LLM: qoder-proxy :8013 → qodercli → Qwen 3.8
# Embed: NVIDIA NIM nemotron-3-embed-1b @ 2048-dim (free ~40 RPM) via nim-embed-proxy :8012
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 1) qoder-proxy (Qwen 3.8 LLM) =="
if ! curl -sS -m 2 -o /dev/null "http://127.0.0.1:8013/v1/models" 2>/dev/null; then
  echo "starting qoder-proxy on :8013..."
  (cd "$HOME/dev/qoder-proxy" && nohup node clean/server.js &>/tmp/qoder-proxy.log &)
  sleep 2
  if ! curl -sS -m 2 -o /dev/null "http://127.0.0.1:8013/v1/models" 2>/dev/null; then
    echo "error: qoder-proxy failed to start — check /tmp/qoder-proxy.log" >&2
    exit 1
  fi
fi
echo "qoder-proxy OK on :8013"

echo "== 2) NIM embed proxy (nemotron-3-embed-1b @ 2048) =="
if ! curl -sS -m 2 http://127.0.0.1:8012/health 2>/dev/null | grep -q ok; then
  echo "starting nim-embed-proxy on :8012..."
  nohup python3 "$ROOT/bin/nim-embed-proxy.py" &>/tmp/nim-embed-proxy.log &
  sleep 2
  if ! curl -sS -m 2 http://127.0.0.1:8012/health 2>/dev/null | grep -q ok; then
    echo "error: nim-embed-proxy failed to start — check /tmp/nim-embed-proxy.log" >&2
    exit 1
  fi
fi
echo "nim-embed-proxy OK on :8012"

echo "== 3) Cognee API with qoder-proxy env =="
# shellcheck disable=SC1091
source "$ROOT/bin/cognee-omniroute-env.sh"
"$ROOT/bin/cognee-start-omniroute.sh"

echo "== 4) Install fleet CLI wrappers on PATH (~/.local/bin) =="
mkdir -p "$HOME/.local/bin"
chmod +x "$ROOT/bin/cognee-fleet-cli.py" "$ROOT/bin/nim-embed-proxy.py"
cat >"$HOME/.local/bin/cognee-status" <<SH
#!/usr/bin/env bash
exec python3 "$ROOT/bin/cognee-fleet-cli.py" status "\$@"
SH
cat >"$HOME/.local/bin/cognee-recall" <<SH
#!/usr/bin/env bash
exec python3 "$ROOT/bin/cognee-fleet-cli.py" recall "\$@"
SH
cat >"$HOME/.local/bin/cognee-remember" <<SH
#!/usr/bin/env bash
# Usage: cognee-remember "text"   OR   cognee-remember --file path.md
exec python3 "$ROOT/bin/cognee-fleet-cli.py" remember "\$@"
SH
chmod +x "$HOME/.local/bin/cognee-status" "$HOME/.local/bin/cognee-recall" "$HOME/.local/bin/cognee-remember"

echo "== 5) Status =="
export COGNEE_PLUGIN_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
python3 "$ROOT/bin/cognee-fleet-cli.py" status || true
curl -sS -m 3 http://127.0.0.1:8012/health 2>/dev/null || echo "nim-embed-proxy health: down"

cat <<'EOF'

Ready — all agents share the same HTTP API + CLI (0 always-on MCP tax):
  cognee-status
  cognee-recall "What is L2 core MCP?"
  cognee-remember --file path/to/note.md

Embed: NVIDIA NIM nemotron-3-embed-1b @ 2048 (free, proxy :8012).
LLM: Qwen 3.8 via qoder-proxy :8013 (Qoder subscription).

Claude Code (extra): plugin auto-inject
  export COGNEE_PLUGIN_DATASET=sin-fleet
  source ~/dev/SIN-Save-Token/bin/cognee-omniroute-env.sh
  claude

Orca subs: preamble includes cognee-recall; use CLI in worker brief.
EOF
