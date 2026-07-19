# PLAN: Cognee as L3 memory leap (pilot → promote/abort)

**Status:** proposal  
**Date:** 2026-07-19  
**Evidence base:** [cognee-demo.gif](https://github.com/topoteretes/cognee/blob/main/assets/cognee-demo.gif) frame-by-frame + README/MCP/Claude plugin docs  
**Related:** L3 in `docs/BEST-PRACTICES.md`, wow `shared/mcp/servers.json`, simone + claude-mem

---

## What the demo actually shows (not marketing fluff)

Side-by-side Claude Code, same hard domain question (Si/SiGe quantum-dot voltages + charge noise):

| | Vanilla Claude | Claude + cognee |
|--|----------------|-----------------|
| Dataset badge | `cognee: disabled` | `cognee: physics_papers` |
| Prompt #1 | Long generic essay from training weights | Structured mechanism answer in **real time** |
| Follow-ups | Prompt #2, #3, web search, paper fetch | **Done** after prompt #1 |
| Demo claim card | Slow · less accurate · 3 follow-ups · **7× more tokens** | **7× faster · 79% more accurate · 0 follow-ups · 7× fewer tokens** |

Mechanics under the hood (from product + MCP):

1. **Ingest** domain material → cognify into a **knowledge graph** (entities + relations, not just chunks).
2. **Session memory** + permanent graph (`remember` with/without `session_id`).
3. **Recall** auto-routes search strategy; injects only relevant subgraph context.
4. Claude plugin hooks: `SessionStart` / `UserPromptSubmit` inject / `PostToolUse` capture / `PreCompact` preserve / `SessionEnd` sync.
5. MCP surface is intentionally tiny: **`remember` · `recall` · `forget`** → low L2 schema tax.

That last point is why this is *not* just another heavy MCP: it aligns with SST’s “CLI + thin tools > fat always-on manifests.”

---

## Fit against our current L3 stack

| Layer we have | Role | Gap vs cognee |
|---------------|------|----------------|
| **claude-mem** | Session observation DB, cross-runtime | Good at “what happened in chat”; weak at **domain graph reasoning** |
| **Simone-MCP** | Code intel + Qdrant+Neo4j (often down) | Code-centric; handshake flaky; not general remember/recall |
| **session-digest / dream** | Resume brief + durable lessons | Distillation, not interactive graph RAG |
| **memory-scope** | BM25 over small index | Honest ROI gate says load-all is fine *today* |
| **graphify** | Code structure graph (0 LLM) | Repo symbols, not semantic agent memory |

**Conclusion:** Cognee is not a drop-in replace for claude-mem. It fills the **domain/company-brain + cross-session graph memory** hole that causes follow-up thrash (the #1 silent token killer in the GIF).

---

## Recommendation (opinionated)

### Do this (phased)

**Phase 0 — Measure baseline (1 evening)**  
Pick 10 real multi-turn questions from our work (OpenSIN, Infra, token stack). Run without cognee; record turns, tokens (`ccusage` / transcript), accuracy.

**Phase 1 — Local pilot (3–5 days), Claude only**  
- Install [cognee Claude plugin](https://github.com/topoteretes/cognee-integrations/tree/main/integrations/claude-code) **or** `cognee-mcp` via Docker HTTP.  
- Dataset: e.g. `sin-fleet` — ingest SST + wow READMEs, BEST-PRACTICES, token-discipline-clis, a few key designs.  
- Keep **claude-mem on** (no cutover).  
- Budget: cognee is **optional L3**, not always-on for every agent until measured.

**Phase 2 — wow registry (gated)**  
Add `cognee` to `shared/mcp/servers.json` with:

```json
"agents": ["claude"],          // expand after pilot
"always_on": false,            // new budget field — see L2 budget issue
"transport": "remote",
"url": "http://127.0.0.1:8001/mcp"
```

Doctor checks process/URL health. Install does **not** auto-start Docker until pilot succeeds.

**Phase 3 — Cross-agent (only if Phase 1 wins)**  
opencode/mimo/codex get MCP remote; orchestrators use recall via orca-delegates when useful. Session dataset shared (`agent_sessions` pattern from cognee plugins).

**Phase 4 — Role map freeze**  
Document permanent division of labor:

```
graphify     → code structure (0 LLM)
claude-mem   → observation stream / “what we did”
cognee       → domain knowledge graph + remember/recall
session-digest/dream → resume + promote durable rules into claude-mem (or cognee)
simone       → code AST/LSP (fix or demote if cognee covers)
```

### Do **not** do this

- Do not flip all 10 MCP servers + cognee always-on (L2 explosion).  
- Do not replace claude-mem on day 1.  
- Do not require OpenAI cloud if we can route LLM via existing OmniRoute/SpaceXAI — pilot must use our keys.  
- Do not treat vendor “79% more accurate” as our truth — re-measure on our corpus.

---

## Token-economics hypothesis (testable)

If cognee removes **one** follow-up turn of ~10–30k tokens on hard domain tasks, it pays for graph build cost within a few sessions.  
MCP with **3 tools** adds ~few hundred tokens of schema vs thousands for fat tool suites — acceptable if recall hits are high quality.

Abort criteria:

- Graph build needs constant LLM $ without hit-rate  
- Context injection bloats always-loaded surface > skill budget  
- Conflicts with `/compact` or claude-mem hooks  
- Reliability worse than “simone down” status quo

---

## Implementation checklist

- [ ] Baseline 10-question harness script under `bin/` or `scripts/bench-memory/`  
- [ ] Local cognee (Docker compose profile mcp **or** plugin local mode)  
- [ ] Ingest SST+wow canon into dataset `sin-fleet`  
- [ ] Side-by-side transcript capture  
- [ ] Decision record: promote / demote / hybrid  
- [ ] If promote: wow registry entry + doctor health + SST L3 verify section  
- [ ] Update BEST-PRACTICES § L3 with role map  

---

## References

- Demo GIF: https://github.com/topoteretes/cognee/blob/main/assets/cognee-demo.gif  
- MCP: https://github.com/topoteretes/cognee/tree/main/cognee-mcp (`remember`/`recall`/`forget`)  
- Claude plugin hooks: https://github.com/topoteretes/cognee-integrations/tree/main/integrations/claude-code  
- Paper: https://arxiv.org/abs/2505.24478  
- BEAM benchmarks (directional): 0.79 @ 100K tokens in their report  
