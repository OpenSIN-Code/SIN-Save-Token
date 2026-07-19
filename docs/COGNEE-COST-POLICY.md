# Cognee + Boundless cost policy

**Terra (`boundless/gpt-5.6-terra`) is paid Boundless credit.**  
Agents must not burn it on smoke tests or bulk re-ingest.

## Free / cheap (default)

| Action | Cost |
|--------|------|
| OmniRoute model registration | free |
| NIM embed `nvidia/nv-embedqa-e5-v5` | free-tier NIM |
| Cognee `/health`, list datasets | free |
| Stack up: `bin/cognee-fleet-up.sh` | free (no Terra chat) |

## Costs Boundless (opt-in only)

| Action | Gate |
|--------|------|
| `cognee-remember` / cognify | `COGNEE_ALLOW_COSTLY=1` |
| Bulk ingest | `COGNEE_ALLOW_COSTLY=1` + caps `COGNEE_BULK_MAX_DOCS` (default 3), `COGNEE_BULK_MAX_CHARS` (default 4000) |
| Terra chat smoke | `COGNEE_COSTLY_SMOKE=1` |
| Graph-completion `cognee-recall` | may call Terra for the **answer** — use sparingly; prefer when you need fleet memory, not for health checks |

## Everyday multi-agent (Claude / Codex / OpenCode / MiMo / Cline / Orca)

```bash
# once per machine boot
~/dev/SIN-Save-Token/bin/cognee-fleet-up.sh

# all agents — READ path (prefer)
cognee-recall "What is L2 core MCP?"

# WRITE path — only for durable lessons (costs Terra)
COGNEE_ALLOW_COSTLY=1 cognee-remember "Decision: …"
```

Do **not** re-ingest full READMEs in a loop. Dataset already has pilot facts.

## Embeddings

Stay on **NVIDIA NIM `nv-embedqa-e5-v5`**. Boundless has no embed models. EmbedCode later if free/live.
