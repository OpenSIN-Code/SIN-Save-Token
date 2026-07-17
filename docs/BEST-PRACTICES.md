# Token-Savings Best Practices — Unified Across All Runtimes

**Status:** Canonical. Distributed via `sin-sync` to Claude Code, opencode, Codex, Orca, and all Team-SIN-* agents.
**Owner:** Infra-SIN-OpenCode-Stack
**Last updated:** 2026-07-17
**Tracking:** issue #71

---

## STATUS (2026-07-17) — what's actually done vs. open

**Infrastructure layer: DONE and verified** (`bin/verify-tokens` → ✅ COMPLIANT on primary Mac):
- L1 rtk hook live in Claude Code + opencode plugin + Codex (both homes) + Orca preamble
- L2 MCP thinned 7→4 (deferral rejected: conflicts with /compact)
- L3 claude-mem confirmed single shared worker+DB
- L4 terse contract in Codex AGENTS.md + Orca sub-agent preamble
- Enforcement: verify-tokens wired into sin-sync (fail-loud on drift)

**NOT done — this is NOT yet 100% of state-of-the-art.** Honest gap list:
1. **Behavioral habits, unenforced** — /clear between features, spec-in-one-session/implement-in-another, /compact discipline. Documented as the highest-ROI *free* lever; currently not baked into any runtime rule.
2. **CLAUDE.md / AGENTS.md tightness (L4 input side)** — not audited. The stack AGENTS.md is 77 KB and loads every call. Converting procedure-heavy rules → on-demand skills is the documented "41% always-loaded reduction" and hasn't been done.
3. **Prompt caching** — never verified it's actually active (auto with cache_control header). Free, big, unchecked.
4. **Phase 4 frontier tools** — context-mode (BM25 on compaction, ~98% claim), Headroom (AST-aware), SWE-Pruner/LaMR (~39% SWE-Bench). All backlog, none piloted.
5. **Fleet rollout** — verify-tokens only proven on the primary Mac. OCI VM + other machines unverified.
6. **Baseline measurement** — no before/after `/context` + `ccusage` numbers captured, so savings are directional, not proven per the plan's own "measure your own numbers" rule.
7. **Session-start enforcement** — verify-tokens not yet called at session start to surface drift.

**Bottom line:** the plumbing is 100%; the *practice* (habits + measurement + input-side trimming + fleet-wide proof) is the remaining work. See §3 Phase 3–4.

---

## 0. TL;DR — the one rule

**Every runtime gets the same four layers, wired the same way, verified the same way.**
No runtime is special. If a layer can't run in a runtime, that's a gap to close, not an exception to accept.

| Layer | Target | Tool (canonical) | Where it runs |
|-------|--------|------------------|---------------|
| **1. Shell output** | CLI noise (git/test/build) | **rtk** (Rust Token Killer) | Claude Code, opencode, Codex, Orca |
| **2. MCP/tool schema** | Tool-def overhead per call | **tool-search deferral** + `sin-code serve --compress-tools` (ponytail) | All MCP hosts |
| **3. Context/memory** | Re-sent history, file re-reads | **claude-mem** + **SIN-Brain** + `sin-code compress` | All |
| **4. Output style** | Model's own verbosity | **verbosity mode** (`terse`/`ultra`, caveman-derived) | All |

---

## 1. Current state (audit 2026-07-17)

**What's already working:**
- `rtk 0.39.0` — **41.2M tokens saved (73.4%)** over 18,539 commands. Proven.
- `claude-mem v13.11.0` — active in Claude Code **and** opencode (mirrored plugin).
- Custom suite already supersedes the popular third-party tools:
  - `SIN-Code-Grasp-Tool` / `SIN-Code-SCKG-Tool` ⟶ = code-review-graph / knowledge-graph class
  - `SIN-Code-Scout-Tool` ⟶ = semantic-grep class (replaces naive grep+read)
  - `sin-code compress` (v3.18.0) ⟶ = memory compaction
  - `internal/mcpcompress/` ponytail tags (v3.19.0) ⟶ = MCP schema compressor
  - `verbosity mode` (v3.17.0) ⟶ = caveman class

**The gaps (this is the whole job):**
1. ❌ **rtk hook NOT installed globally.** `rtk gain` warns `No hook installed`. All 41M savings came from *explicit* `rtk` calls, not transparent rewriting. RTK.md *claims* auto-rewrite — currently false.
2. ❌ **No tool-search deferral** anywhere (`ENABLE_TOOL_SEARCH` unset in Claude Code, opencode, Codex). MCP tool schemas load in full on every call — the single biggest documented drain.
3. ❌ **No unified enforcement.** Each runtime is configured by hand; drift is guaranteed. Nothing verifies the four layers are actually live.
4. ⚠️ **claude-mem runs in two runtimes** — confirm they share one DB, not double-spend.

