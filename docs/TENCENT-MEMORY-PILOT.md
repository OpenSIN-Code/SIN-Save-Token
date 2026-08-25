# TencentDB Agent Memory Pilot

## Decision

TencentDB Agent Memory is an **optional read-only evidence provider**. It does not replace OpenViking, GitNexus, gbrain/global-brain, `sin-memory`, or the `sin-context` routing policy.

Canonical ownership is:

- **OpenViking:** fleet-wide durable semantic memory.
- **GitNexus:** code structure for the repository checkout currently being edited.
- **gbrain/global-brain:** curated staging, goals, plans and archive only; not automatic semantic-memory truth.
- **`sin-memory`:** repository-local operational L1/L2 artifacts and receipts; durable decisions are promoted through `sin-memory-write` to OpenViking.
- **Tencent MemoryCore:** optional team-memory lookup/scenario browsing only.

The assessed upstream is pinned to commit `fe3230f176f1bf5832fee79d12494bbc2d19a8aa` from `https://github.com/TencentCloud/TencentDB-Agent-Memory.git`.

## Security boundary

The adapter in `lib/sin_tencent_memory.py` intentionally implements only:

- `GET /health`
- `POST /v3/atomic/search`
- `POST /v3/scenario/ls`

It intentionally does **not** implement:

- `/v3/conversation/add` or any L0 conversation-ingestion/search path;
- `/v3/core/read` or other L3 persona/profile access;
- any write/import/seed/capture endpoint;
- automatic prompt injection;
- automatic routing from `sin-context`.

MemoryCore v3 does not expose the curated atomic-write contract SIN requires. Its conversation-ingest write path conflicts with SIN's no-raw-chat boundary, so Tencent writes remain unsupported rather than hidden behind a feature flag.

Outbound search queries are rejected when they resemble credentials, Authorization headers, private keys or token assignments. Responses are bounded. Remote endpoints are disabled by default; if explicitly allowed later, HTTPS and an API key are mandatory.

## Default state

`config/tencent-memory.json` ships disabled, with writes disabled, remote access disabled and a loopback base URL (`http://127.0.0.1:8420`). `config/provider-runtime.json` may know the provider, but `config/context-policy.json` does not route normal tasks to it. Registration is not activation.

## What must never be sent

- raw chat transcripts or complete conversation turns;
- passwords, API keys, bearer tokens, cookies, OAuth material or private keys;
- raw tool output or environment dumps;
- system/developer prompts;
- persona/profile data;
- transient chain-of-thought or worker reasoning.

## Promotion policy

Tencent results are untrusted evidence candidates only. They do not automatically become canonical SIN memory. Any durable fact/decision must pass the normal `sin-memory-write` → SIN Memory Gateway → OpenViking fail-closed write path.

## Rollback

Rollback is data-safe because Tencent is neither canonical nor auto-routed: disable `SIN_TENCENT_MEMORY_ENABLED`, stop the MemoryCore process/container and rerun repository tests plus `bin/audit-token-architecture.py`. No canonical-memory migration is required because Tencent owns no durable writes.

## Promotion gate for future write support

Do not implement Tencent writes unless a future pinned release provides a direct, authenticated, tenant-isolated atomic-memory write API that accepts only pre-curated records. A new review must prove there is no conversation-ingest requirement, SIN type allowlists/redaction run before transport, export/backup/delete are tested, tenant isolation/auth are verified and rollback remains complete. Even then, promotion would be behind the SIN Memory Gateway and would require an explicit architecture decision; it must not silently become a second canonical memory.
