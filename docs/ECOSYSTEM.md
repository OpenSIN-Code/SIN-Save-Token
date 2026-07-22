# ECOSYSTEM — SIN-Save-Token × wow-my-zsh

**Status:** canonical contract (2026-07-19)  
**Repos:** OpenSIN-Code/SIN-Save-Token + OpenSIN-Code/wow-my-zsh

## One-line split

| Repo | Owns | Does **not** own |
|------|------|------------------|
| **wow-my-zsh** | *What* agents load: house rules (`shared/AGENTS.md`), MCP registry, per-agent symlinks/merges | Runtime token compression, PreToolUse rewrites, verify gates |
| **SIN-Save-Token** | *How* agents spend tokens: rtk hooks, nudges, CLIs (`agent-grep`, `session-digest`, `dream`, `verify-tokens`) | Authoring MCP server lists or multi-agent instruction symlinks |

Together: **wow wires the tools; SST keeps their cost honest.**

## Install order (new machine)

```bash
# 1. External CLIs (rtk, graphify, orca, sin, skillopt-sleep) — see wow docs/token-discipline-clis.md
# 2. Config router
git clone https://github.com/OpenSIN-Code/wow-my-zsh.git ~/dev/wow-my-zsh
~/dev/wow-my-zsh/install.sh
~/dev/wow-my-zsh/doctor.sh

# 3. Token standard
git clone https://github.com/OpenSIN-Code/SIN-Save-Token.git ~/dev/SIN-Save-Token
~/dev/SIN-Save-Token/bin/install.sh
# Register SessionStart heal once (see SST README)
~/dev/SIN-Save-Token/bin/verify-tokens
```

Re-run either installer anytime; both are idempotent.

## Shared surfaces

| Surface | Source of truth | Consumers |
|---------|-----------------|-----------|
| House rules text | wow `shared/AGENTS.md` | All agents via symlink/import |
| MCP server list | wow `shared/mcp/servers.json` | Merged into real agent configs |
| L2 budget / task-profile policy | wow `shared/mcp/servers.json` + `task-profiles.json` | wow generator/doctor + SST `verify-tokens` |
| L1 rtk behavior | SST hooks + `rtk` binary | Claude PreToolUse, opencode plugin, Codex RTK.md |
| L3 memory backends | SST: claude-mem (session) + Cognee fleet CLI (domain graph) | All runtimes |
| agent-grep binary | SST `bin/agent-grep` | wow doctor PATH check; house rules |

## Health commands

```bash
wow-my-zsh/doctor.sh          # symlinks, registry merge presence, env vars, CLI PATH
SIN-Save-Token/bin/verify-tokens   # L0–L4 compliance; fail-loud on regression
```

Both should be green after install. If they disagree, **gates are wrong** — open a P0.


## L2 MCP budget (shared)

Source of truth: **wow-my-zsh** `shared/mcp/servers.json` fields:

- `tier`: `core` | `optional` | `experimental`
- `always_on`: bool — only `true` servers ship under default install profile

| Profile | Command | Effect |
|---------|---------|--------|
| **minimal** (default) | `./install.sh` or `--profile minimal` | Zero managed MCP servers; lowest schema tax |
| **core** | `./install.sh --profile core` | Only `always_on=true` servers; currently zero |
| **task** | `bin/sin-mcp-profile <task> <agent>` | Capability-based set with a hard cap of one or two servers |
| **full** | `SIN_ADMIN_CONFIRM=1 ./install.sh --profile full` | All registry servers for each agent; explicit administrative opt-in |

Enforcement:

- wow `doctor.sh` — any managed server under `minimal`, or disallowed server under `core`, is drift
- SST `verify-tokens` — managed MCPs outside the selected budget fail unless an explicit diagnostic override is set

Core allowlist (current): **empty**. Tool access is selected per task, not globally preloaded.

## Change rules

1. Adding an MCP server → **wow** registry only; re-run `install.sh`; never hand-edit only one agent.
2. Changing token policy / hooks → **SST**; re-run `install.sh --heal` / verify.
3. Changing house-rule prose that names a CLI → update **wow** AGENTS.md **and** ensure SST or brew still ships that CLI.
4. Secrets never in either repo — `${VAR}` only.

## Memory stack (current + proposed)

```
code structure         graphify (CLI, 0 LLM)
code symbols/LSP       Simone (primary for symbol navigation)
session observations   claude-mem (short-lived, pull-based)
domain graph memory    Cognee fleet (only canonical durable memory; CLI HTTP :8011)
                         embed: NVIDIA NIM / local fallback (:8012)
                         LLM cognify: OmniRoute
curated staging         gbrain → one-way curated export to Cognee
archive / plans         global-brain, on-demand; no default prompt injection
resume / lessons        session-digest, dream (SST CLIs)
```

Do not add a fourth overlapping “brain” without a measured ROI gate.