---

## 2. The standard every runtime must meet

A runtime is **compliant** when all four checks pass:

```
[L1] rtk hook installed & transparent      → `rtk gain` shows NO "No hook installed" warning
[L2] MCP servers thinned (NOT deferral)    → only actively-used servers always-on; `/compact` intact
[L3] memory layer shares one backend       → claude-mem/SIN-Brain single source of truth
[L4] verbosity mode default = terse        → model output compressed by default
```

### 2.1 Claude Code
- **L1:** `rtk init -g` (installs PreToolUse hook globally). Verify: `rtk gain` clean.
- **L2:** do NOT set `ENABLE_TOOL_SEARCH=1` — it conflicts with `/compact` (Claude Code 2.1.208: `defer_loading=true` tools cannot carry `cache_control`, so compact throws 400). `/compact` + prompt caching wins. **Instead, thin always-on MCP servers** — each unused server = 500–1,500 tokens/call. Prefer CLI+skill over always-on MCP (e.g. Playwright CLI, not Playwright MCP). On 2026-07-17 dropped `skyvern`, `chrome-devtools`, `canva` from the active 7 → kept `serena`, `tavily`, `context7`, `vercel`; snapshot at `~/.claude/mcpServers.snapshot-*.json` for reversible restore.
- **L3:** keep `claude-mem`. Confirm DB path shared with opencode.
- **L4:** CLAUDE.md stays tight (bullets only; push procedures into on-demand skills — documented 41% always-loaded reduction).

### 2.2 opencode
- **L1:** rtk via PreToolUse-equivalent plugin hook. Verify against same `rtk gain` ledger.
- **L2:** `sin-code serve --compress-tools` (ponytail compressor) for the 47-tool manifest; `--compress-tags delete|stdlib|native|yagni|shrink`.
- **L3:** `claude-mem.js` plugin already present — point at shared DB.
- **L4:** verbosity mode `terse` via `sin-code` style renderer.

### 2.3 Codex
- **L1:** rtk wrapper at the shell layer (Codex runs `danger-full-access` sandbox — rtk sits in front of exec).
- **L2:** Codex tool manifest through the same ponytail compressor where MCP is bridged (`codex-responses-bridge`).
- **L3:** bridge memory into SIN-Brain so Codex sessions aren't a memory island.
- **L4:** `model_reasoning_effort` already `medium`; add terse output contract in `~/.codex/AGENTS.md` (currently empty — fill it).

### 2.4 Orca (orca-sin-team)
- Orca orchestrates sub-agents → **biggest multiplier**: every sub-agent inherits or wastes the budget.
- Enforce all four layers in the Orca team template so spawned agents are compliant by construction.
- Scoped sub-agent contexts (narrow instructions) — never hand a sub-agent the full context.

---

## 3. Rollout plan (phased, verifiable)

### Phase 1 — Close the critical gap (today, ~15 min)
1. `rtk init -g` → transparent hook live in Claude Code.
2. Verify: `rtk gain` shows no warning; run a `git status`, confirm it routes through rtk.
3. Enable tool-search deferral in Claude Code; measure with `/context` before/after.
4. Confirm claude-mem DB is shared (not duplicated) between Claude Code + opencode.

### Phase 2 — Unify the other runtimes (this week)
5. Add rtk layer to opencode, Codex, Orca; all report to the **same** `rtk gain` ledger.
6. Turn on `sin-code serve --compress-tools` wherever MCP manifests are served.
7. Fill `~/.codex/AGENTS.md` with the terse output contract + layer requirements.
8. Bake all four layers into the Orca team template.

### Phase 3 — Enforce & prevent drift (this week)
9. Ship a `sin-sync` check: `sin-sync verify-tokens` that runs the four L-checks per runtime and fails loud on regression.
10. Add to session-start protocol (understand-anything already runs every session) — surface non-compliance as a warning.
11. Baseline + measure: record `rtk gain` + `/context` numbers weekly; treat percentages as directional, verify against own ledger.

### Phase 4 — Evaluate frontier additions (backlog, measure before adopting)
- **context-mode** (BM25 retrieval on compaction, ~98% context-bloat reduction claimed) — complements rtk (rtk=CLI, context-mode=MCP output). Pilot in one runtime.
- **Headroom** (AST-aware CodeCompressor, reversible, 60–95% claimed) — overlaps our Grasp/SCKG; only adopt if it beats them on our own benchmark.
- **SWE-Pruner / LaMR** (learned context pruning, ~39% on SWE-Bench, preserves syntactic validity) — research-grade; watch, don't integrate yet.
- **token-optimizer-mcp** (smart_read/grep/glob via SQLite cache) — overlaps Scout-Tool; benchmark head-to-head before adding.

