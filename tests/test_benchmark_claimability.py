#!/usr/bin/env python3
"""Claimability gates for honest A/B/C benchmark reports."""

from __future__ import annotations

import json
import unittest

try:
    from jsonschema import Draft202012Validator
except ImportError:  # Local stdlib-only runs may omit the CI validation dependency.
    Draft202012Validator = None
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "benchmark-report.schema.json"
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
