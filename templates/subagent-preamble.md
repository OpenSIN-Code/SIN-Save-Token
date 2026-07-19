TOKEN-SAVINGS STANDARD (non-negotiable — canonical: Infra-SIN-OpenCode-Stack/docs/TOKEN-SAVINGS-BEST-PRACTICES.md):
- **L1 shell:** every shell command routes through `rtk` — never bypass it for git/test/build/package output. It rewrites transparently; do not fight it.
- **L2 tools:** use only the tools your scope needs. Prefer a CLI + `--help` + JSON over pulling in an always-on MCP server. Query the knowledge graph, don't dump the repo.
- **L3 memory:** reuse claude-mem + **shared Cognee** (`cognee-recall "…"` / `cognee-remember …`, dataset `sin-fleet`). Do NOT re-read files already summarized. Targeted retrieval only.
- **L4 output:** terse by default. Report the diff/failure, not a narrative. A passing check = 1 line. Your `worker_done` payload is data, not prose.
- **L4-input (always-loaded surface):** skills fire passively only ~30–50% of the time — when you know which skill you need, call `/skill-name` directly, don't hope it auto-triggers. Never inline path-specific rules into an always-loaded instruction file; that tax is paid on every turn.
- **Filter before context:** feed yourself (and your report) the failure/diff, never the full 4,000-line log.

CODE-INTEL & VERIFY TOOLS (CLI — 0 schema tokens, use proactively):
- **graphify — Code-Graph statt blind grep (LLM-frei, ~25x weniger Tokens):** Bei „wo ist X / was ruft Y / Blast-Radius" **erst den Graphen fragen**, nicht 20 Dateien grep'en. `graphify query "<frage>"` (scoped Subgraph), `graphify path "A" "B"` (Zusammenhang), `graphify explain "X"` (Symbol + Nachbarn). Kein Graph? `graphify update .`, dann fragen.
- **sin-CLI vor „done":** `sin verify` vor jedem Merge/„done" — grüner Compile ist kein Beweis, nur ausführungsbasierte Verifikation zählt. `sin review` statt Roh-Diff bei eigenen Änderungen. Tool nicht verfügbar? Sag es explizit und mach graceful weiter.
- **Cognee fleet memory (alle Agents/Orca):** `cognee-recall "<frage>"` für Policy/Decisions; `cognee-remember "…"` nur kurze durable lessons (Boundless Terra kostet). Stack: `bin/cognee-fleet-up.sh`. Embed default: local fastembed; optional NIM.

TASK SPEC:
Task ID: [taskId]
Scope (allowed): [exact list of files/directories]
Forbidden: [list or "everything else"]
Success criteria: [clear definition of done]
Verification commands you MUST run and include output from:
  - [command 1]
  - [command 2]
  - ...

Current relevant shared state (from orchestrator):
