# SIN Token Optimizer Stack

`sin-token-stack` integrates four reviewed projects behind one fail-closed CLI.
It does not globally install their hooks and never enables lossy processing by
default.

| Component | Purpose | Integration | Default |
|---|---|---|---|
| Ponytail | Prefer reuse, standard library, native platform, existing dependency, then minimum new code | Always-on policy only | On |
| Caveman | Shorter output and explicit compression of recurring memory files | Policy plus explicit file-rewrite command | Output policy on; rewrites off |
| pxpipe | Render large, stable text context as dense PNG pages or proxy selected model traffic | Locked isolated npm runtime | Off |
| Gigatoken | Exact model-bound tokenization and throughput benchmarks | Locked isolated `uv` runtime | Off |

## Install and status

The normal installer links the CLI but performs no network synchronization:

```bash
bin/install.sh
sin-token-stack status --check
```

An entirely unsynchronized stack is valid: all four source checkouts and the
managed pxpipe runtime are optional until their commands are used. `status
--check` fails only when an installed checkout/runtime drifts, a committed lock
manifest is invalid, or a synchronized component is unusable.

Synchronize immutable reviewed sources explicitly:

```bash
sin-token-stack sync
sin-token-stack sync --source pxpipe
sin-token-stack sync --source gigatoken
```

Each source is detached at the full commit recorded in
`config/token-optimizer-stack.json`. Dirty worktrees, unexpected origins,
symlinked managed directories, version drift, and incomplete runtimes fail
closed. pxpipe is installed with `npm ci --ignore-scripts` from the committed
`runtime/pxpipe/package-lock.json`; `npx` and global packages are not used.

## Gigatoken

```bash
sin-token-stack token-count \
  --tokenizer openai-community/gpt2 \
  --json README.md docs/*.md

sin-token-stack token-count \
  --tokenizer openai-community/gpt2 \
  --chunk-size 120000 \
  --chunk-overlap 4000 \
  README.md docs/*.md

sin-token-stack token-bench \
  --tokenizer openai-community/gpt2 \
  --validate-hf README.md
```

Token counts are exact only for the tokenizer explicitly named in the command.
They do not imply provider billing parity, hidden-token counts, cache pricing,
or compatibility with a similarly named hosted model. Run `--validate-hf` for
a new tokenizer before using measurements in production planning.

## Caveman data boundary

`memory-compress` invokes the reviewed Caveman implementation. That
implementation sends the selected file's prose to Claude/Anthropic, either via
`ANTHROPIC_API_KEY` or the local `claude --print` client. It is therefore an
external data transfer, not an offline transform.

Both consent flags are required:

```bash
sin-token-stack memory-compress /absolute/path/CLAUDE.md \
  --yes \
  --allow-third-party-upload \
  --timeout 300
```

Never use it for secrets, credentials, private keys, customer data, regulated
data, opaque identifiers, exact protocol transcripts, or files whose contents
may not leave the machine. Symlink inputs, sensitive names/directories, and
files over 500 KB are refused. On timeout or interrupt, the complete process
group is terminated.

Caveman stores the original outside the source directory under its platform
data directory, normally:

```text
~/.local/share/caveman-compress/backups/<source-parent>/<stem>.original.md
```

The wrapper accepts success only when that external backup exists and exactly
matches the original input. Existing backups block execution before upload.
Confirm the reported backup before relying on the rewritten file.

## pxpipe safety contract

Offline export is preferred because it makes the generated PNGs, prompt, and
factsheet inspectable before they are attached:

```bash
sin-token-stack pxpipe-export src/
sin-token-stack pxpipe-export --git
```

Proxy mode is isolated to one child command and a loopback listener:

```bash
sin-token-stack pxpipe-run \
  --model claude-fable-5 \
  -- claude

OPENAI_API_KEY="$OPENAI_API_KEY" sin-token-stack pxpipe-run \
  --model gpt-5.6-sol \
  --accept-lossy \
  --route openai \
  -- claude --model gpt-5.6-sol
```

Only default-validated models run without `--accept-lossy`. Other explicitly
reviewed models require the flag; unknown models are rejected. Do not image:

- secrets, credentials, private keys, or personal data;
- hashes and opaque identifiers;
- patch anchors and byte-exact protocol state;
- exact error strings needed for search, matching, or tests.

The proxy receives the selected command's API traffic and forwards it to the
configured provider. Provider credentials remain provider-bound but pass
through the local pxpipe process. The adapter reserves the chosen loopback port,
waits for readiness, and always terminates the proxy process group after the
child exits.

## System integration map

- `SIN-Save-Token` owns the manifest, immutable upstream pins, CLI guards,
  process lifecycle, runtime locks, measurements, and compliance gate.
- `wow-my-zsh` owns fleet distribution. The `sin-token-optimizer` skill is
  copied to supported runtimes; no upstream global hook installation occurs.
- `rtk` remains the shell/log compressor. Gigatoken measures and segments exact
  tokenizer output; it does not replace RTK or reduce prompt size by itself.
- `sin-context`, Simone, Graphify, and `agent-grep` remain the exact-text
  retrieval path. pxpipe is never inserted into retrieval automatically.
- Orca workers and ChatGPT Web handoffs keep task packets, checkpoints, patch
  anchors, IDs, and protocol state as text.

## Supply-chain update procedure

To promote a new upstream version:

1. Review the source diff and license at a full immutable commit.
2. Run upstream tests and inspect network, filesystem, subprocess, and
   credential behavior.
3. Update the source commit and exact runtime package version.
4. Regenerate the relevant lockfile on a clean machine.
5. Record and verify exact npm SRI hashes for pxpipe and its pinned transitives.
6. Run focused regression tests, real smoke tests, and complete repository CI.
7. Merge only after all checks pass on the exact release commit.

Never loosen a pin merely to make `sync` succeed.

## Attribution

The adapters reference MIT-licensed upstream projects:

- DietrichGebert/ponytail
- JuliusBrussee/caveman
- teamchong/pxpipe
- marcelroed/gigatoken
