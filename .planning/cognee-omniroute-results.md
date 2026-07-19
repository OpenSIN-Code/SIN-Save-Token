# Cognee + OmniRoute integration results (2026-07-19)

## Architecture (live)

```
Claude / Cognee API (:8011)
        │
        ▼
 OmniRoute (:20128)  ← OMNIROUTE_MASTER_KEY
   ├─ boundless/gpt-5.6-terra  → BoundlessAPI  (LLM / cognify / recall)
   └─ nvidia/nv-embedqa-e5-v5  → NVIDIA NIM    (embeddings, 1024-d)
```

## OmniRoute setup done

BoundlessAPI node (prefix `boundless`) already existed with active key.
Registered models on that node:

- `gpt-5.6-terra` (primary)
- `gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`

Smoke test:

```bash
curl http://localhost:20128/v1/chat/completions \
  -H "Authorization: Bearer $OMNIROUTE_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"boundless/gpt-5.6-terra","messages":[{"role":"user","content":"Say TERRA_OK"}],"max_tokens":16,"stream":false}'
# → TERRA_OK

curl http://localhost:20128/v1/embeddings \
  -H "Authorization: Bearer $OMNIROUTE_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"nvidia/nv-embedqa-e5-v5","input":"test"}'
# → 1024-dim vectors
```

## Cognee start helpers (this repo)

| Script | Purpose |
|--------|---------|
| `bin/cognee-omniroute-env.sh` | Export LLM + EMBEDDING_* toward OmniRoute |
| `bin/cognee-start-omniroute.sh` | Start Cognee :8011 with that env |
| `bin/cognee-pilot-ingest.sh` | Bulk remember fleet docs |
| `bin/cognee-pilot-status` | Plugin / env checklist |

```bash
source bin/cognee-omniroute-env.sh
bin/cognee-start-omniroute.sh
# mint api key once → ~/.cognee-plugin/api_key.json
export COGNEE_API_KEY=… COGNEE_PLUGIN_DATASET=sin-fleet
```

Env (via `cognee-omniroute-env.sh`):

| Var | Value |
|-----|--------|
| `LLM_MODEL` | `boundless/gpt-5.6-terra` |
| `LLM_ENDPOINT` / `OPENAI_API_BASE` | `http://127.0.0.1:20128/v1` |
| `LLM_API_KEY` / `OPENAI_API_KEY` | OmniRoute master key |
| `EMBEDDING_PROVIDER` | `openai_compatible` |
| `EMBEDDING_MODEL` | `nvidia/nv-embedqa-e5-v5` |
| `EMBEDDING_ENDPOINT` | `http://127.0.0.1:20128/v1` |
| `EMBEDDING_DIMENSIONS` | `1024` |

## Pilot results (dataset `sin-fleet`)

| Step | Result |
|------|--------|
| Cognify small facts doc | **completed** (~84s, 1 item) |
| Recall: "L2 core always_on MCP servers?" | **`context7, serena, and tavily`** |
| Recall: wow vs SST ownership | Correct split (router vs token policy) |
| Recall: default MCP profile | **core** |
| Recall: OmniRoute terra routing | **localhost:20128 → BoundlessAPI** |
| Larger multi-file bulk remember | Some **409 Embedding failed** (NIM free-tier flaky under load) — retry later / background |

## Claude Code plugin

- `cognee-memory@cognee` installed, dataset `sin-fleet`
- Launch with same OmniRoute env in the shell before `claude`, so SessionStart boots local API with correct LLM/embed.

```bash
source ~/dev/SIN-Save-Token/bin/cognee-omniroute-env.sh
export COGNEE_PLUGIN_DATASET=sin-fleet
export LLM_API_KEY="$OMNIROUTE_MASTER_KEY"   # plugin local server
claude
# expect: Cognee Memory Connected
```

## Operational notes

1. **OmniRoute must be up** before Cognee (`omniroute serve` / existing process).
2. **Prefix required** on model ids: `boundless/…`, `nvidia/…`.
3. NIM free embeddings can 5xx under parallel load — prefer sequential ingest or `run_in_background=true` + poll.
4. Do not mix embedding dimensions (1024 NIM) with other embedders without wiping the vector store.
5. Boundless quota is separate from NIM free tier — monitor both.

## Verdict

**Promote path is green for core pilot loop:**  
Terra via OmniRoute/Boundless for cognition + NIM EmbedQA for vectors + recall answers grounded in fleet facts.

Next: stabilize bulk ingest (retry/backoff), optional wow registry `cognee` remote entry (`always_on: false`) after more sessions.
