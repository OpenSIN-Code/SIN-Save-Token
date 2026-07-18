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
