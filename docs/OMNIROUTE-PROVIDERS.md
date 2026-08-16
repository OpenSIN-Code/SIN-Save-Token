# OmniRoute Provider Guide

Überblick über die aktuell relevanten LLM-/Embedding-Pfade für SIN-Save-Token und Cognee.

Updated: 2026-08-16

## Architektur

```text
Cognee / sin-memory-write
  -> OmniRoute :20128
  -> auto/best-free (provider-neutral healthy chat route)

Cognee embeddings
  -> NVIDIA NIM embed proxy :8012 when a valid NIM key is available
  -> otherwise configured Gemini/local embedding fallback
```

The memory system is intentionally **provider-neutral**. It is not character/media production, so a healthy fleet combo is preferable to freezing a provider name whose credentials or quota may disappear. The durable contract is `sin-memory-write` -> Cognee -> successful cognify -> local ledger commit.

## Current live status

The 2026-08-16 verification found:

| Route | Live result | Decision |
| --- | --- | --- |
| `nvidia/z-ai/glm-5.2` | 503: no active NVIDIA provider credentials | Do not use as Cognee default |
| Vercel AI Gateway chat aliases | advertised in `/v1/models`, but tested aliases were credit-card-gated, unavailable, or too slow for Cognify | Do not make Vercel chat the Cognee default until a fresh working completion + cognify probe passes |
| direct Gemini key from current Infisical runtime | rejected by Google as invalid | Do not use as LLM fallback until credential is repaired |
| `aug/claude-haiku-4.5` | HTTP 200 but empty `content` | Not suitable for structured Cognify |
| `ddgw/claude-3-5-haiku-20241022` | rate limited during live probe | Not suitable as default |
| `auto/best-free` | **HTTP 200 with non-empty content**; resolved live to an eligible provider/model | **Current Cognee default** |

A model appearing in `/v1/models` is not sufficient evidence that the account can actually execute it. Promotion requires a real non-empty completion and then a real Cognee cognify/write probe.

## Current configuration

`bin/cognee-omniroute-env.sh` exports:

```bash
export LLM_PROVIDER=openai
export LLM_MODEL="openai/auto/best-free"
export LLM_ENDPOINT="http://127.0.0.1:20128/v1"
```

The leading `openai/` is LiteLLM's OpenAI-compatible transport prefix; OmniRoute receives the fleet model `auto/best-free`.

`OMNIROUTE_MASTER_KEY` is injected at runtime through SIN-Infisical for the LaunchAgent. Do not copy it to docs, prompts, task evidence or repository files.

## Provider switching rule

Do not change the default because a model looks newer. For any candidate replacement:

1. verify it is present in the live OmniRoute catalog;
2. send a tiny non-streaming `/v1/chat/completions` probe and require HTTP 200 **plus non-empty `message.content`**;
3. run a real Cognee `cognify`/`sin-memory-write` probe;
4. confirm the local durable-memory ledger is committed only after Cognee succeeds;
5. update this document with evidence/reason, then restart the Cognee LaunchAgent.

Example override for an explicitly verified route:

```bash
LLM_MODEL="openai/<verified-omniroute-model>" ./bin/cognee-fleet-up.sh
```

Do not use reasoning-only routes that return their useful output outside normal `content` unless Cognee's structured-output adapter has been explicitly verified for that response shape.

## Embeddings

The current embedding contract stays 1024-dimensional so existing vector storage remains compatible. `COGNEE_EMBED_BACKEND` supports the existing NIM/Gemini/local fallback modes defined in `bin/cognee-omniroute-env.sh` and `docs/COGNEE-COST-POLICY.md`.

LLM routing and embedding routing are separate decisions. A broken NVIDIA chat credential does not imply that an independently healthy NVIDIA embedding path must be disabled.

## Relationship to ComfyUI

Do not conflate Cognee's memory LLM with the SIN media runtime. The self-hosted ComfyUI stack in `wow-my-zsh` uses its own pinned image/video aliases and the Vercel AI Gateway pool through the SIN media bridge. Character/media production forbids floating production model aliases and automatic cross-model fallback; Cognee's `auto/best-free` memory route does **not** change that policy.
