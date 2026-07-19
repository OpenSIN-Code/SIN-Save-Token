# Cognee fleet — cost & reliability (correct setup)

## Architecture (do this)

```
Any agent / Orca
  → cognee-recall / cognee-remember  (CLI, all harnesses)
  → Cognee API :8011
       ├─ LLM:   OmniRoute → boundless/gpt-5.6-terra   (Boundless, paid)
       └─ Embed: local proxy :8012
              ├─ primary: Gemini gemini-embedding-001 @ 1024-dim (free tier)
              └─ fallback: local mxbai-embed-large-v1 @ 1024-dim (on 429/errors)
```

**Same dim (1024) on both paths** so Lance stays valid when Gemini rate-limits.

## Secrets

```bash
# NEVER commit. NEVER paste keys into chat/commits.
umask 077
mkdir -p ~/.cognee-plugin/secrets
# paste key once, Ctrl-D:
cat > ~/.cognee-plugin/secrets/gemini_api_key
chmod 600 ~/.cognee-plugin/secrets/gemini_api_key
```

If a key was pasted in chat: **rotate it in Google AI Studio** and replace the file.

## Embed backends

| Backend | How | Free? | Notes |
|---------|-----|-------|-------|
| **gemini (default)** | proxy :8012 → Gemini API | free tier + limits | best cloud quality |
| local fallback | auto inside proxy | yes unlimited | mxbai-large, same 1024 dims |
| `COGNEE_EMBED_BACKEND=fastembed` | pure local | yes | skip Gemini |
| `COGNEE_EMBED_BACKEND=nim` | OmniRoute NVIDIA E5 | free tier | hard ~512 token cap |

Bring-up:

```bash
bin/cognee-fleet-up.sh
# or:
bin/cognee-start-embed-proxy.sh
bin/cognee-start-omniroute.sh
```

Force local only for a session:

```bash
export COGNEE_EMBED_FORCE_LOCAL=1   # proxy
# or
export COGNEE_EMBED_BACKEND=fastembed
bin/cognee-start-omniroute.sh
```

## Cost (Boundless only on LLM)

| Action | Boundless? | Gate |
|--------|------------|------|
| fleet-up / embed proxy | no | — |
| Gemini embed | no $ (free tier) | rate limits → local |
| `cognee-remember` | **yes** (Terra cognify) | soft warn; 50k cap |
| bulk re-ingest | **yes expensive** | `COGNEE_ALLOW_COSTLY=1` |

## Everyday

```bash
cognee-status
curl -s http://127.0.0.1:8012/health   # shows gemini_ok / fallback_ok stats
cognee-recall "…"
cognee-remember "short durable note"
```
