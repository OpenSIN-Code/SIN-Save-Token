# OpenViking Fleet Memory Finish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. This repository deliberately uses the existing dirty worktree; creating branches/worktrees or committing is out of scope for this run.

**Goal:** Finish the OpenViking-first fleet memory architecture, make it usable across machines through OCI/Tailscale, keep FreeToken behind OmniRoute as the preferred future GPU text backend, and document the complete architecture with Archify diagrams embedded in README/docs.

**Architecture:** OpenViking is the sole canonical durable semantic memory on OCI. SIN Memory Gateway is the fail-closed governance boundary; `sin-context` is the sole automatic context router; GitNexus remains checkout-local canonical code intelligence. OmniRoute decouples OpenViking/agents from inference providers; FreeToken is a replaceable GPU backend, not another brain.

**Tech Stack:** Python 3, OpenViking 0.3.22, Docker, Tailscale Serve, OmniRoute OpenAI-compatible gateway, GitNexus, Archify 2.11, pytest.

**Spec:** `docs/MEMORY_CONTROL_PLANE.md`, `docs/ARCHITECTURE.md`, repository `AGENTS.md`, and the architecture decision established in this workstream.

## Global Constraints

- Preserve unrelated dirty work from parallel agents.
- No commit, push, merge, rebase, branch, worktree, `git reset --hard`, or `git clean -fd`.
- Every shell command in SIN-Save-Token is executed through `/Users/jeremy/.local/bin/rtk`.
- Use `sin-context`/GitNexus first; run GitNexus impact before modifying functions/classes/methods; run GitNexus detect-changes before completion.
- OpenViking root key stays on OCI only; agents must not receive it.
- OpenViking is canonical semantic memory; Cognee/Tencent/SIN-Brain/global-brain cannot become competing automatic writers.
- GitNexus remains local per checkout so dirty-tree intelligence remains authoritative.
- No public OpenViking exposure; use private Tailscale access only.
- Archify JSON is the editable source for diagrams; HTML is the interactive artifact. README/docs may additionally embed optimized generated image assets.
- FreeToken is an inference backend behind OmniRoute and is not required on ARM64 OCI or macOS.

---

### Task 1: Repair and prove OpenViking semantic indexing

**Files:**
- Modify if needed: OCI OpenViking container build/runtime files under `/home/ubuntu/.openviking-oci/`
- Test: targeted container/runtime smoke commands

**Interfaces:**
- Consumes: OpenViking `/api/v1/sessions`, `/api/v1/tasks`, `/api/v1/content/reindex`, `/api/v1/search/search`
- Produces: completed session commit whose memory records are actually indexed and retrievable

- [ ] Reproduce vector insertion failure and retain exact stack trace.
- [ ] Prove whether the `xxhash` string incompatibility is version-specific.
- [ ] Pin/fix the vector hashing dependency at the container build boundary rather than editing installed package code in-place.
- [ ] Select an embedding route that survives the real OpenViking batch workload without free-tier 429 failures.
- [ ] Rebuild/restart the container without losing persistent data.
- [ ] Reindex the canary memory and verify non-zero vector count.
- [ ] Run semantic search and prove the canary is recalled.

### Task 2: Finish private fleet service exposure and client identity

**Files:**
- OCI runtime/config only, secrets outside repositories
- Mac client config outside repositories as necessary

**Interfaces:**
- Produces: tailnet-only OpenViking/gateway route and non-root service credentials

- [ ] Confirm container restart policy, persistence mounts, auth mode, and localhost/private exposure.
- [ ] Keep the OpenViking root key OCI-only.
- [ ] Use the `sin-fleet` account and dedicated `memory-gateway` service user.
- [ ] Add a non-conflicting Tailscale Serve endpoint without overwriting existing 443/6081/20128 routes.
- [ ] Verify Mac-M1 can reach the private endpoint.
- [ ] Configure the client with a user/service key only, never the root key.

