# Next Agent

## Canonical boundary

SIN-Save-Token owns the Context/Memory control plane: `sin-context`, `sin-memory-write`, `openviking-recall`, `lib/sin_memory_gateway.py`, evidence/secret/provenance gates, retrieval budgets, receipts and tests.

Fleet deployment/distribution is canonical in sibling repo `wow-my-zsh`: shared agent rules/installers, GitNexus rollout, OCI/Tailscale service discovery, OpenViking deployment/client distribution, OmniRoute and FreeToken. Do not recreate those definitions here.

## Completion contract

Preserve unrelated dirty Orca/Web callback work. Run repository-native tests, `git diff --check`, architecture audit/E2E where applicable, and `gitnexus detect-changes --scope all` before claiming completion.

## Callback Broker C-lite

The durable callback transport implementation is canonical here: `lib/sin_orca/callback_broker.py`, `callback_broker_service.py`, `callback_transports.py`, `callback_cli.py`, and `bin/sin-callback`. The signed repository callback remains the only authorization/TTL/completion authority; the broker is reconstructible transport state only.

Operational invariants to preserve:
- one global per-user SQLite/WAL broker, not one daemon per callback;
- no retry-attempt ceiling; canonical callback TTL is the lifetime governor;
- `sent`/`indeterminate` are receipt-watched and never blindly retransmitted;
- exact OpenCode `ses_*`, Prime `activeSessionId`, and DSH top-level `sessionId` are never substituted;
- configured OpenCode loopback API uses exact session+repository verification followed by `/prompt_async`; ambiguous post-send failures are `indeterminate` and never fall back;
- broker/API/CLI never persist or expose callback capability tokens, callback message bodies, HMAC material or credentials;
- `sin-callback doctor` must remain fail-closed on DB/schema/API health and enforce private local state permissions.

Fleet publication, launch/runtime discovery and SIN-GPT-Web doctor wiring remain in sibling `wow-my-zsh`; do not duplicate platform ownership here.

<!-- SIN-GPT-WEB-HANDOVER
task: T-0001
updated: 2026-08-25T23:35:50+00:00
actor: prime-agent
evidence-sha256: 1791bc4eff4a6866029e91fd3424ddd89c62e16ae237b612e1afd10aada51852
-->
