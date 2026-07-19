#!/usr/bin/env bash
# Everyday multi-agent Cognee stack for Claude / Codex / OpenCode / MiMo / Cline / Orca.
# OmniRoute → Boundless Terra (LLM) + local fastembed (embed default) → Cognee :8011 → CLI.
# Optional: COGNEE_EMBED_BACKEND=nim for NVIDIA nv-embedqa-e5-v5 (1024-dim; reindex required).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 1) OmniRoute register Boundless Terra models (no paid chat by default) =="
# Does NOT call Boundless/Terra unless COGNEE_COSTLY_SMOKE=1
"$ROOT/bin/omniroute-ensure-boundless-terra.sh"

echo "== 2) Cognee API with OmniRoute env =="
# shellcheck disable=SC1091
source "$ROOT/bin/cognee-omniroute-env.sh"
"$ROOT/bin/cognee-start-omniroute.sh"

echo "== 3) Install fleet CLI wrappers on PATH (~/.local/bin) =="
mkdir -p "$HOME/.local/bin"
chmod +x "$ROOT/bin/cognee-fleet-cli.py"
for name in cognee-status cognee-recall cognee-remember; do
  cat >"$HOME/.local/bin/$name" <<SH
#!/usr/bin/env bash
exec python3 "$ROOT/bin/cognee-fleet-cli.py" ${name#cognee-} "\$@"
SH
  chmod +x "$HOME/.local/bin/$name"
done
# status subcommand is "status"
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

echo "== 4) Status =="
export COGNEE_PLUGIN_DATASET="${COGNEE_PLUGIN_DATASET:-sin-fleet}"
python3 "$ROOT/bin/cognee-fleet-cli.py" status || true

cat <<'EOF'

Ready — all agents share the same HTTP API + CLI (0 always-on MCP tax):
  cognee-status
  cognee-recall "What is L2 core MCP?"
  cognee-remember --file path/to/note.md

Claude Code (extra): plugin auto-inject
  export COGNEE_PLUGIN_DATASET=sin-fleet
  source ~/dev/SIN-Save-Token/bin/cognee-omniroute-env.sh
  claude

Orca subs: preamble includes cognee-recall; use CLI in worker brief.
EOF