### Task 3: Harden the repository OpenViking adapter and tests

**Files:**
- Modify: `lib/sin_memory_gateway.py`
- Modify: `bin/openviking-recall`
- Modify: `bin/sin-memory-write`
- Test: `tests/test_sin_memory_gateway.py`, add focused adapter tests if needed

**Interfaces:**
- Produces: fail-closed write receipt only after commit completion; bounded recall through canonical OpenViking

- [ ] Add failing tests for any production behavior gap discovered by the OCI canary.
- [ ] Run the tests and confirm the intended red state.
- [ ] Make the smallest adapter change required by the real server behavior.
- [ ] Run focused tests to green.
- [ ] Verify no secret appears in command arguments, logs, or repo files.

### Task 4: Normalize architecture/policy contracts

**Files:**
- Modify: `config/context-policy.json`
- Modify: `config/intelligence-providers.json`
- Modify: `config/provider-runtime.json`
- Modify: `bin/e2e-memory-test.sh`
- Modify: `bin/audit-token-architecture.py` only if its contract is genuinely stale
- Modify: stale memory docs such as `docs/IMPLEMENTATION-AUDIT-2026-07-22.md`, `docs/TENCENT-MEMORY-PILOT.md`, `docs/COGNEE-COST-POLICY.md`, `docs/OMNIROUTE-PROVIDERS.md`
- Cross-repo minimal contract updates if audit requires: `global-brain`, `wow-my-zsh`

**Interfaces:**
- Produces: one canonical memory owner and one canonical code graph role, with optional specialists explicitly non-automatic

- [ ] Remove stale claims that Cognee is canonical.
- [ ] Remove stale claims that Graphify is the default architecture/code graph.
- [ ] Replace stale `npx gitnexus` instructions with direct `gitnexus analyze --index-only`.
- [ ] Ensure global-brain advertises OpenViking as canonical provider while remaining plan/archive-only.
- [ ] Ensure Cognee/Tencent are explicit optional/legacy read/projection paths only.
- [ ] Run architecture contract tests and audit.

### Task 5: Create Archify source diagrams and embed generated visuals

**Files:**
- Create: `docs/diagrams/openviking-fleet.architecture.json`
- Create: `docs/diagrams/openviking-fleet.architecture.html`
- Create: `docs/diagrams/memory-write.workflow.json`
- Create: `docs/diagrams/memory-write.workflow.html`
- Create: `docs/diagrams/context-recall.workflow.json`
- Create: `docs/diagrams/context-recall.workflow.html`
- Create: `docs/diagrams/deployment-topology.architecture.json`
- Create: `docs/diagrams/deployment-topology.architecture.html`
- Create: optimized image assets under `docs/assets/architecture/`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/MEMORY_CONTROL_PLANE.md`

**Interfaces:**
- Produces: editable Archify IR + interactive render + README/docs-visible architecture images

- [ ] Read Archify architecture/workflow schemas/examples.
- [ ] Create architecture and workflow IRs matching the implemented system, not aspirational topology.
- [ ] Validate and render every Archify artifact.
- [ ] Import the four generated visual assets into the repository.
- [ ] Embed overview diagram in README and link the detailed diagram set.
- [ ] Embed write/recall/deployment diagrams in the matching docs.
- [ ] Clearly distinguish current production state from optional FreeToken GPU backend.

### Task 6: Verification and completion evidence

**Files:**
- No new implementation unless verification exposes a defect.

- [ ] Run focused memory/context tests.
- [ ] Run architecture/cross-repo contract tests.
- [ ] Run `bin/audit-token-architecture.py`.
- [ ] Run `bin/e2e-memory-test.sh` with the production endpoint where supported.
- [ ] Run Archify validate/check for all new diagrams.
- [ ] Run `git diff --check`.
- [ ] Run `gitnexus detect-changes --scope all` and inspect risk/processes.
- [ ] Report exact verified state and any genuine external blocker; do not claim completion from code inspection alone.
