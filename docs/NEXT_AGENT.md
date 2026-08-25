# Next Agent

## Canonical boundary

SIN-Save-Token owns the Context/Memory control plane: `sin-context`, `sin-memory-write`, `openviking-recall`, `lib/sin_memory_gateway.py`, evidence/secret/provenance gates, retrieval budgets, receipts and tests.

Fleet deployment/distribution is canonical in sibling repo `wow-my-zsh`: shared agent rules/installers, GitNexus rollout, OCI/Tailscale service discovery, OpenViking deployment/client distribution, OmniRoute and FreeToken. Do not recreate those definitions here.

## Completion contract

Preserve unrelated dirty Orca/Web callback work. Run repository-native tests, `git diff --check`, architecture audit/E2E where applicable, and `gitnexus detect-changes --scope all` before claiming completion.

<!-- SIN-GPT-WEB-HANDOVER
task: T-0001
updated: 2026-08-25T23:35:50+00:00
actor: prime-agent
evidence-sha256: 1791bc4eff4a6866029e91fd3424ddd89c62e16ae237b612e1afd10aada51852
-->
