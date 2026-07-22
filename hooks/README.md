# Token-Saving Hooks

Agent-agnostic hooks that enforce token discipline. Written for Claude Code's
PreToolUse hook interface (stdin JSON → stdout JSON), reusable by any agent
runtime that can shell out on tool calls (opencode, codex via their own hook
mechanisms).

## Hooks

| File | Event | What it does |
|---|---|---|
| `rtk-auto-rewrite.js` | PreToolUse (Bash) | Rewrites `git/cargo/npm/...` → `rtk <cmd>` so command output is compressed by the [RTK](https://github.com/OpenSIN-Code) proxy. Conservative: only simple single commands, never compound (`&&`, `\|`, `;`), idempotent, passes through unchanged if `rtk` is absent. |
| `orca-delegation-guard.js` | PreToolUse (WebFetch/WebSearch/Bash) | Non-blocking nudge to delegate expensive exploration (web lookups, broad `grep -r`/`rg`/`find`) to a subagent via `orca`, keeping token-heavy output out of the main context. Throttled to ≤1 nudge / 10 min. Never blocks. |
| `agent-grep-nudge.js` | PreToolUse (Grep) | Non-blocking nudge toward `agent-grep` when the native Grep tool runs a broad tree scan. `agent-grep` tags hits with their enclosing symbol and self-truncates, saving the follow-up file read. Throttled ≤1/10 min. Never blocks. |
| `cache-cold-warn.js` | PreToolUse (any tool) | Warns when >5 min elapsed since the last turn (Anthropic prompt-cache TTL), meaning the next turn re-reads context uncached (costlier). Suggests `/compact`. Stamp-based; never blocks. |
| `orca-route-nudge.js` | UserPromptSubmit (any prompt) | Non-blocking nudge to delegate heavy/multi-file tasks to the right agent via `orca worktree create`. Uses `bin/orca-route` classifier. Throttled ≤1/90s. Never blocks. |

`lib/git-cmd.js` — shared git-command classifier used by the hooks.

## Install (Claude Code)

Add to `~/.claude/settings.json` under `hooks.PreToolUse`:

```json
{ "matcher": "Bash", "hooks": [
  { "type": "command", "command": "node /path/to/hooks/rtk-auto-rewrite.js", "timeout": 5 } ] },
{ "matcher": "WebFetch|WebSearch|Bash", "hooks": [
  { "type": "command", "command": "node /path/to/hooks/orca-delegation-guard.js", "timeout": 5 } ] }
```

## Design principle

Save tokens without making the model dumber: compress *output* and keep verbose
*exploration* out of the main context — never remove information the model needs
to reason.

## Safety

Every hook exits 0 silently on any error or unmatched case. A missed rewrite is
harmless; a broken command is not. None of these hooks contain or read secrets.

## Bin CLIs

| File | Purpose |
|---|---|
| `cache-warm-ping` | Direct OmniRoute keepalive ping (`--watch`/`--stop`/`--help`). No orca dependency. |
| `orca-route` | Task classifier CLI: prints `agent=<X> model=<Y>`. `--explain` for reasoning. |
