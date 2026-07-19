# SIN-Save-Token × wow-my-zsh — Audit Roadmap (2026-07-19)

Companion: [ECOSYSTEM.md](../docs/ECOSYSTEM.md) · Cognee proposal: [PLAN-cognee-L3.md](./PLAN-cognee-L3.md)

## North star

**Save as many tokens as possible without making agents dumber.**  
Enforcement by construction (hooks + gates), not louder rules.

## Phases

### Phase A — Honest gates (P0, ~1–2 days)
Close the measurement lies so green means green.

| # | Work | Repo | Issue |
|---|------|------|-------|
| A1 | `verify-tokens` L2 reads real MCP sources (`~/.claude.json`, opencode, mimo, codex) | SST | L2 real MCP source |
| A2 | Shared L2 MCP budget (allowlist / always_on / soft max) | both | Joint L2 budget |
| A3 | Align `rtk gain` hook detection with SST rewrite hook (or document dual path) | SST | rtk dual signal |

### Phase B — MCP hygiene (P1, ~2–3 days)
| # | Work | Repo |
|---|------|------|
| B1 | Portable registry paths (no `/Users/jeremy/...`) + `machines/` overrides | wow |
| B2 | `doctor.sh` MCP *runtime* health (HTTP ping / short stdio probe) | wow |
| B3 | Broken servers (simone, sin-*, youtube) triage: fix or drop from always-on | wow |

### Phase C — Memory leap (P1, pilot 1 week)
| # | Work | Repo |
|---|------|------|
| C1 | **Cognee pilot** — graph memory as L3 option (see PLAN-cognee-L3.md) | both |
| C2 | Role map: claude-mem vs simone vs cognee vs session-digest/dream | SST |
| C3 | ROI gate: measure tokens/follow-ups before promoting always-on | SST |

### Phase D — Hardening & fleet (P2)
| # | Work | Repo |
|---|------|------|
| D1 | Wire or delete dead `hooks/lib/git-cmd.js` | SST |
| D2 | ECOSYSTEM contract doc + install order | both |
| D3 | Fleet `verify-tokens` on OCI / second Mac | SST |
| D4 | Behavioral habits (optional nudge hooks for /clear, compact) | SST |
| D5 | Fill wow empty scaffolds only if needed (skills/hooks/machines) | wow |

## Success metrics

- `verify-tokens` L2 count matches live agent configs (±0)
- Always-on MCP schema tax measured (target: ≤4 always-on on Claude unless budget raised)
- Cognee pilot: ≥3× fewer follow-up prompts on a fixed domain Q set, or abort
- `doctor.sh` + `verify-tokens` both exit 0 on primary Mac after Phase A/B

## Explicit non-goals

- Replace claude-mem without a measured pilot
- Aggressive tool-search deferral (breaks `/compact`)
- Headroom / blind ML compression (already rejected in BEST-PRACTICES)

## GitHub issues (filed 2026-07-19)

Epic: https://github.com/OpenSIN-Code/SIN-Save-Token/issues/1

| Issue | Title |
|-------|-------|
| #2 | P0 verify-tokens real MCP source |
| #3 | P0 joint L2 budget |
| #4 | P1 rtk dual signal |
| #5 | P1 Cognee pilot |
| #6 | P2 git-cmd.js |
| #7 | P2 ECOSYSTEM.md |
| #8 | P2 fleet verify |
| #9 | P1 L3 role map |

wow epic: https://github.com/OpenSIN-Code/wow-my-zsh/issues/2
