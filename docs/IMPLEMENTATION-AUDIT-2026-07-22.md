# Token-Minimal Integration Remediation — 2026-07-22

> **Current architecture note (2026-08-25):** This historical remediation has been normalized to the current OpenViking/GitNexus contract. References below describe the resulting architecture, not the superseded Cognee/Graphify defaults that existed during the original July audit.

## Scope

The remediation resolves configuration and architecture drift across `SIN-Save-Token`, `wow-my-zsh`, and `global-brain` while keeping one automatic context router, one canonical durable-memory owner, and one canonical code-intelligence graph per active checkout.

## Implemented

### Single context router and provider order

- `sin-context` remains the only default automatic context entry point.
- Symbol/reference navigation routes `GitNexus -> Simone`.
- Architecture/dependency questions route `GitNexus -> SIN Code`.
- Graphify is reserved for explicit mixed-corpus/code+docs/cross-repository graph questions; it is not an equal automatic code graph.
- Durable decision/rationale recall routes to OpenViking.
- Every routed provider has a bounded runtime specification.
- Maximum provider attempts remain capped at two.

### Persistent provider health

- `sin-context` uses the shared `ProviderRuntime` implementation.
- Timeouts, unavailable executables, failures, cooldowns and persistent circuit-open state are active in the broker path.
- Infrastructure failures are not written into semantic negative cache.
- JSON diagnostics include provider attempt status and duration.
- Provider stderr is retained for diagnostics but excluded from successful context when stdout exists.

### Memory ownership

- **OpenViking is the only canonical durable semantic-memory owner.**
- `sin-memory-write` and the SIN Memory Gateway are the validated write boundary.
- gbrain exports only explicitly curated entries into the canonical OpenViking path.
- There is no automatic OpenViking-to-gbrain reverse sync.
- global-brain remains an on-demand goal/plan/archive store and must not auto-inject or auto-own semantic memory.
- Cognee is legacy/non-automatic projection or forensic compatibility only.
- Tencent MemoryCore remains optional read-only evidence only.
- Automatic transcript extraction into a competing durable store is prohibited.

### Inference and deployment

- OpenViking runs centrally on the always-on OCI host; agents/clients reach the fleet service privately through Tailscale rather than a public brain endpoint.
- The OpenViking root key stays OCI-only; scoped service/user credentials are used by normal clients.
- Embeddings are an independent dependency from text generation and must pass realistic batch/reindex/recall gates.
- OmniRoute is the stable inference gateway.
- FreeToken is an optional Linux/NVIDIA text-LLM worker behind OmniRoute, not a memory store.
- GitNexus remains local to the active checkout so uncommitted working-tree changes remain visible.

### MCP budget

- The canonical default profile remains `minimal`.
- `minimal` contains zero managed MCP servers.
- Task profiles have a hard maximum of one or two servers.
- Legacy Cognee MCP configuration, where retained, is non-default and cannot define memory ownership.
- wow installer, doctor, documentation and SST verification use the same profile semantics.

### Exploration limits

- Default exploration uses one relevant provider/worker.
- A second provider/worker is permitted only for an independent question or bounded fallback.
- Raw worker transcripts are forbidden from entering main context.
- Code understanding begins with GitNexus rather than querying multiple overlapping code graphs.

### Architecture artifacts

The current Brain/Memory/Inference architecture is documented with Archify under `docs/diagrams/`:

- `openviking-fleet.architecture.{json,html,svg}`
- `memory-write.workflow.{json,html,svg}`
- `context-recall.workflow.{json,html,svg}`
- `deployment-topology.architecture.{json,html,svg}`

The JSON IR is canonical/editable, HTML is interactive, and SVG is the README/docs surface.

## Verification contract

The implementation is not considered production-complete from static code inspection. The gates require:

- focused and cross-repo tests;
- `bin/audit-token-architecture.py`;
- the non-polluting memory E2E contract;
- Archify validation/checks;
- `git diff --check`;
- GitNexus `detect-changes` review;
- live OCI proof for OpenViking health, durable commit, non-zero vector indexing, semantic recall, persistence and private client reachability.

No “worldwide number one” claim is supported without independently reproducible competitor evidence.
