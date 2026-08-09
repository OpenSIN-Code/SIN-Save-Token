# SIN-Save-Token Architecture

SIN-Save-Token is the local control plane that keeps agent work token-efficient **without weakening correctness, provenance, or completion gates**. It standardizes the fleet-facing shell and CLI surface, routes context to a bounded set of intelligence providers, keeps durable and transient memory separated, gates lossy/expensive optimizers behind explicit actions, and gives `sin-orca` a same-worktree delegation protocol with controller-owned verification.

The architecture is intentionally compositional: the fast default path stays small, while specialized providers, memory systems, optimization runtimes, and delegated workers are invoked only when the task requires them.

![SIN-Save-Token architecture](sin-save-token-architecture.svg)

Interactive Archify render: [sin-save-token-architecture.html](sin-save-token-architecture.html)

Editable Archify IR: [sin-save-token-architecture.json](sin-save-token-architecture.json)

## Architectural goals

1. **Spend fewer tokens, not less judgment.** RTK output compression, bounded context, caching, and terse response policy reduce waste while tests, evidence, and review remain authoritative.
2. **Select intelligence instead of fanning out.** `sin-context` chooses a route and normally calls one provider, with a configured maximum of two provider attempts.
3. **Treat retrieved material as evidence, never instructions.** Provider output is bounded, fingerprinted, scanned, and wrapped by the Evidence Firewall before it is exposed to a model-facing context packet.
4. **Keep optional cost/risk explicit.** Caveman rewrites, pxpipe lossiness, Gigatoken benchmarking, and source synchronization require explicit actions; provider execution is policy-selected, bounded, and observable.
5. **Keep completion authority with the controller.** Orca workers can execute bounded delegated work, but scope/diff verification, tests, independent review, and completion evidence determine acceptance.

## System topology

| Component | Responsibility | Important surfaces |
|---|---|---|
| **Agent runtimes** | Consumers of the standard across Claude Code, Codex, OpenCode, Orca, and related fleet runtimes. | Fleet adapters and generated/symlinked instructions outside this repository. |
| **Fleet policy + hooks** | Installs and self-heals token discipline, RTK rewrites, context nudges, and compliance gates. | `bin/install.sh`, `bin/verify-tokens`, `hooks/`, `templates/` |
| **SIN CLI surface** | Stable operator/agent entry points. | `bin/sin-context`, `bin/sin-memory`, `bin/sin-token-stack`, `bin/sin-orca` and focused helper CLIs |
| **Context broker** | Classifies a query, selects a provider route and token budget, caches results, deduplicates evidence, and emits bounded model-facing context. | `bin/sin-context`, `config/context-policy.json`, `lib/sin_context/evidence_firewall.py` |
| **Provider runtime** | Executes configured providers using argv arrays, time/output limits, persistent health state, and circuit breaking. | `lib/sin_context/provider_runtime.py`, `config/provider-runtime.json` |
| **Intelligence providers** | Supply code navigation, architecture, research, review, memory, and text-search evidence. | Simone, Graphify, SIN Code, Cognee, DeepTutor, CRG, `agent-grep`, `session-digest` |
| **Memory layer** | Separates task/session facts, summaries, durable decisions, and domain memory so large histories are not blindly reloaded. | `bin/sin-memory`, `lib/sin_memory.py`, Cognee adapters, `session-digest`, `dream` |
| **Token optimizer stack** | Adds reviewed optional optimizers and exact tokenizer-bound measurement behind a fail-closed policy. | `bin/sin-token-stack`, `lib/sin_token_stack.py`, `config/token-optimizer-stack.json`, `runtime/` |
| **Orca orchestrator** | Delegates bounded work in the current worktree, reserves the sole writer, records evidence, and supports direct callbacks. | `bin/sin-orca`, `lib/sin_orca/`, `config/orca-orchestrator.json` |
| **Verification + review** | Decides whether a change is acceptable using repository-native tests, diff/scope gates, independent review, and CI. | `tests/`, `scripts/verify-local-integration.py`, `.github/workflows/`, Orca verification/review modules |

## Primary runtime path: bounded context retrieval

A normal context request follows one narrow path rather than querying every available system:

1. An agent reaches the repository through the fleet policy and stable CLI surface.
2. `sin-context` matches the query against `config/context-policy.json` and assigns both a route and a token budget. Architecture questions, for example, prefer Graphify and can fall back to SIN Code; symbol questions prefer Simone and can fall back to Graphify.
3. The broker uses a repository/config-aware cache key. The key includes the repository state fingerprint, policy fingerprint, provider fingerprint, route, query, and token budget, preventing stale evidence from being reused across materially different states.
4. `ProviderRuntime` renders a configured argv vector, refuses unresolved arguments, checks executable availability, enforces per-provider timeout/output limits, and records failures in a local SQLite health store. Repeated failures open a cooldown circuit instead of repeatedly spending work on a broken provider.
5. Successful provider output is deduplicated and passed through the Evidence Firewall. The firewall fingerprints the source, detects instruction-like spans, escapes nested evidence markers, marks the material as untrusted evidence, and truncates it to the active context budget.
6. The resulting bounded packet returns to the agent. Provider evidence informs the model; it does not gain command authority.

