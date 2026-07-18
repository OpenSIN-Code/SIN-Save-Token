# SIN-Save-Token

**Der 4-Layer Token-Sparstandard für die gesamte SIN-Agenten-Flotte.**
Ein Setup, das jeder Agent (Claude Code, opencode, Codex, Orca) **automatisch selbst nutzt** —
niemand muss je einen Agenten daran erinnern.

> Ziel: **so viele Tokens wie möglich sparen, ohne dass Agenten dümmer werden.**
> Jede Entscheidung hier ist durch abgerechnete Benchmarks belegt (siehe [Evidenz](#evidenz)).

---

## TL;DR — Installation auf einem neuen Mac / in einem neuen System

```bash
# an einen STABILEN Ort klonen (nicht /tmp — der Self-Heal-Hook zeigt hierauf):
git clone https://github.com/OpenSIN-Code/SIN-Save-Token.git ~/dev/SIN-Save-Token
cd ~/dev/SIN-Save-Token
# rtk muss vorhanden sein (RTK - Rust Token Killer):
#   cargo install rtk        # oder brew install rtk
./bin/install.sh             # richtet alle Runtimes ein, idempotent
```

Danach den Self-Heal-Hook **einmalig** in `~/.claude/settings.json` registrieren (siehe [Automatik](#automatik-kein-agent-muss-erinnert-werden)) —
das Ein-Zeilen-Snippet unten macht es idempotent. Ab dann ist es **selbsterhaltend**: jede neue Session repariert fehlende Hooks still.

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

**Layer-übergreifende Hebel (Session 3 & 4, alles gated/mandatory):**

**[L0] Baseline-Messung + Modell-Routing** (`verify-tokens` [L0]-Gate):
- `ccusage` — Real-Dollar-Baseline pro Tag/Session/Block. Befehl: `npx ccusage@latest daily`
- `CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5` — Subagenten auf Sonnet, nicht Opus (40% cheaper). Gate: Failif unset oder Opus.

**[L4-input] Immer-geladene Oberfläche** (`verify-tokens` [L4-input]-Gate):
- `paths:`-Scoping — Rules/Skills mit Glob laden **nur bei passenden Dateien** (−41% always-loaded, dokumentiert). Claude-Code–spezifisch.
- Skill-Sprawl-Budget — max 30 Skills / 8 KB Beschreibungen. ~100 Tokens pro Skill @ Start.
- `disable-model-invocation: true` — nur für reine Slash-Commands (passive Trigger ~30–50% = Coinflip).
- **Slash-First-Habit** — wenn Skill bekannt, `/skill-name` direkt aufrufen statt auf Trigger hoffen.
- `.claudeignore` — `node_modules/ .cache/ *.log __pycache__/ dist/ target/ .rtk/ .planning/graphs/` und Lockfiles blockieren.

**Klassische Hebel** (dokumentiert, größtenteils bereits live):
- **Modell-Routing** (L0) — Subagenten auf Sonnet statt Opus. Größter Dollar-Hebel, Hauptthread bleibt stark.
- **Prompt-Caching schützen** — `/compact` + Caching schlagen aggressive Deferral. ~85–90% der Input-Bill.
- **AGENTS.md / CLAUDE.md schlank** — Referenz auslagern → 41% always-loaded-Reduktion möglich. Größe-Gate verhindert Re-Bloat.
- **Thinking-Deckel** — `MAX_THINKING_TOKENS` für Triviales (Denk-Tokens = teure Output-Tokens).

---

## Automatik (kein Agent muss erinnert werden)

Der Kern deiner Anforderung. Vier Ebenen greifen ineinander:

1. **Auto-Laden pro Runtime** — rtk läuft als Hook/Plugin, das jede Runtime beim Start selbst lädt. Der terse-Kontrakt steht in der Instruktionsdatei, die jede Runtime ohnehin liest.
2. **Self-Heal bei jedem Session-Start** — `bin/install.sh --heal` läuft als `SessionStart`-Hook und stellt fehlende Hooks/Plugins still wieder her.
3. **Drift-Detection bei Session-Start** — `verify-tokens` läuft **silent** nach dem Heal (grüne Hosts = kein Output). Drift wird LAUT (🚨 REGRESSION auf stderr). So bleibt jede Session selbstbewusst ohne Spam.
4. **Regression-Gate im Deploy** — `verify-tokens` läuft im Sync-Skript (`sin-sync`); ein Regress bricht das Deployment mit `🚨`.

**Self-Heal-Hook einmalig registrieren** — idempotent per Python (kein Duplikat bei Mehrfachlauf):
```bash
python3 - "$HOME/.claude/settings.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p))
ss=d.setdefault("hooks",{}).setdefault("SessionStart",[])
cmd='bash "%s/.claude/hooks/sin-save-token-heal.sh"'%__import__("os").environ["HOME"]
if not any('sin-save-token-heal' in x.get('command','') for g in ss for x in g.get('hooks',[])):
    ss.append({"hooks":[{"type":"command","command":cmd}]}); json.dump(d,open(p,'w'),indent=2); print("registered")
else: print("already registered")
PY
```
Resultierender Eintrag unter `hooks.SessionStart`:
```json
{ "hooks": [ { "type": "command",
  "command": "bash \"$HOME/.claude/hooks/sin-save-token-heal.sh\"" } ] }
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
