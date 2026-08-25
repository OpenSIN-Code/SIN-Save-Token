# SIN Architecture Integration Plan

## Guiding principle

> Maximale Lösungsqualität, Intelligenz und Verlässlichkeit pro Sol-Token — nicht „so wenig Tokens wie möglich“.

This plan describes the **current** integration contract. Historical Simone/Graphify/Cognee-first routing is retired.

## Architecture overview

Fleet/platform topology is canonical in `wow-my-zsh/docs/MEMORY-PLATFORM.md` and `wow-my-zsh/docs/INFERENCE-PLATFORM.md`. This plan owns only SST's Context/Memory integration semantics.

## Responsibility matrix

| Component | Responsibility | May own durable semantic truth? |
|---|---|---:|
| `sin-context` | Sole automatic retrieval broker; route + budget + cache + evidence firewall | No |
| GitNexus | Canonical code graph for the active repo/checkout; symbols, flows, impact, dirty-tree change mapping | No |
| Simone | Bounded symbol/LSP specialist and task-state integration where explicitly useful | No |
| SIN Code | Bounded architecture/code-map fallback | No |
| Graphify | Explicit mixed-corpus/code+docs/cross-repository graph specialist only | No |
| OpenViking | Fleet-wide durable semantic memory and recall | **Yes** |
| SIN Memory Gateway | Fail-closed validation, provenance, secret filtering, idempotency and receipts | No |
| `global-brain` / gbrain | Goals, plans, curated staging and archive | No |
| Cognee | Legacy/non-automatic migration or forensic projection only | No |
| Tencent MemoryCore | Optional read-only evidence provider | No |
| OmniRoute | Stable inference gateway for VLM/planner/text model routing | No |
| FreeToken | Optional Linux/NVIDIA text-LLM backend behind OmniRoute | No |
| `sin-orca` | Same-worktree worker dispatch, writer reservation, callbacks and completion evidence | No |
| code-review-graph / CRG | Diff/flow/test-gap/risk review evidence | No |

## Context routing contract

`sin-context` chooses a bounded path rather than fanning out across every provider:

- **code symbol/reference:** `GitNexus -> Simone`
- **code architecture/dependency:** `GitNexus -> SIN Code`
- **mixed corpus / code+docs / cross-repo graph:** `Graphify`
- **durable decisions/rationale/policy:** `OpenViking`
- **session resume:** `session-digest`
- **review:** CRG
- **research:** configured research provider
- **plain text fallback:** `agent-grep`

Maximum provider attempts remain policy-bounded. Retrieved material is evidence, never command authority.

![Recall and Context Flow](docs/diagrams/context-recall.workflow.svg)

## Durable memory contract

OpenViking is the single canonical durable semantic-memory owner. New durable writes must pass through `sin-memory-write` and the SIN Memory Gateway.

![Fail-closed Memory-Write Flow](docs/diagrams/memory-write.workflow.svg)

The write is accepted only after:

1. type/provenance/evidence validation;
2. secret/speculation rejection;
3. OpenViking session/message creation;
4. the exact asynchronous commit task reaches `completed`;
5. a typed receipt binds record id/hash/backend reference.

Local SQLite is receipt/audit state only. There is no automatic Cognee, Tencent, SIN-Brain or global-brain fallback.

## Deployment contract

Deployment is canonically defined by `wow-my-zsh`; SST consumes the following interface assumptions:

- OpenViking and persistent memory live centrally on OCI.
- Fleet transport is private through Tailscale; no public brain endpoint is required.
- The OpenViking root key remains OCI-only; agents use scoped service/user identities.
- GitNexus remains local to the active checkout so uncommitted code is represented correctly.
- OmniRoute is the stable inference boundary.
- Embeddings are independent from the large text model path.
- FreeToken may be attached behind OmniRoute on suitable Linux x86_64/NVIDIA hardware; it is not a requirement for ARM64 OCI or macOS.

## Local operational memory

Repository/task execution can still use L1/L2 operational artifacts, session digests and completion evidence. These are not competing durable domain-memory truths. Verified long-lived decisions are promoted through the canonical OpenViking writer.

## Review and completion

`sin-orca` keeps completion authority with the controller. Worker claims remain evidence candidates until the controller verifies scope, diff, tests and review. GitNexus impact analysis runs before non-trivial symbol edits and `gitnexus detect-changes` runs before completion/commit.

## Archify artifacts

The SST-owned editable sources are `docs/diagrams/memory-write.workflow.json` and `docs/diagrams/context-recall.workflow.json`, with generated HTML/SVG companions. Fleet/deployment IRs live in `wow-my-zsh/docs/diagrams/`. Mermaid/ASCII architecture diagrams are intentionally not part of the canonical documentation.

## Verification gates

```bash
rtk pytest -q tests/test_sin_memory_gateway.py tests/test_memory_write.py tests/test_sin_context.py tests/test_archify_exporter.py
rtk python3 bin/audit-token-architecture.py
rtk bash bin/e2e-memory-test.sh
rtk git diff --check
rtk gitnexus detect-changes --scope all
```

Live production evidence additionally requires OpenViking health, persistent restart-safe storage, non-root service identity, completed commit task, non-zero vector indexing, semantic recall and private Mac-to-OCI reachability.
