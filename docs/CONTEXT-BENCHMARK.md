# Context benchmark A/B/C

`bin/benchmark-context` is the canonical producer for an honest A/B/C comparison of context retrieval cost and quality.

The benchmark is intentionally fail-closed. It does not guess what an unspecialized baseline or a deliberately full stack means for a particular agent/provider setup.

## Variants

The three claimable variants must execute the same task set and repetition IDs:

- **A — baseline**: the explicitly chosen no-SST command.
- **B — SST**: `bin/sin-context`, measured cold for the claimable B variant. A warm SST run is recorded only as supplemental evidence.
- **C — full stack**: the explicitly chosen all-systems command.

A and C are supplied through command templates:

```bash
export SST_BENCH_BASELINE_COMMAND='...'
export SST_BENCH_FULL_STACK_COMMAND='...'
python3 bin/benchmark-context
```

Each template may use `{query}` and `{cwd}`. The benchmark parses the rendered command with `shlex` and executes it directly without a shell.

Do not substitute an arbitrary installed CLI for either variable. A command is benchmark-ready only when its semantics are deliberately selected for the variant and it emits the telemetry contract below.

## Required telemetry

Every comparable A/B/C run must preserve these metrics:

- `input_tokens`
- `cache_read_tokens`
- `cache_write_tokens`
- `output_tokens`
- `duration_ms`
- `success`
- `provider_attempts`
- `cache_hit`

External A/C commands provide exact token/cache/provider telemetry by returning a top-level JSON object containing:

```json
{
  "benchmark_metrics": {
    "input_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "output_tokens": 0,
    "provider_attempts": 0,
    "cache_hit": false
  }
}
```

The numeric values above illustrate the JSON shape only; they are not benchmark results.

The command must also return the task answer in stdout so the configured `expected_any` markers can evaluate success. `duration_ms` is measured by the harness around the command execution.

If exact telemetry is absent for any A/B/C run, `claimable_abc_comparison` remains `false`. Approximate output-token fallbacks may be stored for diagnostics but never make an A/B/C report claimable.

## Run and validate

The representative task set lives in `config/benchmark-tasks.json`; the output contract is defined by `schemas/benchmark-report.schema.json` and guarded by `tests/test_benchmark_claimability.py`.

A normal run is:

```bash
python3 bin/benchmark-context --repetitions 1
```

For a smoke test of SST only:

```bash
python3 bin/benchmark-context --allow-partial --repetitions 1
```

A partial run is explicitly non-claimable. It is useful only for harness/runtime diagnostics and must not be presented as an A/B/C comparison.

Focused verification:

```bash
python3 -m pytest -q tests/test_benchmark_claimability.py
```

## Failure and blocker policy

`bin/benchmark-context` exits with status 2 before executing benchmark tasks when either required A/C command is missing and `--allow-partial` was not requested.

Treat that as a configuration/external-dependency blocker, not as permission to invent a baseline, infer a provider command, reuse unrelated historical measurements, or fabricate token/cache telemetry. Record which command templates are unavailable and keep GitHub issue #10 open until deliberate, reproducible A and C commands are supplied.

Likewise, if a selected command requires provider authorization, credits, network access, or another external dependency that is not available, record the dependency factually and stop rather than spending money or changing credentials merely to make the benchmark pass.
