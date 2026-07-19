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
Launch: `export LLM_API_KEY="${LLM_API_KEY:-$OPENAI_API_KEY}" COGNEE_PLUGIN_DATASET=sin-fleet && claude`

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
