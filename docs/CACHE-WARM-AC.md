# Cache Warm Hooks

This worktree adds three pieces that use the documented Claude Code hook
protocol only: JSON on stdin, JSON on stdout, and no settings-file mutation.

## A: SessionStart warmup

`hooks/cache-warm-session.js` accepts only `SessionStart`. It resolves
`cache-warm` from `PATH`, then falls back to the sibling
`../cache-opt-grok/bin/cache-warm`. A short dry-run probe identifies the
provider, then the real warm ping is detached and bounded to eight seconds so
session startup is not held on a slow network. Its only context injection is:

```json
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"cache prewarmed: <provider|skipped>"}}
```

## B: SessionStart keepalive

`hooks/cache-keepalive-autostart.js` checks the repo-local
`.cache-opt/keepalive.pid` sidecar before starting `cache-keepalive`. It passes
`CACHE_KEEPALIVE_ROOT` so the PID and log remain in this worktree, and is safe
to invoke repeatedly. The hook emits `cache keepalive: running` as
`SessionStart.additionalContext` after a successful or already-running check.

The matching stop path is manual because this build does not register a
`Stop` hook: run `../cache-opt-grok/bin/cache-keepalive stop` from this
worktree, or use the same `cache-keepalive stop` command resolved from `PATH`.
The sibling CLI honors `CACHE_KEEPALIVE_ROOT` for the repo-local sidecar.

## C: Prefix gate

`bin/cache-prefix-check` runs the sibling `cache-prefix-stability` read-only
scan and then checks git status for `CLAUDE.md`, `AGENTS.md`, `settings.json`,
and `shared/mcp/servers.json`. It exits nonzero when any listed cache-buster
has uncommitted changes. `--warn-only` reports the same result but exits zero.
It never writes to the repository.

## D: Cache warm ping (keepalive)

`bin/cache-warm-ping` touches the OmniRoute pool directly with a tiny
`/v1/chat/completions` (max_tokens=1), reading `OMNIROUTE_MASTER_KEY` from env
or `~/.omniroute/.env`. Flags: `--watch` (loop every 240s, PID sidecar),
`--stop`, `--help`. Never references `orca run`. Fail-open exit 0.

## E: orca-router (classifier + nudge)

Split into two pieces because a hook cannot "execute elsewhere and return":

1. `bin/orca-route` — classifier CLI only, no spawn. Reads task text (arg or
   stdin), prints `agent=<mimo-code|opencode|codex> model=<gpt-5.6-sol|->`.
   Heuristic: 1-file/read/lint/rename/typo → trivial→mimo-code;
   multi-file/build/refactor/audit/migrate → heavy→codex gpt-5.6-sol;
   else default→opencode. `--explain` prints reason.
2. `hooks/orca-route-nudge.js` — `UserPromptSubmit` hook (NEVER blocks). If the
   prompt looks heavy/default, emits a nudge via `additionalContext`. Throttle:
   ≤1/90s. Does NOT run orca synchronously. Does NOT use `permissionDecision:deny`.

All programs support `--help` where applicable, use English output, and
fail open in hook mode.
