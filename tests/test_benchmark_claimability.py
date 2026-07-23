#!/usr/bin/env python3
"""Claimability gates for honest A/B/C benchmark reports."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError:  # Local stdlib-only runs may omit the CI validation dependency.
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "benchmark-report.schema.json"
BENCHMARK_PATH = ROOT / "bin" / "benchmark-context"
VARIANTS = ("baseline", "sst", "full_stack")
METRIC_FIELDS = {
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "duration_ms",
    "success",
    "provider_attempts",
    "cache_hit",
}
NUMERIC_METRIC_FIELDS = {
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "duration_ms",
    "provider_attempts",
}


def load_benchmark_cli():
    loader = importlib.machinery.SourceFileLoader(
        "benchmark_context_contract_cli",
        str(BENCHMARK_PATH),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {BENCHMARK_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


BENCHMARK = load_benchmark_cli()


def is_claimable(report: dict[str, Any]) -> bool:
    variants = report.get("variants")
    if not isinstance(variants, dict):
        return False
    if report.get("errors"):
        return False

    task_sets: list[set[str]] = []
    for variant_name in VARIANTS:
        variant = variants.get(variant_name)
        if not isinstance(variant, dict):
            return False
        runs = variant.get("runs")
        if not isinstance(runs, list) or not runs:
            return False

        task_ids: list[str] = []
        for run in runs:
            if not isinstance(run, dict):
                return False
            task_id = run.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                return False
            if run.get("variant") != variant_name:
                return False
            metrics = run.get("metrics")
            if not isinstance(metrics, dict) or not METRIC_FIELDS <= set(metrics):
                return False
            if not isinstance(metrics.get("success"), bool):
                return False
            if not isinstance(metrics.get("cache_hit"), bool):
                return False
            for field in NUMERIC_METRIC_FIELDS:
                value = metrics.get(field)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    return False
            task_ids.append(task_id)

        if len(task_ids) != len(set(task_ids)):
            return False
        task_sets.append(set(task_ids))

    return task_sets[0] == task_sets[1] == task_sets[2]


def metrics(*, success: bool = True) -> dict[str, Any]:
    return {
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 20,
        "duration_ms": 15,
        "success": success,
        "provider_attempts": 1,
        "cache_hit": False,
    }


def producer_result(
    mode: str,
    *,
    complete_telemetry: bool = True,
    success: bool = True,
) -> Any:
    telemetry = {
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reported_output_tokens": 20,
        "provider_attempts": 1,
        "telemetry_cache_hit": False,
    } if complete_telemetry else {}
    return BENCHMARK.RunResult(
        task_id="task-1",
        mode=mode,
        repetition=1,
        command=BENCHMARK.persisted_command_summary(
            ["runner", "--api-key", "top-secret", "positional-secret"]
        ),
        exit_code=0 if success else 1,
        duration_ms=15,
        success=success,
        expected_match="task",
        stdout_chars=60,
        stderr_chars=0,
        approx_output_tokens=20,
        **telemetry,
    )


def producer_report(*, complete_telemetry: bool = True) -> dict[str, Any]:
    results = [
        producer_result(mode, complete_telemetry=complete_telemetry)
        for mode in ("baseline", "sst-cold", "full-stack", "sst-warm")
    ]
    return BENCHMARK.build_report(
        results=results,
        tasks=[{"id": "task-1", "query": "task", "expected_any": ["task"]}],
        cwd=ROOT,
        tasks_path=ROOT / "config" / "benchmark-tasks.json",
        repetitions=1,
        missing_modes=[],
    )


def complete_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-07-22T20:00:00Z",
        "claimable_abc_comparison": True,
        "task_set_hash": "a" * 64,
        "variants": {
            name: {
                "runs": [
                    {"task_id": "task-1", "variant": name, "metrics": metrics()},
                    {"task_id": "task-2", "variant": name, "metrics": metrics()},
                ]
            }
            for name in VARIANTS
        },
        "errors": [],
        "environment": {"python": "3.x"},
    }


class BenchmarkClaimabilityTests(unittest.TestCase):
    def test_sst_only_report_is_not_claimable(self) -> None:
        report = complete_report()
        report["variants"]["baseline"]["runs"] = []
        report["variants"]["full_stack"]["runs"] = []
        self.assertFalse(is_claimable(report))

    def test_complete_abc_report_is_claimable(self) -> None:
        self.assertTrue(is_claimable(complete_report()))

    def test_variants_must_use_identical_task_ids(self) -> None:
        report = complete_report()
        report["variants"]["full_stack"]["runs"][1]["task_id"] = "other-task"
        self.assertFalse(is_claimable(report))

    def test_duplicate_task_ids_are_rejected(self) -> None:
        report = complete_report()
        report["variants"]["sst"]["runs"][1]["task_id"] = "task-1"
        self.assertFalse(is_claimable(report))

    def test_missing_success_metric_prevents_ranking_claims(self) -> None:
        report = complete_report()
        del report["variants"]["baseline"]["runs"][0]["metrics"]["success"]
        self.assertFalse(is_claimable(report))

    def test_recorded_errors_prevent_claimability(self) -> None:
        report = complete_report()
        report["errors"] = ["baseline command failed"]
        self.assertFalse(is_claimable(report))

    def test_schema_declares_required_variants_and_metrics(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertTrue(
            {
                "schema_version",
                "generated_at",
                "claimable_abc_comparison",
                "task_set_hash",
                "variants",
                "errors",
                "environment",
            }
            <= set(schema["required"])
        )
        self.assertEqual(set(schema["properties"]["variants"]["required"]), set(VARIANTS))
        self.assertTrue(METRIC_FIELDS <= set(schema["$defs"]["metrics"]["required"]))

    def test_real_producer_emits_claimable_schema_report_with_complete_telemetry(self) -> None:
        report = producer_report(complete_telemetry=True)
        self.assertTrue(report["claimable_abc_comparison"])
        self.assertTrue(is_claimable(report))
        self.assertEqual(
            set(report["variants"]),
            {"baseline", "sst", "full_stack"},
        )
        self.assertEqual(len(report["task_set_hash"]), 64)
        if Draft202012Validator is not None:
            schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(report)

    def test_real_producer_refuses_claims_without_exact_telemetry(self) -> None:
        report = producer_report(complete_telemetry=False)
        self.assertFalse(report["claimable_abc_comparison"])
        self.assertFalse(is_claimable(report))
        self.assertTrue(
            any("exact token/cache telemetry missing" in error for error in report["errors"])
        )

    def test_benchmark_redacts_secret_bearing_arguments_and_omits_them_from_reports(self) -> None:
        argv = BENCHMARK.redact_argv(
            [
                "runner",
                "--api-key",
                "top-secret",
                "--token=second-secret",
                "https://user:password@example.invalid/path",
            ]
        )
        rendered = " ".join(argv)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("second-secret", rendered)
        self.assertNotIn("password@example", rendered)
        self.assertIn("<redacted>", rendered)
        self.assertEqual(
            BENCHMARK.redact_sensitive_text("password=hunter2"),
            "password=<redacted>",
        )
        summary = BENCHMARK.persisted_command_summary(
            ["runner", "--api-key", "top-secret", "positional-secret"]
        )
        self.assertEqual(summary[0], "runner")
        self.assertEqual(summary[1], "<3 arguments omitted>")
        self.assertNotIn("top-secret", " ".join(summary))
        self.assertNotIn("positional-secret", " ".join(summary))

        report = producer_report(complete_telemetry=True)
        evidence = report["variants"]["baseline"]["runs"][0]["evidence"]
        self.assertNotIn("stderr_tail", evidence)
        self.assertNotIn("stderr_sha256", evidence)
        self.assertNotIn("stdout_sha256", evidence)
        self.assertNotIn("command_sha256", evidence)
        self.assertEqual(evidence["command"], ["runner", "<3 arguments omitted>"])

    def test_schema_validates_complete_report(self) -> None:
        if Draft202012Validator is None:
            self.skipTest("jsonschema is required for full schema validation")
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(complete_report())

    def test_claimable_flag_must_not_overrule_missing_evidence(self) -> None:
        report = complete_report()
        report["variants"]["baseline"]["runs"] = []
        report["claimable_abc_comparison"] = True
        self.assertFalse(is_claimable(report))


if __name__ == "__main__":
    unittest.main()
