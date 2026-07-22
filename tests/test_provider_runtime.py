#!/usr/bin/env python3

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_context.provider_runtime import ProviderRuntime, ProviderSpec


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
            self.assertEqual(result["stderr_tail"], "warning")
            self.assertEqual(runtime.health("success")["consecutive_failures"], 0)

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
