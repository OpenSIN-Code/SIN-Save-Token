# OmniRoute Provider Guide

Überblick über verfügbare LLM-Provider in OmniRoute für SIN-Save-Token.

## Architektur

```
Cognee + gbrain → OmniRoute :20128 → Provider (GLM 5.2, GPT, Claude etc.)
                → NIM-Proxy :8012 → Embeddings (nemotron-3-embed-1b)
```

## Verfügbare Provider

| Provider | Modell | Status | Kosten |
|----------|--------|--------|--------|
| **Vercel AI Gateway** | `vag/zai/glm-5.2` | ✅ Aktuell genutzt | Benötigt Kreditkarte |
| **NVIDIA NIM** | `nvidia/nemotron-3-embed-1b` | ✅ Embeddings | Free tier (~40 RPM) |
| **OpenCode** | `oc/deepseek-v4-flash-free` | ⚠️ Reasoning-Modell | Free (content leer) |
| **AgentRouter** | `agentrouter/glm-5.2` | ❌ 500 errors | Unbekannt |
| **BoundlessAPI** | `boundless/gpt-5.6-*` | ❌ Pleite (f10316cb) | Kein Guthaben |

## Provider wechseln

```bash
# In cognee-omniroute-env.sh ändern:
export LLM_MODEL="openai/vag/zai/glm-5.2"        # GLM 5.2 (aktuell)
export LLM_MODEL="openai/oc/deepseek-v4-flash-free"  # DeepSeek (Reasoning)
export LLM_MODEL="openai/agentrouter/glm-5.2"     # AgentRouter (ungetestet)

# Danach Cognee neustarten:
./bin/cognee-fleet-up.sh
```

## Nächste Schritte (wenn Guthaben da ist)

1. **ChatGPT Plus** — OpenAI-Modell in OmniRoute registrieren
2. **Aerolink** — als Failover-Provider hinzufügen
3. **Model-Routing** — verschiedene Modelle für verschiedene Tasks

## Model-Auswahl für Cognee

Cognee braucht ein **nicht-Reasoning** Modell für:
- `cognify` (Entity/Relation Extraction)
- `recall` (Graph-Completion)

GLM 5.2 funktioniert, ist aber langsam (~20s pro remember).
DeepSeek V4 Flash ist ein Reasoning-Modell — `content` ist leer, nicht geeignet.
