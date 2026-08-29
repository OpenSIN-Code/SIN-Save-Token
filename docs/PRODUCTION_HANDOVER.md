# Production Handover

OpenViking remains the only canonical durable semantic-memory backend. The SIN Memory Gateway remains fail-closed and is implemented only in this repository. Production OCI/Tailscale/OpenViking service deployment and OmniRoute/FreeToken infrastructure are owned by `wow-my-zsh`; runtime secrets and persistent OpenViking data remain outside Git.

The cross-repository ownership consolidation intentionally moved the fleet-level OpenViking and deployment Archify artifacts to `wow-my-zsh/docs/diagrams/`. SST keeps only its Memory-Write and Recall/Context workflow artifacts as canonical local diagrams.

## Callback Broker C-lite production contract

The durable callback broker is implemented here and deployed per user through `bin/sin-callback install`. Its default state is `~/.local/state/sin-orca/callback-broker.sqlite3` (SQLite/WAL, mode 0600), with a loopback control-plane token in the same private 0700 state directory (mode 0600). `sin-callback doctor` is the production readiness gate and must report DB integrity `ok`, schema 2, private modes, a reachable loopback API and no issues.

The broker owns transport persistence only. Signed/HMAC-bound repository callback records continue to own capability validation, task/round/repository/origin identity, TTL and completion. OpenCode exact-session API delivery, Prime Agent exact `activeSessionId`, and DeepSeek Harness exact top-level `sessionId` must never be silently substituted. `sent` or `indeterminate` deliveries wait for canonical receipt/TTL reconciliation without retransmission.

`wow-my-zsh` publishes the canonical `SIN-Save-Token/bin/sin-callback` into the fleet and includes it in SIN-GPT-Web doctor checks. macOS uses the per-user `com.sin-orca.callback-broker` LaunchAgent; Linux uses the user-scoped `sin-callback-broker.service` systemd unit. No service file contains callback capabilities or upstream credentials.

OCI production acceptance was completed on `sin-supabase` on 2026-08-29 from SST `5eb5a09cba2d56f5aa2d702e2bbd104f4f5d3b52`: the Ubuntu user service is `enabled` and `active (running)`, `loginctl` reports `Linger=yes`, `NRestarts=0`, `ExecMainStatus=0`, and `sin-callback doctor` reports `ok=true`, schema 2, SQLite integrity `ok`, state directory `0700`, DB/token `0600`, and `issues=[]`. The operator CLI is published at `~/.local/bin/sin-callback`; the unit remains loopback-only on `127.0.0.1:61369` and contains no credentials.

<!-- SIN-GPT-WEB-HANDOVER
task: T-0001
updated: 2026-08-25T23:35:50+00:00
actor: prime-agent
evidence-sha256: 1791bc4eff4a6866029e91fd3424ddd89c62e16ae237b612e1afd10aada51852
-->
