# Cognee pilot bootstrap (Claude plugin)

**Decision:** Claude **plugin** over always-on MCP for the pilot.

| | Plugin | MCP (`remember/recall/forget`) |
|--|--------|--------------------------------|
| Context inject | Automatic on every prompt | Agent must call tools |
| Matches demo GIF | Yes | Partial |
| L2 schema tax | **Zero** extra always-on tools | 3 tools (small but not free) |
| Multi-agent | Claude-first | wow registry later (#6) |
| Fail mode | SessionStart skip | Broken MCP handshake spam |

## Installed

```bash
claude plugin marketplace add topoteretes/cognee-integrations
claude plugin install cognee-memory@cognee --scope user
```

Config: `~/.cognee-plugin/claude-code/config.json` → dataset `sin-fleet`

### OmniRoute LLM + NIM embeddings (recommended)

See **[cognee-omniroute-results.md](./cognee-omniroute-results.md)** for measured results.

```bash
# 1) OmniRoute up + Boundless terra models
bin/omniroute-ensure-boundless-terra.sh

# 2) Cognee with Terra (LLM) + nvidia/nv-embedqa-e5-v5 (embed)
source bin/cognee-omniroute-env.sh
bin/cognee-start-omniroute.sh

# 3) Claude plugin session (same shell env)
export COGNEE_PLUGIN_DATASET=sin-fleet
claude   # expect: Cognee Memory Connected
```

| Role | Model via OmniRoute |
|------|---------------------|
| Cognify / recall LLM | `boundless/gpt-5.6-terra` → BoundlessAPI |
| Embeddings | `nvidia/nv-embedqa-e5-v5` → NVIDIA NIM (1024-d) |

Legacy (no OmniRoute): `export LLM_API_KEY=… COGNEE_PLUGIN_DATASET=sin-fleet && claude`

## First session checklist

1. Confirm system message: **Cognee Memory Connected**
2. Ingest fleet canon (ask Claude or CLI):
   - `SIN-Save-Token/README.md`
   - `SIN-Save-Token/docs/BEST-PRACTICES.md`
   - `SIN-Save-Token/docs/ECOSYSTEM.md`
   - `wow-my-zsh/README.md`
   - `wow-my-zsh/shared/AGENTS.md`
   - `wow-my-zsh/docs/token-discipline-clis.md`
3. Ask a multi-hop question that previously needed follow-ups
4. Log turns/tokens vs baseline in issue #5

## Abort / keep claude-mem

claude-mem stays enabled. Abort cognee if inject bloat, build $ spikes, or reliability worse than baseline.

## Promote path

If pilot wins → wow issue #6 (remote MCP, `always_on: false`) + SST L3 role map (#9).
