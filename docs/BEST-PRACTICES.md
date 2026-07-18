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

**DONE (2026-07-17 → 2026-07-18):**
- ✅ #2 **L4 input-side surface (§7)** — gated: paths:-scoping governance (41% documented), skill-sprawl budget (30/8k), disable-model-invocation selective, slash-first habit, .claudeignore adopted. opencode AGENTS.md already trimmed 76KB→20KB; size-gate prevents re-bloat.
- ✅ #3 **Prompt caching** — confirmed active via gateway logs (cache_creation/cache_read tokens verified).
- ✅ #6 **Baseline measurement** — `ccusage` + L0 subagent-model gate integrated into verify-tokens [L0] block.
- ✅ #7 **Session-start enforcement** — verify-tokens now called at session start (heal hook regenerated); drift surfaces LOUD, healthy hosts silent.

**Remaining open (deferred pending decision/fleet-scale):**
1. **Behavioral habits, unenforced** — /clear between features, spec-in-one-session/implement-in-another, /compact discipline. Documented as the highest-ROI *free* lever; currently not baked into any runtime rule.
4. **Phase 4 frontier tools** — context-mode (BM25 on compaction, ~98% claim), Headroom (AST-aware), SWE-Pruner/LaMR (~39% SWE-Bench). All backlog, none piloted.
5. **Fleet rollout** — verify-tokens only proven on the primary Mac. OCI VM + other machines unverified.

**Bottom line:** the plumbing is 100%, input-side governance gated, session-start drift detection live. The *remaining* work is behavioral habits + frontier pilot + fleet validation. See §3 Phase 3–4.

---

## 0. TL;DR — the one rule

**Every runtime gets the same four layers, wired the same way, verified the same way.**
No runtime is special. If a layer can't run in a runtime, that's a gap to close, not an exception to accept.

| Layer | Target | Tool (canonical) | Where it runs |
|-------|--------|------------------|---------------|
| **1. Shell output** | CLI noise (git/test/build) | **rtk** (Rust Token Killer) | Claude Code, opencode, Codex, Orca |
| **2. MCP/tool schema** | Tool-def overhead per call | **tool-search deferral** + `sin-code serve --compress-tools` (ponytail) | All MCP hosts |
| **3. Context/memory** | Re-sent history, file re-reads | **claude-mem** + `sin-code compress` | All |
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
[L3] memory layer shares one backend       → claude-mem single source of truth
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
- **L3:** bridge memory into claude-mem so Codex sessions aren't a memory island.
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

**#2 Input-side trimming — ✅ DONE (verified 2026-07-18, session 3).**
Measured always-loaded instruction files:
- Claude Code `CLAUDE.md` 657 B, Codex `AGENTS.md` 1.4 KB → already tight ✅
- opencode `AGENTS.md` = **19.9 KB** (was 76 KB). Reference sections already extracted into a
  `## Referenz-Sektionen (ausgelagert)` block with 1-line pointers; behavioral rules (§0 session-start,
  §3 hard mandates, ceo-audit) stay inline. The ~10k-token/call win is realised.

**Sync integrity:** `hooks/sync_agents_md.sh` 3-way-syncs AGENTS.md (source repo ↔ Live config ↔ Infra-Stack)
behind an `is_canonical_source()` guard. Live and Infra-Stack copies are now **byte-identical (19,929 B)**.

**🚨 Secret-leak fixed in the same pass:** the Live copy had 4 appended `SIN-BRAIN GLOBAL RULE` lines with
plaintext EvoLink API keys + `OMNIROUTE_MASTER_KEY` + `STORAGE_ENCRYPTION_KEY` — loaded into the model on
EVERY opencode call. Blast radius was local only: the Live file is **not git-tracked / never pushed**, and
Infra had **0 occurrences** in tree or history. Removed; Live == Infra clean. Those keys should still be
rotated as a precaution (they sat in the model context) and kept in Infisical, never in an instruction file.

---

## 6. New levers (2026-07-17, session 3) — web-researched, adopted

Four additions after a fresh 2026 landscape sweep (prompt-caching mechanics, `ccusage`,
subagent routing, TOON). All are free or near-free and cannot make agents dumber — they
either protect a cache that already exists or measure what we already spend.

### 6.1 Cache-protection rules (L2 governance) — **adopted, gated**