---

## 4. Principles (why this works)

1. **Stack layers, don't pick one.** Each layer targets a different token source (shell / schema / history / output) — they compound, they don't compete.
2. **Filter before the model sees it.** A passing test needs 1 line, not 155. A successful `npm install` needs 15 lines, not 4,000.
3. **CLI + skill > always-on MCP.** MCP schemas tax every call; a CLI's `--help` + JSON output is all an agent needs, and you control the supply chain.
4. **Measure your own numbers.** Every headline % in the wild is a vendor dashboard. Our `rtk gain` ledger is ground truth.
5. **One backend per concern.** One rtk ledger, one memory DB, one compressor. Duplication silently doubles spend.
6. **Enforce by construction.** Drift is the enemy. `sin-sync` distributes; a verify step keeps every runtime honest.

---

## 5. Sources (2026 landscape, for reference)

- [How I Cut Claude Code Token Usage by 90%+ (Abid Abdul Gafoor)](https://medium.com/@abdulgafoorabid/how-i-cut-claude-code-token-usage-by-90-with-4-tools-custom-hooks-and-enforcement-d3f8d2488cd6)
- [12 Ways to Cut Token Consumption in Claude Code (Firecrawl)](https://www.firecrawl.dev/blog/claude-code-token-efficiency)
- [token-optimizer-mcp (GitHub)](https://github.com/ooples/token-optimizer-mcp)
- [Reduce Token Usage in Claude Code and Opencode (toorhamza)](https://toorhamza.com/blog/reduce-token-usage-claude-code-opencode)
- [Claude Code Token Optimization: 19 Changes (buildtolaunch)](https://buildtolaunch.substack.com/p/claude-code-token-optimization)
- [8 Open Source Tools to Slash AI Coding Agent Token Usage (Pinggy)](https://pinggy.io/blog/tools_to_reduce_ai_coding_agent_token_usage/)
- [Headroom: Cut LLM Token Costs 60-95% (SaaSCity)](https://saascity.io/blog/headroom-cut-llm-token-costs-60-95-ai-agents)
- [How I Cut Token Usage 120x With a Code Knowledge Graph (DEV)](https://dev.to/deusdata/how-i-cut-my-ai-coding-agents-token-usage-by-120x-with-a-code-knowledge-graph-4a3d)
- [SWE-Pruner: Self-Adaptive Context Pruning (arXiv)](https://arxiv.org/pdf/2601.16746)
- [LaMR: Multi-Rubric Latent Reasoning (arXiv)](https://arxiv.org/pdf/2605.15315)

---

## #2 + #3 findings (2026-07-17, session 2)

**#3 Prompt caching — ✅ CONFIRMED ACTIVE (no work needed).**
Direct test against the gateway (`localhost:8788`) returned `cache_creation_input_tokens` / `cache_read_input_tokens` in usage → gateway preserves `cache_control`; Claude Code sets the headers automatically. Free win already in place.

**#2 Input-side trimming — SCOPED, NOT YET DONE (deferred: needs a fresh session, real risk).**
Measured always-loaded instruction files:
- Claude Code `CLAUDE.md` 657 B, Codex `AGENTS.md` 1.4 KB → already tight ✅
- **opencode `AGENTS.md` = 76 KB (~19k tokens loaded EVERY call)** 🚨 — the one real lever.
  - ~40 KB is pure reference: §6 Repo layout (12 KB), §7 Config contract (13 KB), §8 Roadmap (9 KB), §12 Eval/OTel (6 KB).
  - ~30 KB is behavioral rules that must stay inline (§0 session-start, §3 hard mandates, §10 naming).

**Why deferred, not done:** AGENTS.md is NOT a plain file. `hooks/sync_agents_md.sh` 3-way-syncs it (source repo ↔ Live config ↔ Infra-Stack) with an `is_canonical_source()` guard, and `sin-sync` rsyncs it 1:1 to the fleet (Mac = source of truth) while asserting the ceo-audit mandate survives. Live (76,449 B) and Infra-Stack (77,926 B) copies already differ. Extracting 40 KB safely requires:
  1. Identify the true canonical source repo driving the sync.
  2. Extract reference → `docs/{repo-layout,config-contract,roadmap,eval-observability}.md`, leave 1-line pointers.
  3. Preserve the ceo-audit mandate section (sin-sync asserts it).
  4. Run `sync_agents_md.sh` once so Live + Infra + source converge on the trimmed version.
  5. `verify-tokens` + a `/context` before/after to prove the ~10k-token/call drop.

Estimated win: **~10k tokens per opencode call** — the single biggest remaining lever. Do this first in the next session.
