# SIN Callback Broker C-lite — Implementation Plan

**Goal:** Make callback completion durable and self-healing across OpenCode, Prime Agent, and DeepSeek Harness without making the broker a second completion authority.

**Architecture:** The canonical signed callback record in `sin_orca.web_callbacks` remains authoritative. A local SQLite/WAL broker stores transport-only identity and leases, continuously reconciles exact-session delivery, and exposes one loopback API plus the `sin-callback` CLI. Delivery is bound to the exact recorded OpenCode session, Prime `activeSessionId`, or DSH top-level `sessionId`; no adapter may guess or switch targets. The broker never stores callback tokens, messages, summaries, credentials, or HMAC material.

## Task 1 — Lock reliability invariants with tests

**Files:**
- Modify: `tests/test_callback_broker.py`
- Modify: `tests/test_unbounded_callback_governors.py`

Add failing tests for:
- no delivery-attempt ceiling; callback TTL is the only automatic lifetime governor;
- expired broker leases remain safely recoverable;
- broker never locally invents canonical expiry;
- exact `/callbacks/:id/reconcile` can only claim the requested delivery;
- sent/indeterminate callbacks are watched for ACK/TTL without retransmission;
- repository registration/sync recovers a canonical pending callback after broker restart;
- broker API auth and redaction;
- broker state survives process restart;
- OpenCode, Prime Agent, and DSH exact target identity is never substituted.

## Task 2 — Harden durable broker store

**Files:**
- Modify: `lib/sin_orca/callback_broker.py`

Implement schema v2 with:
- unbounded attempt counter (observability only, never a stop condition);
- repository registry for crash recovery;
- exact `claim_delivery(delivery_id)` in addition to batch `claim_due()`;
- no local TTL terminal transition; canonical callback code owns expiry;
- receipt-watch scheduling for `sent` and `indeterminate` rows;
- idempotent schema migration from v1.

## Task 3 — Harden broker service/API

**Files:**
- Modify: `lib/sin_orca/callback_broker_service.py`
- Modify: `lib/sin_orca/callback_cli.py`

Implement:
- one `run_cycle()` that syncs registered repositories, drains due deliveries, and reconciles sent/indeterminate receipts;
- exact-ID reconcile endpoint that cannot drain an unrelated delivery;
- public `/health` with non-sensitive readiness only;
- authenticated list/inspect/reconcile/drain/sync endpoints;
- doctor result that is only healthy when DB integrity, file permissions, and service health are healthy;
- graceful bounded service loop and explicit JSON status.

## Task 4 — Exact-session transport hardening

**Files:**
- Modify: `lib/sin_orca/callback_transports.py`
- Modify only as needed: `lib/sin_orca/web_callbacks.py`

OpenCode:
- prefer an explicitly configured loopback OpenCode server (`SIN_OPENCODE_CALLBACK_URL`) and `POST /session/:id/prompt_async` after exact-session verification;
- support HTTP Basic auth from environment only; never persist credentials;
- fall back to `opencode run --session <exact-id>` when no broker server is configured;
- timeout/ambiguous failures become `indeterminate`, never blind retry.

Prime Agent:
- retain exact `activeSessionId` binding and validate the returned receipt target.

DeepSeek Harness:
- retain exact top-level `sessionId` and loopback `session.prompt` RPC binding.

No adapter may select a different session when the exact target is offline.

## Task 5 — Canonical callback integration and crash recovery

**Files:**
- Modify: `lib/sin_orca/web_callbacks.py`
- Modify: `tests/test_web_callbacks.py`

Ensure:
- repository is registered with the broker before/while callback lifecycle begins;
- persisted pending callbacks enqueue idempotently;
- ACK/cancel/abandon/expiry mirror to broker;
- broker is preferred for new durable retry while legacy per-callback relay remains backward-compatible;
- canonical signed callback record remains the only completion/expiry authority.

## Task 6 — Service management and fleet shim

**Files:**
- Modify: `bin/sin-callback`
- Modify: `docs/*` in this repository as needed
- Modify in sibling `wow-my-zsh`: `bin/sin-callback`, `docs/callback-broker.md`, `shared/skills/sin-gpt-web/scripts/doctor.sh`, relevant install/distribution docs/tests

Implement:
- macOS LaunchAgent install/uninstall;
- Linux user-systemd install/uninstall (create-only local user service, no root requirement);
- one global broker per user, not one service per callback;
- `wow-my-zsh` remains the fleet/distribution owner and delegates runtime semantics to canonical `SIN-Save-Token/bin/sin-callback`.

## Task 7 — Verification

Run:
- focused broker tests;
- callback/web callback tests;
- unbounded-governor tests;
- repo architecture/ownership tests;
- `git diff --check`;
- GitNexus `detect-changes --scope all`;
- `sin-callback doctor` with the service installed;
- an end-to-end synthetic pending callback recovery test for exact OpenCode/Prime/DSH targets using mocks or local test fixtures only (no live user callback injection).

Update `README.md`, `docs/architecture.md`, `docs/NEXT_AGENT.md`, and `docs/PRODUCTION_HANDOVER.md` with the final operational contract. Preserve all unrelated dirty work.