Prompt caching is already active (§5, #3 confirmed) and is ~85–90% of the input bill.
Caching is a **prefix cache** over `tools → system → instruction files → history`: **any**
change to an earlier block invalidates the cache for the rest of that request. Two rules
follow, and `verify-tokens` now surfaces the risk:

- **Never add/remove an MCP server mid-session.** Adding a tool changes the tool-definition
  prefix → cache miss on *every* subsequent turn, not just the next one. Plan MCP changes at
  session boundaries. (This is *why* L2 thins the always-on set once, up front — not lazily.)
- **Treat CLAUDE.md / AGENTS.md as a cache anchor.** Every edit = one guaranteed cache write
  (25% surcharge) + cold prefix until it re-warms. Edit them **rarely and in batches**, never
  mid-task. A 5 KB instruction file re-sent uncached costs the full input price on the turn it
  changes. This is the mechanical reason the §5 "keep instruction files tight" rule matters.

Source: Anthropic prompt-caching docs + the April-2026 postmortem (a clearing bug that
re-fired every turn caused a cache miss every turn — the exact failure mode these rules prevent).

### 6.2 `ccusage` — real dollar baseline (L0 measurement) — **adopted**

`rtk gain` measures **only** the L1 shell layer (73.4%). It cannot see the *total* effect of
the standard (caching, model routing, output style). `ccusage` parses `~/.claude/projects/*.jsonl`
and reports actual **$ per day / session / model** — the ground-truth number that answers
"how much does the whole standard save?" (the honest gap #6 above).

```bash
npx ccusage@latest daily      # $ per day, model breakdown
npx ccusage@latest session    # per-session spend
npx ccusage@latest blocks     # 5-hour billing blocks (subscription users)
```

`verify-tokens` now prints a one-line `ccusage` hint under an `[L0] baseline` section (advisory,
never fails the gate — it is a measurement, not a compliance rule).

### 6.3 Subagent-model gate (L0 dollar lever) — **adopted, gated**

`CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5` is set (Sonnet ≈ 40% cheaper than Opus on
research/exploration with negligible quality loss). But nothing verified it wasn't silently
reverted to Opus. `verify-tokens` now **fails** if the key is unset or contains `opus`, and
**passes** on any sonnet/haiku value. Rationale for not forcing Haiku: the same var drives the
planning agent — a weak planner compounds errors downstream (violates "not dumber"). Sonnet is
the safe floor.

### 6.4 TOON + anti-patterns — **documented, not auto-enabled**

**TOON (Token-Oriented Object Notation)** — ~40% average / up to 60% on *large uniform tabular*
data (e.g. `graphify query` JSON, test matrices). **Conditional**: 2026 research shows the win
shrinks or reverses on small/nested data and on *generation* tasks (JSON's training ubiquity
wins there). Verdict: worth a future **optional rtk filter for tabular tool output only**, never
a global default. Backlog, measure per-shape before enabling.

**Context-editing / `clear_thinking` tool-result pruning — REJECTED (belongs in "do NOT" table).**
Server-side pruning of stale tool_results/thinking blocks *sounds* like a win but any prune of an
earlier block invalidates the prefix cache → cache miss. Anthropic's own postmortem shows a
mis-fire turned it into a cache miss **every turn**. For our workloads caching outweighs the
prune. Do not enable. (Same reasoning that rejected aggressive tool-search deferral.)

**Code-execution / Code-Mode (MCP-as-code, 98–99.9% claims) — WATCH, not now.** Real reduction,
but requires a sandboxed V8/exec environment per §"important caveat". Revisit only if we run a
large internal API surface behind MCP; today our L2 thinning + CLI-over-MCP already avoids the
bloat it targets.

---

## 7. Input-side surface (2026-07-17, session 4) — web+YouTube+Reddit sweep

The always-loaded surface — everything paid at session **start, every session, before you type**:
skill descriptions + CLAUDE.md/AGENTS.md + system prompt. This is the highest-leverage lever after
the plumbing, because it is a *constant* tax on every turn. Five additions, all free, none make the
agent dumber. `verify-tokens` now surfaces the surface under an `[L4-input]` section.

### 7.1 `paths:`-scoped rules — the 41% always-loaded cut (Hebel #1) — **governance-gated**

The single biggest input-side finding. A rule/skill with a `paths:` glob in its frontmatter loads
**only when the agent touches a matching file** (conditional load) — zero tokens until triggered.
Documented real result: 1,358 → 807 always-loaded lines (**−41%**). Two hard rules:

- **Splitting a file alone saves nothing.** If you move content out of CLAUDE.md into `rules/` but
  omit `paths:`, all of it still loads at start — you just have more files. The *only* thing that
  saves tokens is the `paths:` filter.
- **CLAUDE.md = what every session needs (build/test/arch). Rules = everything path-specific.**
  Workflow in CLAUDE.md = burned every session even when irrelevant. Rule buried in a skill = missed
  on the 80% of tasks the skill never fires. Get the split wrong and you pay twice.

State of our fleet: Claude `CLAUDE.md` is already tiny (1.7 KB) and Codex `AGENTS.md` lean (3 KB).
opencode `AGENTS.md` was trimmed 76 KB → 20 KB on 2026-07-17 (backup `.bak-pre-trim-*`) — but by
*deletion*, and opencode uses a single file, not a `rules/` dir (the `paths:` mechanism is Claude
Code–specific). The remaining work is **governance, not a one-off edit**: `verify-tokens` now warns
when any instruction file exceeds ~30 KB so it cannot silently re-bloat. New Claude-Code rules with
path-specific scope belong in `~/.claude/rules/*.md` with a `paths:` glob, never inlined into CLAUDE.md.

### 7.2 Skill-sprawl budget (Hebel #2) — **gated**

Every installed skill's name+description is preloaded into the system prompt at session start —
~**100 tokens per skill**, always, whether or not it fires. One reported workspace hit 6,000 tokens
of skill descriptions across ~80 skills before a single keystroke. The description budget is **1% of
context / 8,000 chars** for all skills combined. `verify-tokens [L4-input]` now counts skills and
sums description bytes, warning past 30 skills or 8 KB. (Currently 9 skills / 1.5 KB — healthy.)

### 7.3 `disable-model-invocation: true` (Hebel #3) — **selective, not blanket**

A skill marked `disable-model-invocation: true` costs **0 description tokens** at start (it is only
reachable via explicit `/skill-name`). Use it for **pure slash-command skills**. **Do NOT apply it
blanket** — skills built to trigger *passively* on intent (e.g. `skill-multimodal-web-tools`, which
should fire on "research"/"look up") would go silent. Rule: opt in per-skill, only when the skill is
never meant to auto-trigger. Blindly flipping it on shared skills breaks discovery.

### 7.4 Slash-command-first (Hebel #4) — **documented habit**

Passive skill triggering from descriptions alone fires only **30–50%** of the time — a coin flip. So
when you know which skill you need, **call `/skill-name` directly**; treat passive triggering as a
bonus, not the primary path. This is *why* 7.3 is safe for genuine slash-only skills and why good
descriptions still matter for the rest.

### 7.5 `.claudeignore` (Hebel #5) — **adopted**

A `~/.claude/.claudeignore` blocks generated/vendored/binary noise (`node_modules/`, `*.log`,
`__pycache__/`, `.planning/graphs/`, `.rtk/`, lockfiles, media) from ever entering context — one
stray `node_modules` read is thousands of wasted tokens. `verify-tokens` warns if it's missing.
Query large code-graphs via the `graphify` CLI instead of reading the raw JSON.


---

## Session 5 (2026-07-18) — Delegation-Doktrin, Memory-Fix, kuratierte Skills

**Orca-Delegations-Doktrin (Kernhebel).** Hauptagenten sind Orchestratoren, nicht
Ausführer. Alles Verbose — Code-Recherche, Web-Suche, Tests, Log-/Info-Sammeln,
Multi-File-Exploration, Audits — wird via `orca` CLI an mimo-code-Subagenten
delegiert (Skill `orca-sin-team`). Nur destillierte Reports kommen zurück; der teure
Rohausgabe-Müll (40k+ Tokens grep/log/web) bleibt im Sub isoliert. Verankert in
`~/.config/opencode/AGENTS.md` UND `~/.codex/AGENTS.md`. Modell nicht dümmer — es
bekommt Reports statt Rohdaten.

**claude-mem-Memory repariert.** `observations=0` trotz 187 erfasster Prompts: Root
Cause war ein korrupter uv-Cache (`~/.cache/uv`), der `chroma-mcp`s Prewarm mit
"WHEEL: No such file" killte → jede Observation verworfen. Fix: `uv cache clean` +
`UV_HTTP_TIMEOUT=300`. chroma-mcp läuft wieder CLEAN.

**Brain → claude-mem Writer (one-way, pull-basiert).**
`global-brain/scripts/brain-to-claude-mem.py` spiegelt durable rule/decision-Einträge
aus `knowledge.json` in claude-mems `observations` (Dedup über UNIQUE INDEX
`content_hash`). In `pcpm-after-run.sh` eingeklinkt. Durchsuchbar via `mcp-search`,
kein SessionStart-Bloat. Zwei Brains (Node/JSON global-brain vs Python/SQLite
SIN-Brain) NICHT mergen — claude-mem ist die Query-Surface, die Brains speisen ein.

**session-warmup/preflight/merge-safety als Gates.** `sin session-warmup` als
Schritt 1 vor understand-anything (deterministischer Kontext-Load statt Selbst-Grep).
`sin preflight` vor jedem Code-Task (kein Blind-Coding). `sin merge-safety` vor Merge.

**graphify vs understand-anything — komplementär, kein Entweder-oder.**
understand-anything = Session-Start (Breite). graphify = punktuelle Code-Fragen
während der Arbeit STATT grep. Nicht "graphify statt understand-anything".

**Kuratierte OpenSIN-Skills.** 4 token-/coding-relevante Skills (llm-cost-optimizer,
prompt-governance, mcp-server-builder, self-improving-agent) aus OpenSIN-Skills als
Symlinks in `~/.config/opencode/skills/` (paths, nicht baseline → nur Frontmatter
geladen, Body bei Aktivierung).

**Hinweis/Risiko:** `/Users/jeremy/.git` ist ein Repo, das das gesamte $HOME trackt
(Remote SINator-EvoLink) — potenzielles Secret-Leak-Risiko, separat prüfen.
