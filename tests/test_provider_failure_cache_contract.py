#!/usr/bin/env python3
"""Hermetic circuit-breaker and negative-cache contracts."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from sin_context.provider_runtime import ProviderRuntime, ProviderSpec


def load_context_cli():
    path = ROOT / "bin" / "sin-context"
    loader = importlib.machinery.SourceFileLoader("sin_context_contract_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


CONTEXT = load_context_cli()


class ProviderFailureCacheContractTests(unittest.TestCase):
    def test_success_resets_previous_failure_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            failed = ProviderSpec(
                name="resettable",
                argv=[sys.executable, "-c", "raise SystemExit(9)"],
                failure_threshold=3,
                cooldown_seconds=60,
            )
            succeeded = ProviderSpec(
                name="resettable",
                argv=[sys.executable, "-c", "print('ok')"],
                failure_threshold=3,
                cooldown_seconds=60,
            )

            self.assertEqual(runtime.call(failed, cwd=root, variables={})["status"], "failed")
            self.assertEqual(runtime.health("resettable")["consecutive_failures"], 1)
            self.assertTrue(runtime.call(succeeded, cwd=root, variables={})["ok"])
            self.assertEqual(runtime.health("resettable")["consecutive_failures"], 0)
            self.assertEqual(runtime.health("resettable")["opened_until"], 0)

    def test_missing_binary_counts_as_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="missing",
                argv=["definitely-not-a-real-provider-binary-7f91"],
                failure_threshold=2,
                cooldown_seconds=60,
            )

            first = runtime.call(spec, cwd=root, variables={})
            second = runtime.call(spec, cwd=root, variables={})
            self.assertEqual(first["status"], "unavailable")
            self.assertEqual(first["consecutive_failures"], 1)
            self.assertEqual(second["status"], "unavailable")
            self.assertGreater(second["opened_until"], 0)

    def test_open_circuit_does_not_launch_process_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / "launches.txt"
            code = (
                "from pathlib import Path; "
                f"p=Path({str(counter)!r}); "
                "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); "
                "raise SystemExit(4)"
            )
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="guarded",
                argv=[sys.executable, "-c", code],
                failure_threshold=1,
                cooldown_seconds=120,
            )

            first = runtime.call(spec, cwd=root, variables={})
            second = runtime.call(spec, cwd=root, variables={})
            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "circuit-open")
            self.assertEqual(counter.read_text(encoding="utf-8"), "x")

    def test_attempt_is_allowed_after_cooldown_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "health.sqlite3"
            counter = root / "launches.txt"
            code = (
                "from pathlib import Path; "
                f"p=Path({str(counter)!r}); "
                "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); "
                "raise SystemExit(4)"
            )
            runtime = ProviderRuntime(state_path=state)
            spec = ProviderSpec(
                name="cooldown",
                argv=[sys.executable, "-c", code],
                failure_threshold=1,
                cooldown_seconds=120,
            )

            runtime.call(spec, cwd=root, variables={})
            with sqlite3.connect(state) as connection:
                connection.execute(
                    "UPDATE provider_health SET opened_until = 0 WHERE provider = ?",
                    ("cooldown",),
                )
                connection.commit()
            retried = runtime.call(spec, cwd=root, variables={})
            self.assertEqual(retried["status"], "failed")
            self.assertEqual(counter.read_text(encoding="utf-8"), "xx")

    def test_timeout_is_infrastructure_failure_and_not_negative_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            specs = {
                "slow": ProviderSpec(
                    name="slow",
                    argv=[sys.executable, "-c", "import time; time.sleep(2)"],
                    timeout_seconds=0.01,
                    failure_threshold=2,
                    cooldown_seconds=60,
                )
            }
            outcome = CONTEXT.call_runtime_provider(
                "slow",
                cwd=str(root),
                variables={},
                runtime=runtime,
                specs=specs,
            )
            self.assertEqual(outcome.status, "timeout")
            self.assertFalse(outcome.cache_negative)
            self.assertIsNone(outcome.result)

    def test_unavailable_binary_is_not_negative_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            specs = {
                "missing": ProviderSpec(
                    name="missing",
                    argv=["definitely-not-a-real-provider-binary-3c21"],
                )
            }
            outcome = CONTEXT.call_runtime_provider(
                "missing",
                cwd=str(root),
                variables={},
                runtime=runtime,
                specs=specs,
            )
            self.assertEqual(outcome.status, "unavailable")
            self.assertFalse(outcome.cache_negative)

    def test_empty_success_is_negative_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            specs = {
                "empty": ProviderSpec(
                    name="empty",
                    argv=[sys.executable, "-c", "pass"],
                )
            }
            outcome = CONTEXT.call_runtime_provider(
                "empty",
                cwd=str(root),
                variables={},
                runtime=runtime,
                specs=specs,
            )
            self.assertEqual(outcome.status, "empty")
            self.assertTrue(outcome.cache_negative)
            self.assertIsNone(outcome.result)

    def test_positive_and_negative_cache_ttl_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = CONTEXT.ContextCache(Path(directory) / "context.sqlite3")
            positive = CONTEXT.ProviderResult("provider", "useful context")
            with patch.object(CONTEXT.time, "time", return_value=1000):
                cache.put("positive", positive, provider="provider")
                cache.put("negative", None, provider="provider")

            with patch.object(CONTEXT.time, "time", return_value=1006):
                self.assertIsNotNone(cache.get("positive", ttl=10, negative_ttl=5))
                self.assertIsNone(cache.get("negative", ttl=10, negative_ttl=5))


if __name__ == "__main__":
    unittest.main()
