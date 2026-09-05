# OmniRoute Provider Guide

Updated: 2026-08-25

OmniRoute is the **stable OpenAI-compatible inference gateway** for the SIN fleet. It is not a memory store. OpenViking owns durable semantic memory; OmniRoute only abstracts model/provider choice so OpenViking and agents do not depend on one concrete inference backend.

## Current architecture

Fleet deployment/inference topology is canonical in `wow-my-zsh/docs/MEMORY-PLATFORM.md` and `wow-my-zsh/docs/INFERENCE-PLATFORM.md`.

The intended production split is:

| Concern | Canonical path | Notes |
|---|---|---|
| Durable semantic memory | `sin-memory-write` → SIN Memory Gateway → OpenViking | OpenViking is the only semantic-memory truth |
| Durable recall | `sin-context` → OpenViking | pull-based, bounded context |
| Code intelligence | `sin-context` → GitNexus | repo-/checkout-local, dirty-tree aware |
| Embeddings | OpenViking → dedicated embedding endpoint | independent from text LLM routing |
| VLM / planner inference | OpenViking → OmniRoute `:20128` | provider-neutral routing |
| Large text LLM | Agent/OpenViking → OmniRoute → backend | FreeToken can become the preferred Linux/NVIDIA worker |

## Embedding policy

Embedding availability is a correctness dependency for semantic recall, so it must not rely on a route that only works for a few free-tier requests. During the 2026-08-24/25 rollout, Vercel AI Gateway embedding aliases returned valid vectors in light probes but rate-limited realistic batches. The production direction is therefore a dedicated, private embedding endpoint on OCI using **Qwen3-Embedding-0.6B** through llama.cpp, with a fixed 1024-dimensional contract.

Promotion requires:

1. multiple realistic batches without rate limiting;
2. stable vector dimension;
3. an OpenViking reindex that creates non-zero vector records;
4. semantic recall of a committed canary.

A provider appearing in `/v1/models` is never enough evidence by itself.

## Text/VLM provider switching

Do not change a production alias merely because a model is newer. A candidate route must pass:

1. live catalog/route availability;
2. a non-streaming completion with HTTP 200 and usable content;
3. the specific OpenViking/agent workload it is intended to serve;
4. latency/error-rate checks under realistic context size;
5. fallback behavior through OmniRoute rather than direct client rewiring.

Reasoning-only routes are not promoted unless the consuming adapter has been verified against their response shape.

## FreeToken role

FreeToken is the preferred **future local GPU text-inference engine** when a suitable Linux x86_64/NVIDIA worker is available. It sits behind OmniRoute. It does **not** replace OpenViking, GitNexus, the Memory Gateway or the embedding service.

This separation lets the fleet move between FreeToken and other text providers without changing memory URLs, agent contracts or durable data.

## Security

- `OMNIROUTE_MASTER_KEY` and provider credentials are runtime secrets only.
- Never copy keys into docs, prompts, task evidence or repository files.
- OCI-internal embedding/OpenViking backends bind to loopback/private interfaces.
- Fleet access is through the private Tailscale path; no public brain endpoint is required.

## Legacy Cognee note

Older scripts and documents may still describe Cognee-specific OmniRoute launch helpers. They are retained only for migration/forensic compatibility. Cognee is no longer the canonical durable memory and must not determine the fleet-wide inference architecture.
