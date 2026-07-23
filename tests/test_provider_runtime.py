#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_context.provider_runtime import ProviderRuntime, ProviderSpec  # noqa: E402


class ProviderRuntimeTests(unittest.TestCase):
    def test_success_resets_health_and_keeps_stderr_out_of_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="success",
                argv=[
                    sys.executable,
                    "-c",
                    "import sys; print('bounded context'); print('warning', file=sys.stderr)",
                ],
            )

            result = runtime.call(spec, cwd=root, variables={})

            self.assertTrue(result["ok"])
            self.assertEqual(result["output"], "bounded context")
            self.assertEqual(result["stderr_bytes"], len("warning\n"))
            self.assertNotIn("stderr_tail", result)
            self.assertNotIn("argv", result)
            self.assertNotIn("output_sha256", result)
            self.assertEqual(runtime.health("success")["consecutive_failures"], 0)

    def test_stderr_only_success_is_not_promoted_to_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="stderr-only",
                argv=[
                    sys.executable,
                    "-c",
                    "import sys; print('diagnostic only', file=sys.stderr)",
                ],
            )

            result = runtime.call(spec, cwd=root, variables={})

            self.assertTrue(result["ok"])
            self.assertEqual(result["output"], "")
            self.assertEqual(result["output_chars"], 0)
            self.assertGreater(result["stderr_bytes"], 0)

    def test_failure_does_not_expose_arguments_or_process_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            secret = "super-secret-provider-token"
            spec = ProviderSpec(
                name="secret-failure",
                argv=[
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "print(sys.argv[1]); "
                        "print(sys.argv[1], file=sys.stderr); "
                        "raise SystemExit(7)"
                    ),
                    secret,
                ],
            )

            result = runtime.call(spec, cwd=root, variables={})
            health = runtime.health("secret-failure")
            serialized = json.dumps(
                {"result": result, "health": health},
                sort_keys=True,
            )

            self.assertEqual(result["status"], "failed")
            self.assertNotIn(secret, serialized)
            self.assertNotIn("argv", result)
            self.assertNotIn("output_tail", result)
            self.assertNotIn("stderr_tail", result)
            self.assertEqual(result["stdout_bytes"], len(secret) + 1)
            self.assertEqual(result["stderr_bytes"], len(secret) + 1)

    def test_large_provider_output_is_drained_but_not_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="large-output",
                argv=[
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 2_000_000)",
                ],
                maximum_output_chars=256,
            )

            result = runtime.call(spec, cwd=root, variables={})

            self.assertTrue(result["ok"])
            self.assertEqual(len(result["output"]), 256)
            self.assertEqual(result["output_chars"], 256)
            self.assertEqual(result["stdout_bytes"], 2_000_000)
            self.assertTrue(result["truncated"])

    def test_call_first_available_attempts_do_not_reintroduce_raw_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            secret = "attempt-secret-value"
            spec = ProviderSpec(
                name="attempt-secret",
                argv=[
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1]); raise SystemExit(4)",
                    secret,
                ],
            )

            result = runtime.call_first_available(
                [spec],
                cwd=root,
                variables={},
            )
            serialized = json.dumps(result, sort_keys=True)

            self.assertEqual(result["status"], "all-providers-failed")
            self.assertNotIn(secret, serialized)
            self.assertNotIn("argv", serialized)
            self.assertNotIn("output_tail", serialized)
            self.assertNotIn("stderr_tail", serialized)

    def test_timeout_still_applies_after_child_closes_output_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="closed-pipes-timeout",
                argv=[
                    sys.executable,
                    "-c",
                    "import os, time; os.close(1); os.close(2); time.sleep(2)",
                ],
                timeout_seconds=0.05,
            )

            result = runtime.call(spec, cwd=root, variables={})

            self.assertEqual(result["status"], "timeout")
            self.assertFalse(result["ok"])

    def test_failure_threshold_opens_persistent_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "health.sqlite3"
            spec = ProviderSpec(
                name="broken",
                argv=[sys.executable, "-c", "raise SystemExit(7)"],
                failure_threshold=2,
                cooldown_seconds=120,
            )
            runtime = ProviderRuntime(state_path=state)

            first = runtime.call(spec, cwd=root, variables={})
            second = runtime.call(spec, cwd=root, variables={})
            reopened = ProviderRuntime(state_path=state)
            third = reopened.call(spec, cwd=root, variables={})

            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "failed")
            self.assertGreater(second["opened_until"], 0)
            self.assertEqual(third["status"], "circuit-open")

    def test_unresolved_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProviderRuntime(state_path=root / "health.sqlite3")
            spec = ProviderSpec(
                name="placeholder",
                argv=[sys.executable, "-c", "print('{missing}')"],
            )

            with self.assertRaises(ValueError):
                runtime.call(spec, cwd=root, variables={})


if __name__ == "__main__":
    unittest.main()
