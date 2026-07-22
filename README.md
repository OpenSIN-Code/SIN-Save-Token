# SIN-Save-Token

**Der 4-Layer Token-Sparstandard für die gesamte SIN-Agenten-Flotte.**
Ein Setup, das jeder Agent (Claude Code, opencode, Codex, Orca) **automatisch selbst nutzt** —
niemand muss je einen Agenten daran erinnern.

> Ziel: **so viele Tokens wie möglich sparen, ohne dass Agenten dümmer werden.**
> Architekturentscheidungen sind durch Gates und vorhandene Evidenz begründet. Eine weltweite Bestplatzierung oder garantiert minimale Kosten gelten erst nach einem reproduzierbaren lokalen A/B-Benchmark als belegt.

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
| **L3 Memory** | Geteiltes Gedächtnis | claude-mem (Session) + **Cognee fleet** (Domain-Graph, multi-agent CLI) | kein Doppel-Spend |
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

## Voraussetzungen (externe CLIs)

Dieses Repo installiert diese Tools **nicht** — es setzt sie voraus und verweist auf sie. Fehlt eins, degradiert der jeweilige Hebel still (nie ein harter Fehler):

| CLI | Rolle | Bezug |
|---|---|---|
| `rtk` | L1 — komprimiert Shell-Output (Pflicht für den Installer) | `cargo install rtk` / `brew install rtk` |
| `graphify` | LLM-freier Code-Graph statt grep (Struktur/Blast-Radius, 0 Tokens) | separat installiert |
| `orca` | Delegation teurer Exploration an billige Modelle (opencode/mimo) | separat installiert |
| `sin` | SIN-Code-Hub (`sin verify`/`review`/`debt`) | separat installiert |
| `sin-sync` | verteilt den Standard + fährt `verify-tokens` als Deploy-Gate | `~/.local/bin/sin-sync` |
| `skillopt` | Session-Review + Skill-Selbstoptimierung | separat installiert |
| `ccusage` | Real-Dollar-Baseline (`npx ccusage@latest daily`) | via `npx`, keine Installation |

---

## Ökosystem — Verhältnis zu wow-my-zsh

**SIN-Save-Token** und **wow-my-zsh** sind komplementär, nicht überlappend:

- **wow-my-zsh** = *MCP-Config-Transpiler + Symlink-Installer* — eine kanonische
  Server-Registry, transpiliert in die native Config von 6 Agents (author once,
  transpile everywhere). Es regelt **welche Tools** ein Agent sieht.
- **SIN-Save-Token** (dieses Repo) = *Token-Disziplin-Standard* — die 4 Layer
  (Shell/Tools/Memory/Output) + Hooks, die **wie sparsam** jeder Agent mit
  Tokens umgeht. Es regelt **wie** die Agents arbeiten.

Zusammen: wow-my-zsh richtet die Werkzeuge ein, SIN-Save-Token hält ihren
Verbrauch schlank.

