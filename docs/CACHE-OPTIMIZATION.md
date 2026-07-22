# Cache Optimization Layer

Additive tools to maximize **warm prompt-cache** usage across SIN / Orca agents.
Canonical design: [`CACHE-SPEC.md`](../CACHE-SPEC.md) · task brief: [`BRIEF.md`](../BRIEF.md).

## Why

| Fact | Implication |
|---|---|
| Prompt-cache TTL ≈ **5 min** | Idle >5 min → next turn cold (full input price) |
| ~**85–90%** of input bill is cacheable | Stable system + tools + early messages dominate cost |
| Mid-session edit of CLAUDE.md / AGENTS.md / settings.json / MCP registry | **Invalidates entire prefix** for all live sessions |
| First turn of a session is always cold | **Pre-warm** so the first real turn is a hit |

## Tools

| CLI / Hook | Prio | Role |
|---|---|---|
| `bin/cache-warm` | 1 | One cheap ping with `cache_control` on stable prefix |
| `bin/cache-keepalive` | 2 | Daemon: re-ping every ~4 min (`start\|stop\|status`) |
| `hooks/cache-stable-guard.js` + `bin/cache-stable-guard` | 3 | Non-blocking warn on cache-buster edits |
| `bin/orca-router` | 4 | Task text → `agent=… model=…` (no spawn) |
| `bin/orca-result-compress` | 5 | Worker raw log → WORKER-REPORT signal |
| `bin/cache-prefix-stability` | opt | Read-only report of prefix volatility |
| `bin/orca-pool` / `orca-batch` / `orca-prefetch` | opt | Stubs for session reuse / batch / prefetch |

State is **repo-local only**: `<repo>/.cache-opt/` (pid, log, warm stamp). No secrets on disk — keys only from env (`${ANTHROPIC_API_KEY}`, `${ORCA_CACHE_API_KEY}`, …).

## Quick start

```bash
# Preview warm payload (no network)
./bin/cache-warm --dry-run

# Warm once (fail-open if no key / network)
./bin/cache-warm

# Keep warm across idle (~4 min interval)
./bin/cache-keepalive start
./bin/cache-keepalive status
./bin/cache-keepalive stop

# Route a task (machine-readable)
./bin/orca-router "fix typo in README"
# → agent=mimo-code model=-
./bin/orca-router --explain "refactor auth across 12 files"
# → agent=codex model=gpt-5.6-sol

# Compress worker output before lead synthesis
./bin/orca-result-compress worker.raw.md

# Prefix stability report
./bin/cache-prefix-stability
```

### Hook install (Claude Code PreToolUse)

Add matcher for Write/Edit (paths are illustrative — use your checkout):

```json
{
  "matcher": "Write|Edit|MultiEdit",
  "hooks": [
    {
      "type": "command",
      "command": "node /path/to/SIN-Save-Token/hooks/cache-stable-guard.js",
      "timeout": 5
    }
  ]
}
```

Standalone check:

```bash
./bin/cache-stable-guard CLAUDE.md
./bin/cache-stable-guard --check-cwd
```

## Routing rule (fixed)

- **trivial / default** → `mimo-code` or `opencode` (env `ORCA_DEFAULT_CHEAP`)
- **heavy** → `codex` + model `gpt-5.6-sol` (env `ORCA_HEAVY_MODEL`)
- Lead (Claude) keeps architecture / review / synthesis only

## Env knobs

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ORCA_CACHE_API_KEY` | cache-warm | Auth (never committed) |
| `ANTHROPIC_BASE_URL` / `ORCA_CACHE_BASE_URL` | cache-warm | Gateway / OmniRoute base |
| `ORCA_CACHE_MODEL` | cache-warm | Override ping model |
| `ORCA_CACHE_PROVIDER` | cache-warm | `auto` \| `anthropic` \| `openai` |
| `CACHE_KEEPALIVE_INTERVAL` | cache-keepalive | Seconds (default 240) |
| `ORCA_DEFAULT_CHEAP` | orca-router | `mimo-code` \| `opencode` |
| `ORCA_HEAVY_MODEL` | orca-router | default `gpt-5.6-sol` |

## Safety / constraints

- **Fail-open**: missing key, network error, bad stdin → exit 0 + stderr warn
- **Nudges never block**: guards always `permissionDecision: allow` / exit 0
- **No secrets in tree**: only `${VAR}` placeholders
- **No external write paths** for new state (use `.cache-opt/` in-repo)
- Timeouts on every network call

## Related existing hooks

- `hooks/cache-cold-warn.js` — warns when >5 min since last turn (stamp-based)
- `hooks/orca-delegation-guard.js` — nudge expensive exploration to `orca` / mimo-code
