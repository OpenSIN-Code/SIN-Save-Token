#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "bin" / "cognee-fleet-cli.py"

spec = importlib.util.spec_from_file_location("cognee_fleet_cli", CLI_PATH)
assert spec and spec.loader
CLI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CLI)


class CogneeFleetRuntimeTests(unittest.TestCase):
    def test_request_uses_proxy_free_opener(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = MagicMock()
        opener.open.return_value = response

        with patch.object(
            CLI.urllib.request, "build_opener", return_value=opener
        ) as build:
            with patch.object(CLI, "_api_key", return_value="test-key"):
                code, body = CLI._req("GET", "/health", timeout=3)

        self.assertEqual((code, body), (200, "{}"))
        proxy_handler = build.call_args.args[0]
        self.assertIsInstance(proxy_handler, CLI.urllib.request.ProxyHandler)
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)

    def test_recall_is_retrieval_only(self):
        captured = {}

        def fake_req(method, path, *, data=None, headers=None, timeout=30):
            captured.update(
                method=method,
                path=path,
                payload=json.loads(data.decode("utf-8")),
                timeout=timeout,
            )
            return 200, "[]"

        with patch.object(CLI, "_api_key", return_value="test-key"):
            with patch.object(CLI, "_req", side_effect=fake_req):
                result = CLI.cmd_recall(
                    Namespace(query="architecture", dataset="sin-fleet", top_k=4)
                )

        self.assertEqual(result, 0)
        self.assertEqual(captured["path"], "/api/v1/recall")
        self.assertEqual(captured["payload"]["search_type"], "CHUNKS")
        self.assertTrue(captured["payload"]["only_context"])
        self.assertEqual(captured["payload"]["top_k"], 4)
        self.assertEqual(captured["timeout"], 45)

    def test_start_script_rejects_unhealthy_substring_and_reaps_children(self):
        script = (ROOT / "bin" / "cognee-start-omniroute.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"health"[[:space:]]*:[[:space:]]*"healthy"', script)
        self.assertNotIn("grep -q healthy", script)
        self.assertIn('pkill -TERM -P "$PID"', script)
        self.assertIn('kill -KILL "$PID"', script)
        self.assertIn("export ENABLE_BACKEND_ACCESS_CONTROL=false", script)
        self.assertIn("export REQUIRE_AUTHENTICATION=false", script)

    def test_environment_adds_loopback_no_proxy(self):
        script = (ROOT / "bin" / "cognee-omniroute-env.sh").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1 localhost ::1", script)
        self.assertIn('export NO_PROXY="$_NO_PROXY_BASE"', script)
        self.assertIn('export no_proxy="$_NO_PROXY_BASE"', script)

    def test_cognee_llm_default_uses_verified_provider_neutral_fleet_route(self):
        script = (ROOT / "bin" / "cognee-omniroute-env.sh").read_text(encoding="utf-8")
        self.assertIn('export LLM_PROVIDER=openai', script)
        self.assertIn('openai/auto/best-free', script)
        self.assertNotIn('openai/nvidia/z-ai/glm-5.2', script)
        self.assertIn('Override only', script)


if __name__ == "__main__":
    unittest.main()