Vollständiger Ownership-/Install-Vertrag: [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

---

## Diagramm-Standard: Archify ist PFLICHT (kein Mermaid)

Teil des **L4 Output**-Standards: Diagramme sind Kommunikations-Artefakte, keine
Code-Blöcke. Jeder Agent der Flotte erzeugt Architektur-/Workflow-/Sequence-/
Data-Flow-/Lifecycle-Diagramme **ausschließlich über den `archify`-Skill**
(`tt-a1i/archify`), nie über Mermaid, PlantUML, ASCII-Art oder handgemachtes SVG.

- **Kanonische Regel:** `wow-my-zsh/shared/AGENTS.md` → Abschnitt *Diagrams are
  MANDATORY via Archify (never Mermaid)*. Diese Datei ist die einzige Quelle der
  Wahrheit; alle Agent-Adapter importieren/symlinken sie.
- **Install (einmalig, fleet-weit):** `npx skills add tt-a1i/archify -g`
- **Deliverable:** self-contained HTML (Dark/Light-Toggle, PNG/JPEG/WebP/SVG-Export).
  Mermaid-Source-Blöcke in Chat/Docs sind **verboten** — für inline-Vektor in READMEs
  Archify-SVG exportieren, nicht Mermaid.
- **Fünf Modi:** `architecture` (Topologie), `workflow` (Prozess), `sequence`
  (Aufrufkette), `dataflow` (Datenbewegung) und `lifecycle` (Zustände/Retry/
  Terminal). Pro Frage den passenden Modus wählen, nicht alles in einen Graphen
  pressen.
- **Artefaktvertrag:** `*.json` ist die editierbare Archify-IR, `*.html` das
  interaktive Render-Artefakt und `*.svg` der originale Archify-Vektor-Export für
  README/Docs. HTML/SVG nie von Hand bearbeiten; immer aus JSON regenerieren.
- **Blocker-Verhalten:** fehlt `archify` im Runtime, wird das explizit gemeldet und
  gracefully fall-backt — **nie** still Mermaid substituieren.

Archify senkt gleichzeitig Token-Kosten (eine HTML-Datei statt mehrseitiger
Mermaid-Round-Trips) und hebt die Diagrammqualität — ein echter L4-Gewinn, kein
Trade-off gegen „nicht dümmer werden".

### Beispiel — wow-my-zsh Architektur (mit Archify erzeugt)

![wow-my-zsh architecture](https://raw.githubusercontent.com/OpenSIN-Code/wow-my-zsh/main/docs/wow-my-zsh-architecture.svg)

> Quell-IR + gerendertes HTML im wow-my-zsh-Repo:
> [`docs/wow-my-zsh-architecture.html`](https://github.com/OpenSIN-Code/wow-my-zsh/blob/main/docs/wow-my-zsh-architecture.html)
> Das ist der originale Archify-SVG-Export ohne Browser-Chrome oder Screenshot-Ränder.
> Das HTML unterstützt weiterhin Dark/Light-Toggle und PNG/JPEG/WebP/SVG-Export
> (`T` zum Umschalten, `E` zum Export).

Der kanonische wow-my-zsh-Workflow prüft diese Artefakte mit
`node scripts/verify-archify-diagrams.mjs` und einem CI-Gate. Browser-Screenshots
sind keine Diagramm-Artefakte. Die Manifest-/Exporter-Implementierung liegt im
[wow-my-zsh-Repo](https://github.com/OpenSIN-Code/wow-my-zsh):
[`docs/archify-manifest.json`](https://github.com/OpenSIN-Code/wow-my-zsh/blob/main/docs/archify-manifest.json),
[`scripts/export-archify-svg.mjs`](https://github.com/OpenSIN-Code/wow-my-zsh/blob/main/scripts/export-archify-svg.mjs)
und [`scripts/verify-archify-diagrams.mjs`](https://github.com/OpenSIN-Code/wow-my-zsh/blob/main/scripts/verify-archify-diagrams.mjs).

---

## Repo-Layout

```
SIN-Save-Token/
├── README.md                 ← diese Datei
├── bin/
│   ├── install.sh            ← idempotenter Installer + Self-Heal-Hook-Writer
│   ├── verify-tokens         ← 4-Layer Compliance-Checker (prüft rtk-Hook/Plugin,
│   │                            MCP-Server-Zahl, Modell-Routing, always-loaded-Fläche;
│   │                            exit 1 bei Regress)
│   ├── agent-grep            ← struktur-augmentierte, selbst-kürzende Code-Suche
│   ├── memory-scope          ← jcode ② — Memory-Ranking (BM25-lite) + ehrliches ROI-Gate
│   ├── session-digest        ← jcode ③ — Transcript → kompakter Resume-Digest (~99%)
│   └── dream                 ← mimo /dream — dauerhafte Lehren → geteiltes Memory
├── hooks/                    ← agent-agnostische PreToolUse-Hooks (siehe hooks/README.md)
│   ├── rtk-auto-rewrite.js   ← rewrite `git/cargo/...` → `rtk <cmd>`
│   ├── orca-delegation-guard.js ← Nudge: teure Exploration an orca delegieren
│   ├── agent-grep-nudge.js   ← Nudge: broad Grep → agent-grep
│   ├── cache-cold-warn.js    ← warnt bei kaltem Prompt-Cache (>5 min)
│   └── lib/git-cmd.js        ← geteilter git-Command-Classifier
├── docs/
│   └── BEST-PRACTICES.md     ← kanonischer Standard, Konfig pro Runtime, Quellen
└── templates/
    └── subagent-preamble.md  ← terse/L1-L4-Block für Orca-Sub-Agenten
```

---

## `agent-grep` — Suche, die man nicht nachlesen muss

Ein Wrapper um `rg`/`grep`, der jeden Treffer **selbsterklärend** macht — die
token-stärkste Einzelidee aus dem jcode-Harness, portiert nach stdlib-Python
(keine Deps).

```bash
bin/agent-grep "process.exit" hooks/
# hooks/rtk-auto-rewrite.js
#     L40 [main] if (data.tool_name !== 'Bash') process.exit(0)
#     L59 [main] if (/^rtk(\s|$)/.test(trimmed)) process.exit(0)
#     … +2 more in this file
# — showing 14/16 hits across 2 files (caps: 8/file, 60 total).
```

Zwei Dinge über rohem grep:
1. **Umschließendes Symbol** (`[funktion]`) pro Treffer — ein Hit, der seine
   Funktion nennt, spart das Öffnen der Datei (= der eigentliche Token-Fresser).
2. **Adaptive Kürzung** mit **sichtbarem** `… +N more` — nie stilles Abschneiden
   (ein stiller Cap liest sich als „das ist alles", obwohl es das nicht ist).

Knöpfe: `AGENT_GREP_PER_FILE` (8), `AGENT_GREP_TOTAL` (60), `AGENT_GREP_CTX` (160).
Nutzt `rg` wenn vorhanden (respektiert `.gitignore`), sonst POSIX-`grep`.

---

## Cognee fleet memory (L3 domain graph)

Multi-agent shared graph for Claude / Codex / OpenCode / MiMo / Cline / Orca.
**Not** always-on MCP (0 schema tax) — HTTP API + CLI only.

```
Agents → cognee-recall / cognee-remember
       → Cognee :8011
            ├─ LLM:  OmniRoute :20128 → vag/zai/glm-5.2  (Vercel AI Gateway)
            └─ Embed: nim-embed-proxy :8012 → NVIDIA NIM nemotron-3-embed-1b @ 1024 (free)
```

### Bring-up

```bash
# Prerequisites: OmniRoute on :20128, NVIDIA_API_KEY in env (free from build.nvidia.com)
# Full stack (checks OmniRoute + starts nim-embed-proxy + cognee)
./bin/cognee-fleet-up.sh
```

### Everyday (any agent)

```bash
cognee-status
curl -s http://127.0.0.1:8012/health    # nim ok/error stats
cognee-recall "What is L2 core MCP?"
cognee-remember "short durable decision"   # uses GLM 5.2 for cognify
```

### Cost & ops

| Path | Cost |
|------|------|
| Embed (NVIDIA NIM free tier) | $0, ~40 RPM |
| `remember` / cognify | **GLM 5.2 via OmniRoute** — requires credit card on Vercel |
| Bulk re-ingest | `COGNEE_ALLOW_COSTLY=1` required |

Full policy, backends, reindex: **[docs/COGNEE-COST-POLICY.md](docs/COGNEE-COST-POLICY.md)**.

```bash
# pure local embeds (fallback)
export COGNEE_EMBED_BACKEND=fastembed
./bin/cognee-start-omniroute.sh

# after switching embed model/dims
./bin/cognee-reindex-vectors.sh
```

---

## gbrain / global-brain — kuratierter Vorbereich und Archiv

Cognee ist der **einzige kanonische Besitzer langlebiger Domain-Memory**. gbrain dient als kuratierter Vorbereich; global-brain verwaltet Pläne, Archive und Knowledge-Artefakte. Keines dieser Systeme injiziert im tokenminimalen Standard automatisch Kontext in Prompts.

`bin/brain-sync.py` unterstützt absichtlich ausschließlich einen idempotenten, kuratierten Export:

```text
gbrain --(nur markierte Einträge)--> Cognee
Cognee ----------------------------X gbrain
```

### Everyday

```bash
gbrain stats

gbrain search "credentials"
python3 bin/brain-sync.py export --dry-run
python3 bin/brain-sync.py export
python3 bin/brain-sync.py status
```

Nur Einträge mit expliziten Export-Markern beziehungsweise erlaubten Memory-Typen werden übertragen. Es gibt keine automatische Rücksynchronisation, damit keine Dubletten, Feedback-Schleifen oder mehrfaches Retrieval entstehen.

### E2E-Gate

```bash
bash bin/e2e-memory-test.sh
```

Das Gate prüft Dienste, Portkonsistenz, Routing-Konfiguration und die Einweg-Sync-Policy ohne Testdaten dauerhaft in Cognee zu schreiben.

Für den vollständigen lokalen Rollout einschließlich Unix-Modus-Reparatur, Tests, Minimal-MCP-Konvergenz, Doctor und Smoke-Benchmark:

```bash
bash bin/apply-token-minimal-local.sh
```

---

## `memory-scope` — jcode ②, aber ehrlich gegatet

jcode injiziert pro Task nur die **top-k relevanten** Memories statt des ganzen
Index. Portiert — mit einem entscheidenden CEO-Unterschied: die immer-geladene
Fläche ist heute winzig (~115 tok Index). Ein Embedding-Modell + Vektor-Store
dafür aufzusetzen **kostet mehr als es spart** — genau die „coole Tech ohne ROI",
vor der dieses Repo warnt. Also:

1. **Ranking-Engine** (deterministisch, stdlib-only, 0 API): BM25-lite über die
   Memory-Dateien, `description:`-Zeile 2× gewichtet.
   ```bash
   memory-scope "resume opencode session in claude" -k 3
   #   7.07  idea3-cross-harness-session-resume.md
   #   2.03  idea2-semantic-memory-retrieval.md
   ```
2. **ROI-Gate** (`--audit`): misst die immer-geladene Fläche und sagt **ehrlich**,
   ob sich ein Hook lohnt — unter der Schwelle „load-all is fine", darüber
   „ACTIVATE".
   ```bash
   memory-scope --audit
   # index (always): ~115 tok → VERDICT: load-all is FINE (negativer ROI)
   ```

So verdient ② seinen Hook erst, wenn das Memory-Korpus wirklich groß wird — und
keinen Turn früher. Kein Embedding-Spend auf Verdacht.

---

## `session-digest` — jcode ③, die testbare Scheibe

Cross-harness Session-Resume: statt ein 6-MB-Transcript neu zu lesen, destilliert
`session-digest` es zu einem **~700-Token-Brief** (Task, Request-Thread, berührte
Dateien, „wo wir aufgehört haben"), mit dem eine frische Session geseedet wird.

```bash
session-digest --latest                    # Claude: neueste JSONL für cwd
# 6.1 MB / ~1M tok Transcript  →  686 tok Digest  =  99.9% Reduktion

session-digest ses_09d50c0e4ffe…           # opencode: Session per id
session-digest --latest --format opencode  # opencode: neueste Session für cwd
# 23/36-Turn-Session  →  375 tok Digest
```

Architektur: dünne **Adapter** normalisieren ein Transcript zu Events; der
Digester ist harness-agnostisch. **Zwei Adapter liefern aus:**
- **Claude Code JSONL** (`~/.claude/projects/<id>/*.jsonl`).
- **opencode** — Transcripts liegen in SQLite (`~/.local/share/opencode/opencode.db`),
  nicht als JSON-Files: eine `message`-Zeile (role) + N `part`-Zeilen (text/tool);
  Tool-Dateipfade in `part.data.state.input.filePath`. Der Adapter setzt Text +
  Tools pro Message aus den Parts zusammen.

Damit ist echtes Cross-Harness-Resume live: einen `orca`-Sub (opencode/mimo)
laufen lassen → dessen opencode-Session digesten → Claude damit seeden, ohne
Report-Round-Trip. Ein mimo-Adapter dockt gleich an, sobald dessen Format
bestätigt ist. Bewusst kein Hook: Resume ist ein On-Demand-Handoff.

---

## `dream` — mimo `/dream`, portiert (dauerhafte Lehren → Memory)

Wo `session-digest` „wo haben wir aufgehört" beantwortet (flüchtiger Resume-State),
beantwortet `dream` „was haben wir GELERNT, das eine künftige Session behalten
soll": Korrekturen, Entscheidungen, Gotchas, bestätigte Ansätze. Nutzt dieselben
Adapter wie `session-digest` → liest Claude **und** opencode.

Kein LLM, kein API-Call: deterministische, **zweisprachige** (DE+EN) Signal-
Heuristik minet den Transcript nach dauerhaftem Signal (`don't`/`nicht auf`,
`we decided`/`wir nehmen`, `root cause`/`ursache`, `that worked`/`hat geklappt`).
Ausgabe sind Memory-**Kandidaten** — nie auto-committet, du bleibst in der Schleife.

```bash
dream --latest                      # Claude: neueste Session
dream ses_XXXX                       # opencode: Session per id
dream --latest --format opencode     # opencode: neueste Session für cwd
dream --write                        # Kandidaten an <memdir>/dreamed.md anhängen
```

Der einzige mimo-Import mit klar positivem ROI: `sin verify` schlägt mimos
Judge-Stop (ausführungsbasiert > Modell-Judge), Tools wie `task`/`cron`/`notebook`
hat jeder Harness schon nativ — aber cross-harness Wissens-Extraktion fehlte.

---

*Teil des OpenSIN-Code-Ökosystems. Kanonische Kopie des Standards:
`Infra-SIN-OpenCode-Stack/docs/TOKEN-SAVINGS-BEST-PRACTICES.md`.*
