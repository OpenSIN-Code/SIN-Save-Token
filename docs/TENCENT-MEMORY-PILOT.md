# TencentDB Agent Memory Pilot

## Decision

TencentDB Agent Memory is an **optional read-only team-memory provider**. It does not replace Cognee, gbrain, `sin-memory`, or the `sin-context` routing policy.

Canonical ownership remains:

- gbrain: fast local/curated staging.
- Cognee: canonical durable domain memory.
- `sin-memory`: repository-local L1/L2/L3 artifacts and explicit decisions.
- Tencent MemoryCore: optional team-memory lookup and scenario browsing only.

The assessed upstream is pinned to commit `fe3230f176f1bf5832fee79d12494bbc2d19a8aa` from `https://github.com/TencentCloud/TencentDB-Agent-Memory.git`.

## Security boundary

The adapter in `lib/sin_tencent_memory.py` intentionally implements only:

- `GET /health`
- `POST /v3/atomic/search`
- `POST /v3/scenario/ls`

It intentionally does **not** implement:

- `/v3/conversation/add` or any L0 conversation ingestion/search path
- `/v3/core/read` or other L3 persona/profile access
- any write/import/seed/capture endpoint
- automatic prompt injection
- automatic routing from `sin-context`

MemoryCore v3 does not expose a direct curated atomic-write method in the assessed client. Its write path is conversation ingestion, which conflicts with SIN's no-raw-chat boundary. Therefore Tencent writes are unsupported rather than merely hidden behind a flag.

Outbound search queries are rejected when they look like credentials, Authorization headers, private keys, or token assignments. Responses are bounded in size. Remote endpoints are disabled by default; if explicitly allowed later, HTTPS and an API key are mandatory.

## Default state

`config/tencent-memory.json` ships disabled, with writes disabled, remote access disabled, and a loopback base URL (`http://127.0.0.1:8420`). `config/provider-runtime.json` knows the `tencent-memory` provider, but `config/context-policy.json` does not route any task to it. Registration is not activation.

## Local pilot

Run a MemoryCore build matching the pinned assessed commit and bind it only to loopback port `8420`. Verify without enabling it with `sin-tencent-memory status`. Enable only for the current process/session using `SIN_TENCENT_MEMORY_ENABLED=1`, then use `sin-tencent-memory health`, `sin-tencent-memory search "architecture decision" --limit 5`, or `sin-tencent-memory scenarios`.

Do not place API keys in command-line arguments. If auth is enabled, provide it via the environment variable named by `auth.api_key_env` (default `TENCENT_MEMORY_API_KEY`).

## What must never be sent

- raw chat transcripts or complete conversation turns
- passwords, API keys, bearer tokens, cookies, OAuth material, private keys
- raw tool output or environment dumps
- system/developer prompts
- persona/profile data
- transient chain-of-thought or worker reasoning

## Promotion policy

Tencent results are evidence candidates only. They do not automatically become canonical SIN memory. Durable decisions still pass through the existing curated writer and Cognee ownership boundary.

## Rollback

Rollback is immediate and data-safe because the provider is not canonical and is not auto-routed: disable `SIN_TENCENT_MEMORY_ENABLED`, stop the local MemoryCore process/container, remove the optional provider files/config if retiring the pilot, and rerun repository tests plus `bin/audit-token-architecture.py`. No Cognee/gbrain migration is required because the pilot never owns canonical memory and performs no writes.

## Promotion gate for future write support

Do not implement Tencent writes unless a future pinned MemoryCore release provides a direct, authenticated, tenant-isolated atomic-memory write API that accepts only pre-curated records. A new review must prove there is no conversation-ingest requirement, SIN type allowlists and redaction run before transport, export/backup/delete are tested, tenant isolation and auth are verified, and rollback remains complete.
