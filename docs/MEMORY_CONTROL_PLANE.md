# SIN Memory Control Plane

Status: **OpenViking-first control plane implemented.** Hermetic gateway tests are the default regression gate; live OCI proof is an additional operational gate and must include semantic recall, not only a successful write.

## Canonical architecture

OpenViking is the **only canonical durable semantic-memory owner** for the SIN fleet. The SIN Memory Gateway is the governance boundary in front of durable writes; it is not a second memory database. GitNexus remains the canonical code-intelligence graph for the repository checkout currently being edited.

Fleet-level OpenViking/OCI/Tailscale topology is canonical in `wow-my-zsh/docs/MEMORY-PLATFORM.md`; this document is the canonical semantic/governance contract for the Memory Control Plane.

## Write path

![Fail-closed Memory-Write Flow](diagrams/memory-write.workflow.svg)

The production write contract is:

1. `sin-memory-write` accepts a curated durable-memory candidate.
2. The gateway validates type, provenance, evidence hash and content.
3. Secret-shaped or speculative content is rejected before persistence.
4. The OpenViking backend creates a session, adds the message and commits it.
5. The exact OpenViking commit task must reach `completed`.
6. Only then may the gateway return a `COMMITTED` receipt.
7. Local SQLite stores receipt/audit metadata only; it is not a second semantic-memory truth.

A backend exception, incomplete task or malformed receipt is a failed write. There is **no automatic Cognee, Tencent, SIN-Brain or global-brain fallback**.

## Recall path

![Recall and Context Flow](diagrams/context-recall.workflow.svg)

`sin-context` is the sole automatic context router. Durable decision/rationale recall routes to OpenViking. Code-symbol and code-architecture questions route to GitNexus first. `global-brain` is read only when plan/archive context is explicitly useful. This avoids the previous anti-pattern of querying multiple overlapping “brains” and merging their answers.

## Deployment boundary

Production topology is owned by `wow-my-zsh`; SST consumes it under this interface contract:

- **OCI:** OpenViking, persistent memory data, SIN Memory Gateway, OmniRoute and the small embedding service.
- **Tailscale:** private fleet transport; no public OpenViking/Funnel exposure.
- **Macs / agent hosts:** `sin-context`, `sin-memory-write` clients and repo-local GitNexus indexes.
- **OmniRoute:** stable OpenAI-compatible inference gateway for OpenViking/agents.
- **FreeToken:** optional Linux x86_64 + NVIDIA text-LLM worker behind OmniRoute; it is not required on ARM64 OCI or macOS and is never a memory owner.

The OpenViking root key remains OCI-only. Normal clients use scoped account/user/service credentials. Repository files never contain runtime credentials.

## Backend decisions

- **OpenViking is canonical.** `OpenVikingCLIBackend` waits for the exact session-commit task before acknowledging persistence.
- **Embeddings and text inference are separate concerns.** Durable retrieval must not depend on a free-tier embedding route that rate-limits realistic OpenViking batches.
- **OmniRoute abstracts inference.** OpenViking and agents should not bind directly to a specific large text model or GPU worker.
- **FreeToken is an optional preferred GPU text engine.** It belongs behind OmniRoute on suitable Linux/NVIDIA hardware.
- **Cognee is legacy/non-automatic.** It may remain for migration/forensic projection only.
- **Tencent MemoryCore is optional read-only evidence.** It is not auto-routed and has no durable write authority.
- **Honcho is a sidecar, not a writer.** Behavioral-memory tooling cannot bypass the canonical write boundary.

## Fail-closed invariants

1. **Validation before persistence.** `source` and `actor` are safe identifiers; `evidence_sha256` is a lowercase 64-character SHA-256 value.
2. **Speculation is rejected.** “maybe”, “guess”, “unverified”, “vielleicht” and equivalent speculative markers cannot become canonical records.
3. **Secrets are rejected.** API keys, bearer tokens, private keys and credential-shaped strings are refused at the boundary.
4. **Success requires a typed receipt.** The receipt binds record id, content hash, backend reference and committed timestamp.
5. **Commit completion is mandatory.** A successful session/message call is insufficient; the exact asynchronous commit task must report completion.
6. **Backend failure is never success.** No silent fallback can manufacture a success receipt.
7. **Recall excludes inactive records.** Superseded/inactive records are not returned by default.

## Key types

| Type | Purpose |
|---|---|
| `CanonicalMemoryRecord` | Immutable evidence-bearing record with provenance and content hash |
| `PersistenceReceipt` | Typed receipt binding record id/hash to the OpenViking backend reference |
| `MemoryBackend` | Backend abstraction; production is OpenViking, tests use in-memory |
| `SinMemoryGateway` | Fail-closed facade: validate → commit → verify receipt |

## Runtime evidence required for a production claim

A complete live proof is deliberately stronger than “the HTTP server is up”:

- OpenViking health is green and auth is enabled.
- Persistent data survives container/service restart.
- The dedicated `sin-fleet` service identity can write without the root key.
- A canary session commit reaches `completed`.
- Its extracted memory obtains a vector index entry.
- Semantic search recalls that canary through the normal service identity.
- Mac-M1 can reach the private Tailscale endpoint.

The repository E2E/audit gates must fail rather than claim success when one of these mandatory facts is unknown.

## Verification

```bash
rtk pytest -q tests/test_sin_memory_gateway.py tests/test_memory_write.py tests/test_sin_context.py tests/test_archify_exporter.py
rtk python3 bin/audit-token-architecture.py
rtk bash bin/e2e-memory-test.sh
rtk git diff --check
```

Archify artifacts are validated from their JSON IR and rendered to HTML/SVG; generated HTML/SVG are never hand-edited.
