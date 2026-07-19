# Cognee fleet — cost & reliability (correct setup)

## Architecture (do this)

```
Any agent / Orca
  → cognee-recall / cognee-remember  (CLI, all harnesses)
  → Cognee API :8011
       ├─ LLM:   OmniRoute → boundless/gpt-5.6-terra   (Boundless, paid)
       └─ Embed: local fastembed mixedbread-ai/mxbai-embed-large-v1 (default, 1024)
                 optional: COGNEE_EMBED_BACKEND=nim → nvidia/nv-embedqa-e5-v5
```

## Local embed quality on Mac M1 16GB (measured)

All models run natively via `fastembed` + ONNX (`arm64`, providers include CoreML/CPU).
First load downloads weights once; later starts use cache (seconds, not minutes).

| Model | Dim | short×16 (this M1) | Role |
|-------|-----|--------------------|------|
| **mixedbread-ai/mxbai-embed-large-v1** | 1024 | ~2.5s | **default — best local quality** |
| BAAI/bge-large-en-v1.5 | 1024 | ~0.9s | near-top quality, faster |
| nomic-ai/nomic-embed-text-v1.5 | 768 | ~0.3s | long context / docs |
| thenlper/gte-large | 1024 | ~6s | strong, slower batch |
| intfloat/multilingual-e5-large | 1024 | ~0.9s | multi-lang |
| jinaai/jina-embeddings-v3 | 1024 | ~8–14s | strong, **too slow** on loaded M1 16GB |
| BAAI/bge-small-en-v1.5 | 384 | ~0.07s | rejected — too weak for “best” |

**Not** the absolute world #1 (Voyage/Cohere API can still edge MTEB).  
**Yes** the best *reliable free local* option that this M1 runs cleanly under multi-agent load.

RAM note: M1 16GB under heavy swap makes large models feel slower. Free RAM → large embeds stay easy.

## Is NVIDIA the right embed?

| Backend | Pros | Cons | Verdict |
|---------|------|------|---------|
| **fastembed mxbai-large (default)** | free, local, top ONNX quality, long chunks | first download ~0.6GB; slower than small | **default** |
| **NIM nv-embedqa-e5-v5** | strong cloud retrieval | **hard ~512-token limit**; free-tier timeouts | optional short texts only |
| Boundless embed | — | **none available** | n/a |
| Voyage/Cohere/Gemini API | often paper-SOTA | paid + network | later A/B if needed |

### Real failures we hit (not “maybe”)

1. **`Input length N exceeds maximum allowed token size 512`** — NIM E5 via OmniRoute rejects long chunks.
2. **Timeouts / 409 under bulk load** — free NIM path flakes under parallel cognify.
3. **Dim mismatch** — Lance is `fixed_size_list[N]`. Switching models → `bin/cognee-reindex-vectors.sh`.

## Cost (Boundless only on LLM)

| Action | Boundless? | Gate |
|--------|------------|------|
| `cognee-fleet-up.sh` | no | — |
| local embed smoke | no | — |
| `cognee-recall` | **maybe** (graph_completion may call Terra) | use when needed |
| `cognee-remember "short note"` | **yes** (cognify) | soft warning; size cap 50k |
| Large file / bulk re-ingest | **yes, expensive** | `COGNEE_ALLOW_COSTLY=1` + max docs/chars |

## Everyday (all agents)

```bash
~/dev/SIN-Save-Token/bin/cognee-fleet-up.sh   # free stack bring-up

cognee-status
cognee-recall "L2 core MCP servers?"
cognee-remember "Decision: default embed is mxbai-embed-large local."
```

Faster local alternative (still strong):

```bash
export EMBEDDING_MODEL=BAAI/bge-large-en-v1.5
export EMBEDDING_DIMENSIONS=1024
bin/cognee-reindex-vectors.sh
```

Optional NIM (short texts only; **must reindex**):

```bash
export COGNEE_EMBED_BACKEND=nim
bin/cognee-reindex-vectors.sh
cognee-remember "short durable fact"
```
