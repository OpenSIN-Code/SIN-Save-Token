# SIN Token Optimizer Stack

## Decision

Integrate the four projects by responsibility, not by stacking all of their
hooks globally:

| Source | Useful mechanism | SIN integration | Default |
|---|---|---|---|
| Ponytail | Stop at the first sufficient implementation: reuse, stdlib, native platform, existing dependency, then minimum code | Compact always-on implementation ladder plus fleet skill | On |
| Caveman | Remove filler from replies; explicitly compress recurring memory files with validation and backup | Existing terse output contract plus `sin-token-stack memory-compress` | Output on; rewrites explicit |
| pxpipe | Render large stable text context as dense images | Isolated `pxpipe-run` and offline `pxpipe-export` adapters | Off |
| Gigatoken | Losslessly tokenize large corpora at very high throughput | Explicit `token-count`/`token-bench`, exact tokenizer required | Off |

This avoids three common regressions: duplicate hook injection, larger startup
prompts from overlapping skills, and lossy image context on tasks requiring
byte-exact identifiers.

## Commands

```bash
sin-token-stack status
sin-token-stack sync
sin-token-stack token-count --tokenizer openai-community/gpt2 --json README.md docs/*.md
sin-token-stack token-count --tokenizer openai-community/gpt2 --chunk-size 120000 --chunk-overlap 4000 README.md docs/*.md
sin-token-stack token-bench --tokenizer openai-community/gpt2 --validate-hf README.md
sin-token-stack memory-compress /absolute/path/CLAUDE.md --yes
sin-token-stack pxpipe-export src/
sin-token-stack pxpipe-export --git
sin-token-stack pxpipe-run --model claude-fable-5 -- claude
OPENAI_API_KEY="${OPENAI_API_KEY}" sin-token-stack pxpipe-run --model gpt-5.6-sol --accept-lossy --route openai -- claude --model gpt-5.6-sol
```

`sync` keeps inspected upstream checkouts under
`~/.local/share/sin-save-token/upstream` without installing their global hooks.
Each checkout is pinned to the full `assessed_commit`; `sync` never advances to
an unreviewed release. Set `SIN_TOKEN_STACK_HOME` to move that directory.

`PXPIPE_MODELS` controls compression, while provider routing is separate. The
wrapper sets both client base URLs and resolves `gpt-*`/o-series models to
`OPENAI_MODELS` in `--route auto`; use `--route openai` explicitly for custom
OpenAI-compatible models such as Grok. Real OpenAI traffic needs
`OPENAI_API_KEY`; Cloudflare routing needs its account ID and API token.

Gigatoken commands require `uv`, the immutable reviewed checkout, and the exact wheel versions pinned in the manifest and `runtime/gigatoken/uv.lock`. The
`--tokenizer` value is always explicit and may be a Hugging Face repository,
local tokenizer directory/JSON, `.tiktoken`, or SentencePiece `.model`. SIN
never maps a provider model name to a guessed tokenizer. `token-count` returns
lossless counts and optional fixed-window chunk plans; it does not rewrite or
send the input. `token-bench --validate-hf` verifies token-ID parity on a bounded
comparison sample before a tokenizer is trusted for a new workflow.

## System integration map

- `SIN-Save-Token` owns the manifest, immutable upstream pins, CLI guards,
  process lifecycle, measurements, and compliance gate.
- `wow-my-zsh` owns fleet distribution. The `sin-token-optimizer` skill is
  copied to Claude Code, opencode, Codex, Cline, jcode, and mimo-code; the
  compact implementation ladder also lives in canonical `shared/AGENTS.md`.
- `rtk` remains the shell/log compressor. Gigatoken does not replace RTK and does not reduce prompt size; it measures and segments exact tokenizer output at high throughput.
- Gigatoken is used by corpus/index preparation, repository budget audits, and offline chunk planning only when the exact tokenizer is known. Ponytail does not replace it; it
  reduces code written and dependencies owned.
- `sin-context`, Simone, Graphify, and `agent-grep` remain the exact-text
  retrieval path. pxpipe is not inserted into retrieval automatically.
- Orca workers and ChatGPT Web handoffs keep task packets, checkpoints, patch
  anchors, IDs, and protocol state as text. For a large read-only source bundle,
  use `pxpipe-export` and attach its pages plus `prompt.txt`/`factsheet.txt`.
- Memory files may use `memory-compress` only after explicit review. Session
  continuation still uses `session-digest`; raw transcripts are never the input.

## Rollout and measurement plan

1. Record billed cost, task success, retry count, changed lines, and duration on
   representative tasks before changing policy.
2. Keep the minimal-solution ladder and terse output active fleet-wide; compare
   total cost and successful completion, not token count alone.
3. Pilot memory compression on one duplicated non-sensitive instruction file.
   Keep it only when validation passes and the diff preserves every invariant.
4. Pilot pxpipe with offline export first. Then use isolated proxy runs only on
   allowlisted models and large semantic contexts.
5. Validate a new Gigatoken tokenizer against Hugging Face on representative Unicode, code, and special-token samples; record tokenizer identity with every measurement.
6. Review pxpipe dashboard rows for actual/counterfactual cost, cache state,
   negative-savings rows, retries, and exact-recall failures. Disable the model
   immediately if quality or total cost regresses.
7. Promote a new upstream version only by updating the reviewed immutable pin,
   running upstream and SIN suites, then re-running the same workload baseline.

Rollback is local and immediate: stop the isolated proxy process, restore the
memory backup, remove the distributed skill, or revert the manifest/policy diff.
No upstream global hook installation is required.

## pxpipe safety contract

Use only for large, dense, mostly semantic context. Keep these as native text:
secrets, credentials, hashes, opaque IDs, exact error strings, patch anchors,
and open tool/protocol state. `gpt-5.6-sol` is opt-in because upstream results
show strong arithmetic but weaker gist recall and unreliable dense exact-string
recall. The wrapper therefore requires `--accept-lossy`.

Use `pxpipe-export` before proxy mode when a client supports image uploads. It
has no request interception and makes the transformed pages inspectable.

## Memory compression contract

Memory compression is never automatic. The command delegates to Caveman's
validated compressor, which preserves code blocks, inline code, paths, URLs,
headings, and creates an original backup. Review the resulting diff before
keeping it. Do not compress source code, config, lockfiles, secrets, or material
where exact wording is legally or operationally significant.

## Attribution and updates

The adapters reference MIT-licensed upstream projects:

- DietrichGebert/ponytail
- JuliusBrussee/caveman
- teamchong/pxpipe
- marcelroed/gigatoken

`config/token-optimizer-stack.json` records the full commits assessed during
the 2026-07-24 integration. Gigatoken source is pinned for review, while execution uses the matching exact official wheel through the frozen `runtime/gigatoken` lock project rather than a global install or a costly local release build. HF parity validation enables the locked `validation` dependency group. To promote a new release, review its diff and tests,
then update the pinned commit/package version in that manifest; ordinary
`sin-token-stack sync` remains fail-closed on the reviewed versions.
