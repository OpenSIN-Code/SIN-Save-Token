# Cognee fleet — cost & reliability (correct setup)

## Architecture (do this)

```
Any agent / Orca
  → cognee-recall / cognee-remember  (CLI, all harnesses)
  → Cognee API :8011
       ├─ LLM:   OmniRoute :20128 → vag/zai/glm-5.2  (Vercel AI Gateway)
       └─ Embed: nim-embed-proxy :8012 → NVIDIA NIM nemotron-3-embed-1b @ 1024-dim (free ~40 RPM)
```

## Secrets

```bash
# NEVER commit. NEVER paste keys into chat/commits.
# NVIDIA_API_KEY: free from build.nvidia.com (env var, no file needed)
# OMNIROUTE_MASTER_KEY: in ~/.omniroute/.env (chmod 600)
# Vercel credit card: required for GLM 5.2 via Vercel AI Gateway
```

## Embed backends

| Backend | How | Free? | Notes |
|---------|-----|-------|-------|
| **nim (default)** | nim-embed-proxy :8012 → NVIDIA NIM | yes ~40 RPM | nemotron-3-embed-1b, 2048 dims, #1 RTEB |
| `COGNEE_EMBED_BACKEND=gemini` | proxy :8012 → Gemini API | free tier + limits | 1024 dims (legacy) |
| `COGNEE_EMBED_BACKEND=fastembed` | pure local | yes | mxbai-large, 1024 dims |

Bring-up:

```bash
bin/cognee-fleet-up.sh
# or manually:
python3 bin/nim-embed-proxy.py &
bin/cognee-start-omniroute.sh
```

## Cost (OmniRoute subscription on LLM)

| Action | Costs? | Gate |
|--------|--------|------|
| fleet-up / nim-embed-proxy | no | — |
| NVIDIA NIM embed | no (free tier ~40 RPM) | — |
| `cognee-remember` | **yes** (GLM 5.2 cognify) | soft warn |
| bulk re-ingest | **yes expensive** | `COGNEE_ALLOW_COSTLY=1` |

## Everyday

```bash
cognee-status
curl -s http://127.0.0.1:8012/health   # shows nim ok/error stats
cognee-recall "…"
cognee-remember "short durable note"
```


## Local fleet authentication posture

The canonical fleet Cognee runtime is bound to `127.0.0.1:8011` and is a
single-user local service. Newer Cognee releases default backend access control
and authentication to enabled, which breaks the existing local `cognee-recall` /
`cognee-remember` contract when an old static API key is no longer accepted.

`bin/cognee-start-omniroute.sh` therefore makes the intended deployment posture
explicit before starting Uvicorn:

```sh
export ENABLE_BACKEND_ACCESS_CONTROL=false
export REQUIRE_AUTHENTICATION=false
```

This is valid only for the loopback-only single-user runtime. Do not reuse these
settings for a remotely reachable or multi-user Cognee deployment. OmniRoute and
the embedding proxy remain separate local dependencies; durable writes still
fail closed unless the Cognee write completes successfully.
