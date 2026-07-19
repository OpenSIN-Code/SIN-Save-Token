#!/usr/bin/env bash
# Everyday multi-agent Cognee stack for Claude / Codex / OpenCode / MiMo / Cline / Orca.
# LLM: OmniRoute → Boundless Terra
# Embed: Gemini free tier via :8012 proxy, auto-fallback → local mxbai-large (1024-dim)
# Optional: COGNEE_EMBED_BACKEND=fastembed|nim
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 1) OmniRoute register Boundless Terra models (no paid chat by default) =="
# Does NOT call Boundless/Terra unless COGNEE_COSTLY_SMOKE=1
"$ROOT/bin/omniroute-ensure-boundless-terra.sh"

echo "== 2) Embed proxy (Gemini → local mxbai fallback) =="
"$ROOT/bin/cognee-start-embed-proxy.sh"

echo "== 3) Cognee API with OmniRoute env =="
# shellcheck disable=SC1091
source "$ROOT/bin/cognee-omniroute-env.sh"
"$ROOT/bin/cognee-start-omniroute.sh"

echo "== 4) Install fleet CLI wrappers on PATH (~/.local/bin) =="
mkdir -p "$HOME/.local/bin"
chmod +x "$ROOT/bin/cognee-fleet-cli.py" "$ROOT/bin/cognee-embed-proxy.py" "$ROOT/bin/cognee-start-embed-proxy.sh"
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
curl -sS -m 3 http://127.0.0.1:8012/health 2>/dev/null || echo "embed-proxy health: down"

cat <<'EOF'

Ready — all agents share the same HTTP API + CLI (0 always-on MCP tax):
  cognee-status
  cognee-recall "What is L2 core MCP?"
  cognee-remember --file path/to/note.md

Embed path: Gemini free (proxy :8012) → auto local mxbai on rate limit.
LLM: OmniRoute Boundless gpt-5.6-terra (paid cognify).

Claude Code (extra): plugin auto-inject
  export COGNEE_PLUGIN_DATASET=sin-fleet
  source ~/dev/SIN-Save-Token/bin/cognee-omniroute-env.sh
  claude

Orca subs: preamble includes cognee-recall; use CLI in worker brief.
EOF
