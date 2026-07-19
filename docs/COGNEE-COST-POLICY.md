# Cognee fleet — cost & reliability (correct setup)

## Architecture (do this)

```
Any agent / Orca
  → cognee-recall / cognee-remember  (CLI, all harnesses)
  → Cognee API :8011
       ├─ LLM:   OmniRoute → boundless/gpt-5.6-terra   (Boundless, paid)
       └─ Embed: local fastembed BAAI/bge-small-en-v1.5 (default, free, stable)
                 optional: COGNEE_EMBED_BACKEND=nim → nvidia/nv-embedqa-e5-v5
```

## Is NVIDIA the right embed?

| Backend | Pros | Cons | Verdict |
|---------|------|------|---------|
| **fastembed (default)** | free, local, no flake, multi-agent safe | slightly weaker than E5-v5 | **default for everyday** |
| **NIM nv-embedqa-e5-v5** | stronger retrieval quality | free-tier timeouts under load → 409 | **optional** when OmniRoute+NIM stable |
| Boundless embed | — | **none available** | n/a |
| EmbedCode | better for pure code index | not free/live on our NIM path | later |

NIM is **not wrong** — intermittent 409s were **timeouts under bulk load**, not a bad model.  
For **all-day multi-agent** reliability, **local fastembed is the right default**; switch to NIM when you care more about embed quality than zero-ops.

## Cost (Boundless only on LLM)

| Action | Boundless? | Gate |
|--------|------------|------|
| `cognee-fleet-up.sh` | no | — |
| NIM/fastembed smoke | no | — |
| `cognee-recall` | **maybe** (graph_completion may call Terra) | use when needed |
| `cognee-remember "short note"` | **yes** (cognify) | soft warning; size cap 50k |
| Large file / bulk re-ingest | **yes, expensive** | `COGNEE_ALLOW_COSTLY=1` + max docs/chars |

**Agents must work without a special flag** for normal `remember` of short notes.  
**Scripts must not bulk-ingest** without `COGNEE_ALLOW_COSTLY=1`.

## Everyday (all agents)

```bash
~/dev/SIN-Save-Token/bin/cognee-fleet-up.sh   # free stack bring-up

cognee-status
cognee-recall "L2 core MCP servers?"
cognee-remember "Decision: we keep NIM E5 optional; default fastembed."
```

Optional stronger embeds:

```bash
export COGNEE_EMBED_BACKEND=nim
# then restart cognee (new vector dims → wipe/reindex if switching from 384↔1024)
bin/cognee-start-omniroute.sh
```
