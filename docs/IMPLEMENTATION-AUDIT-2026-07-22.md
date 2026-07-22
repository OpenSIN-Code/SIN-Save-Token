# Token-Minimal Integration Remediation — 2026-07-22

## Scope

This remediation resolves the configuration and architecture drifts identified across:

- `SIN-Save-Token`
- `wow-my-zsh`
- `global-brain`

The filesystem bridge used for the remediation provides read/write access but no process execution. Therefore this document distinguishes static implementation from live execution evidence.

## Implemented

### Single context router and provider order

- `sin-context` remains the only default context entry point.
- Symbol/reference navigation now routes `Simone -> Graphify`.
- Architecture/dependency questions retain `Graphify -> sin-code`.
- Every routed provider has a runtime specification.
- Maximum provider attempts remain capped at two.

### Persistent provider health

- `sin-context` now uses the shared `ProviderRuntime` implementation.
- Timeouts, unavailable executables, failures, cooldowns and persistent circuit-open state are active in the broker path.
- Infrastructure failures are not written into the semantic negative cache.
- JSON diagnostics include provider attempt status and duration.
- Provider stderr is retained for diagnostics but excluded from successful context when stdout exists.

### Memory ownership

- Cognee is the only canonical durable domain-memory owner.
- gbrain exports only explicitly curated entries to Cognee.
- Automatic Cognee-to-gbrain reverse sync remains disabled.
- global-brain is an on-demand archive and plan store.
- global-brain before/after hooks are passive by default.
- Automatic transcript extraction, archive sync and prompt injection require separate explicit opt-ins.
- `sync-chat-turn` and automatic claude-mem export were removed from the live afterRun hook.

### MCP budget

- The canonical default profile is `minimal`.
- `minimal` contains zero managed MCP servers.
- Task profiles have a hard maximum of one or two servers.
- Cognee uses `http://127.0.0.1:8011/mcp` consistently.
- wow installer, doctor, documentation and SST verification now use the same profile semantics.
- Doctor detects transient Claude-worktree PATH/runtime shadowing.

### Exploration limits

- The previous 5–10 explorer plus 5–10 librarian mandate was removed.
- Default exploration uses one provider/worker.
- A second worker is permitted only for an independent question or fallback.
- Raw worker transcripts are forbidden from entering the main context.

### Verification assets

- Added persistent circuit-breaker unit tests.
- Updated routing, registry, hook isolation and MCP-profile tests.
- Replaced the memory E2E script with a non-polluting gate.
- Added a reproducible A/B/C benchmark harness and representative task set.
- Partial SST-only benchmark reports are marked non-claimable.
- Added one idempotent local rollout script that repairs Unix modes, runs tests, converges live MCP configs to minimal, and executes all gates.

## Filesystem-mode note

The Mac filesystem bridge writes modified text files with mode `0644`. The rollout script repairs executable modes before invoking any entry point. Run it through Bash so it does not depend on its own execute bit:

```bash
bash /Users/jeremy/dev/SIN-Save-Token/bin/apply-token-minimal-local.sh
```

## Evidence still required

The following are intentionally not claimed as completed until the rollout runs in a normal terminal:

- actual Python/Node test-suite results;
- live home-config convergence under `~/.claude`, `~/.config/opencode`, `~/.codex`, `~/.jcode`, `~/.config/mimocode`;
- process and service health;
- real cache-hit rates;
- authoritative billed input/cache/output token counters;
- a complete baseline/SST/full-stack A/B/C report.

No “worldwide number one” claim is supported without those results and an independently reproducible competitor comparison.
