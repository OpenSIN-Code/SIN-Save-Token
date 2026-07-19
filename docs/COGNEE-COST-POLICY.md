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
| **fastembed (default)** | free, local, no flake, multi-agent safe, handles long chunks | slightly weaker than E5-v5; 384-dim | **default for everyday** |
| **NIM nv-embedqa-e5-v5** | stronger retrieval (1024-dim) | **hard ~512-token input limit**; free-tier timeouts under bulk | **optional**, short texts only |
| Boundless embed | — | **none available** | n/a |
| EmbedCode | better for pure code index | not free/live on our path | later |

### Real failures we hit (not “maybe”)

1. **`Input length N exceeds maximum allowed token size 512`** — NIM E5 via OmniRoute rejects long chunks. Cognee chunkers often emit >512 tokens → `Embedding failed`.
2. **Timeouts / 409 under bulk load** — free NIM path flakes when many parallel cognify jobs hit embeds.
3. **Dim mismatch 1024↔384** — Lance stores `fixed_size_list[N]`. Switching backend without wipe breaks search. Fix: `bin/cognee-reindex-vectors.sh` then re-`remember` notes.

**Verdict:** NVIDIA is a fine *model*, but **not the right everyday backend** for our fleet (long docs + multi-agent load). Default stays **local fastembed**. Use NIM only when you explicitly want quality on short notes and can reindex.

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

Optional stronger embeds (short texts only; **must reindex**):

```bash
export COGNEE_EMBED_BACKEND=nim
bin/cognee-reindex-vectors.sh   # wipe 384 store + restart as NIM 1024
# then re-seed notes you care about
cognee-remember "short durable fact"
```

Back to default:

```bash
unset COGNEE_EMBED_BACKEND   # or export COGNEE_EMBED_BACKEND=fastembed
bin/cognee-reindex-vectors.sh
```
