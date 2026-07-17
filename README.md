# SIN-Save-Token

**Der 4-Layer Token-Sparstandard für die gesamte SIN-Agenten-Flotte.**
Ein Setup, das jeder Agent (Claude Code, opencode, Codex, Orca) **automatisch selbst nutzt** —
niemand muss je einen Agenten daran erinnern.

> Ziel: **so viele Tokens wie möglich sparen, ohne dass Agenten dümmer werden.**
> Jede Entscheidung hier ist durch abgerechnete Benchmarks belegt (siehe [Evidenz](#evidenz)).

---

## TL;DR — Installation auf einem neuen Mac / in einem neuen System

```bash
git clone https://github.com/OpenSIN-Code/SIN-Save-Token.git
cd SIN-Save-Token
# rtk muss vorhanden sein (RTK - Rust Token Killer):
#   cargo install rtk        # oder brew install rtk
./bin/install.sh             # richtet alle Runtimes ein, idempotent
```

Danach einmalig den Self-Heal-Hook in `~/.claude/settings.json` registrieren (siehe [Automatik](#automatik-kein-agent-muss-erinnert-werden)).
Ab dann ist es **selbsterhaltend**: jede neue Session repariert fehlende Hooks still.

Prüfen:
```bash
./bin/install.sh --check     # Compliance-Report, exit 1 bei Regression
```

---

## Die 4 Layer

| Layer | Was | Mechanismus | Ersparnis |
|---|---|---|---|
| **L1 Shell** | `rtk` komprimiert Shell-Output transparent | Claude Code PreToolUse-Hook, opencode-Plugin, Codex RTK.md | ~80% auf git/test/build/package-Output |
| **L2 Tools** | Schlanke MCP-Oberfläche | Nur gebrauchte MCP-Server; **kein** aggressives Tool-Search-Deferral | vermeidet 10–60k Tokens/Turn Schema-Bloat |
| **L3 Memory** | Geteiltes Gedächtnis | 1 claude-mem Worker + 1 DB für alle Runtimes | kein Doppel-Spend |
| **L4 Output** | Knappe Antworten | terse-Kontrakt in jeder Instruktionsdatei | Output-Tokens sind die teuersten |

**Zusätzliche Hebel (in `docs/BEST-PRACTICES.md`):**
- **Modell-Routing** — Subagenten auf Sonnet statt Opus (`CLAUDE_CODE_SUBAGENT_MODEL`). Größter Dollar-Hebel, Hauptthread bleibt stark.
- **Thinking-Deckel** — `MAX_THINKING_TOKENS` für Triviales (Denk-Tokens = teure Output-Tokens).
- **AGENTS.md / CLAUDE.md schlank** — Referenz auslagern, nur Pointer behalten. Große Kontextdateien senken Erfolg **und** erhöhen Kosten.
- **Prompt-Caching schützen** — `/compact` und Caching schlagen aggressive Deferral. Caching ist ~87% der Rechnung.

---

## Automatik (kein Agent muss erinnert werden)

Der Kern deiner Anforderung. Drei Ebenen greifen ineinander:

1. **Auto-Laden pro Runtime** — rtk läuft als Hook/Plugin, das jede Runtime beim Start selbst lädt. Der terse-Kontrakt steht in der Instruktionsdatei, die jede Runtime ohnehin liest.
2. **Self-Heal bei jedem Session-Start** — `bin/install.sh --heal` läuft als `SessionStart`-Hook und stellt fehlende Hooks/Plugins still wieder her.
3. **Regression-Gate im Deploy** — `verify-tokens` läuft im Sync-Skript (`sin-sync`); ein Regress bricht das Deployment mit `🚨`.

**Self-Heal-Hook einmalig registrieren** (`~/.claude/settings.json`):
```json
{ "hooks": { "SessionStart": [
  { "hooks": [ { "type": "command",
    "command": "bash \"$HOME/.claude/hooks/sin-save-token-heal.sh\"" } ] }
] } }
```

---

## Was wir bewusst NICHT tun (belegt schädlich)

| Tool / Ansatz | Warum nicht |
|---|---|
| **Headroom / ML-Kompression** | +48% Overhead bzw. +6,8% Kosten in abgerechneten Runs; zerstört Edit-Anker → Agent löst weniger Tasks |
| **Aggressives Tool-Search-Deferral** | `defer_loading=true` + `cache_control` schließen sich aus → bricht `/compact` (400-Fehler). Caching ist wichtiger. |
| **Auto-`/init` CLAUDE.md** | ETH-Zürich: −3% Erfolg, +20% Kosten durch aufgeblähte generierte Kontextdateien |
| **Haiku für Coding-Subs** | spart am meisten, aber senkt Coding-Qualität → gegen „nicht dümmer werden" |

---

## Evidenz

- **„Token Reduction ≠ Cost Reduction"** (arXiv 2607.12161) — 2.908 abgerechnete Claude-Code-Runs. Deterministisches rtk: −2,7% Kosten bei 96%+ Erfolg (**einziger sauberer Gewinner**). Aggressive Kompression: +6,8% Kosten, bricht SEARCH/REPLACE-Patching (27/40 → 15/40).
- **ETH Zürich / LogicStar** — LLM-generierte Kontextdateien: −3% Erfolg, +20% Kosten.
- **Anthropic / Cloudflare Code Mode** — Code-Execution 37–99% Ersparnis (Zukunfts-Backlog; braucht Sandbox).
- **Chroma „Context Rot"** — mehr Kontext ≠ besser; Attention ist ein Budget.

Volle Quellenliste + Konfig-Details: `docs/BEST-PRACTICES.md`.

---

## Repo-Layout

```
SIN-Save-Token/
├── README.md                 ← diese Datei
├── bin/
│   ├── install.sh            ← idempotenter Installer + Self-Heal-Hook-Writer
│   └── verify-tokens         ← 4-Layer Compliance-Checker (exit 1 bei Regress)
├── docs/
│   └── BEST-PRACTICES.md     ← kanonischer Standard, Konfig pro Runtime, Quellen
└── templates/
    └── subagent-preamble.md  ← terse/L1-L4-Block für Orca-Sub-Agenten
```

---

*Teil des OpenSIN-Code-Ökosystems. Kanonische Kopie des Standards:
`Infra-SIN-OpenCode-Stack/docs/TOKEN-SAVINGS-BEST-PRACTICES.md`.*
