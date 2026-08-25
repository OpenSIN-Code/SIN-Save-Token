# Production Handover

OpenViking remains the only canonical durable semantic-memory backend. The SIN Memory Gateway remains fail-closed and is implemented only in this repository. Production OCI/Tailscale/OpenViking service deployment and OmniRoute/FreeToken infrastructure are owned by `wow-my-zsh`; runtime secrets and persistent OpenViking data remain outside Git.

The cross-repository ownership consolidation intentionally moved the fleet-level OpenViking and deployment Archify artifacts to `wow-my-zsh/docs/diagrams/`. SST keeps only its Memory-Write and Recall/Context workflow artifacts as canonical local diagrams.

<!-- SIN-GPT-WEB-HANDOVER
task: T-0001
updated: 2026-08-25T23:35:50+00:00
actor: prime-agent
evidence-sha256: 1791bc4eff4a6866029e91fd3424ddd89c62e16ae237b612e1afd10aada51852
-->
