TOKEN-SAVINGS STANDARD (non-negotiable — canonical: Infra-SIN-OpenCode-Stack/docs/TOKEN-SAVINGS-BEST-PRACTICES.md):
- **L1 shell:** every shell command routes through `rtk` — never bypass it for git/test/build/package output. It rewrites transparently; do not fight it.
- **L2 tools:** use only the tools your scope needs. Prefer a CLI + `--help` + JSON over pulling in an always-on MCP server. Query the knowledge graph, don't dump the repo.
- **L3 memory:** reuse SIN-Brain / claude-mem context. Do NOT re-read files already summarized in the shared state below. Targeted retrieval only.
- **L4 output:** terse by default. Report the diff/failure, not a narrative. A passing check = 1 line. Your `worker_done` payload is data, not prose.
- **Filter before context:** feed yourself (and your report) the failure/diff, never the full 4,000-line log.

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