This design makes provider choice and context size observable policy decisions rather than accidental prompt growth.

## Side paths

### Memory

The memory path is deliberately separate from generic context retrieval. Local L1/L2/L3 state handles task events, topic summaries, and durable decisions; Cognee owns fleet-wide durable domain memory; `session-digest` and `dream` extract compact resume/lesson artifacts from existing sessions. Retrieval is pull-based so memory does not become an always-loaded prompt tax.

### Token optimizer stack

`sin-token-stack` integrates four reviewed upstream roles behind pinned manifests and explicit commands:

- **Ponytail** supplies the minimal-solution policy.
- **Caveman** can rewrite recurring memory/instruction prose only after explicit local rewrite and third-party-upload consent, with a byte-identical external backup requirement.
- **pxpipe** remains off by default; lossy model routes require an explicit opt-in and run through an isolated locked npm runtime.
- **Gigatoken** performs exact tokenizer-bound counting/benchmarking from a locked `uv` runtime and never claims provider-billing parity implicitly.

These optimizers are sidecars to the correctness path; they do not replace retrieval, verification, or source-of-truth text.

### Orca delegation

`sin-orca` coordinates bounded delegated work without creating worker worktrees. An implementer receives a synthetic baseline ref for the exact live worktree, while the real branch, `HEAD`, and index remain controller-owned. Only one editing task may hold the repository writer reservation at a time; explorers and reviewers remain read-only.

Worker checkpoints and reports are structured artifacts. Direct callbacks provide control-plane notifications, but a worker's claim is never sufficient for completion. The controller reconstructs the ledger, validates protocol/order, checks changed paths and the baseline diff, executes verification commands, and can start a blind reviewer in the same worktree using a different agent. Completion manifests bind the accepted evidence to hashes.

## Trust, state, and ownership boundaries

### Repository-owned policy

Versioned configuration and implementation live in the repository (`config/`, `bin/`, `lib/`, `hooks/`, `runtime/`, `tests/`). These files define the reproducible contract. Runtime-specific credentials and mutable host state are never part of that contract.

### Local mutable state

Operational state is deliberately separate from versioned architecture/product documentation. Examples include context/provider SQLite caches under the user's cache directory, local memory state, `.sin-worker/` orchestration evidence, and ignored `.sin-gpt-web/` browser-delegation/task state. These stores support execution and recovery; they are not architecture source files and must not be committed as product documentation.

### External evidence boundary

Simone, Graphify, SIN Code, Cognee, DeepTutor, CRG, and other configured providers are treated as evidence-producing dependencies. `ProviderRuntime` controls process execution and failure behavior, while the Evidence Firewall controls what their text means when it crosses back into model context.

### Fleet ownership boundary

`wow-my-zsh` owns fleet-wide tool wiring and shared agent rules; SIN-Save-Token owns token-discipline behavior and its runtime gates. This avoids two repositories independently authoring the same MCP or instruction topology. See [ECOSYSTEM.md](ECOSYSTEM.md).

## Failure model

The architecture prefers visible degradation to hidden fallback:

- Missing/broken providers are recorded as unavailable/failed and can trip a circuit breaker.
- Context routing tries only the policy-allowed bounded fallback set.
- Untrusted evidence remains explicitly delimited and fingerprinted.
- Lossy optimizer routes remain disabled unless explicitly accepted.
- Orca scope, protocol, writer reservation, baseline, verification, or review failures prevent completion.
- Architecture diagrams are generated and validated by Archify; Mermaid, screenshots, and hand-edited SVG are not canonical artifacts.

## Architecture artifacts and regeneration

The diagram is generated with the fleet-standard `tt-a1i/archify` skill in `architecture` mode. The JSON IR is the editable source; the HTML is the self-contained interactive render; the SVG is the Archify-exported vector used by Markdown/README surfaces.

Canonical local validation/render loop:

```bash
rtk node ~/.claude/skills/archify/bin/archify.mjs doctor
rtk node ~/.claude/skills/archify/bin/archify.mjs validate architecture docs/sin-save-token-architecture.json --json
rtk node ~/.claude/skills/archify/bin/archify.mjs render architecture docs/sin-save-token-architecture.json docs/sin-save-token-architecture.html
rtk node ~/.claude/skills/archify/bin/archify.mjs check docs/sin-save-token-architecture.html
```

Export `docs/sin-save-token-architecture.svg` from the generated HTML using Archify's built-in **SVG** export. Do not edit the generated HTML or SVG by hand; regenerate from the JSON IR instead.
