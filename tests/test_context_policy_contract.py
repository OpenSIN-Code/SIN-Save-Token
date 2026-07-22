#!/usr/bin/env python3
"""Architecture contracts for the bounded sin-context routing policy."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "context-policy.json"
RUNTIME_PATH = ROOT / "config" / "provider-runtime.json"


class ContextPolicyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        cls.runtime = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        cls.routes = {
            route["name"]: route
            for route in cls.policy.get("routes", [])
            if isinstance(route, dict) and isinstance(route.get("name"), str)
        }

    def test_required_routing_order(self) -> None:
        self.assertEqual(
            self.routes["code_symbol"]["providers"],
            ["simone", "graphify"],
        )
        self.assertEqual(
            self.routes["code_architecture"]["providers"],
            ["graphify", "sin-code"],
        )
        self.assertEqual(
            self.routes["domain_memory"]["providers"],
            ["cognee"],
        )
        self.assertEqual(
            self.routes["session_resume"]["providers"],
            ["session-digest"],
        )
        self.assertEqual(
            self.routes["text_search"]["providers"],
            ["agent-grep"],
        )

    def test_provider_attempts_and_context_budget_are_bounded(self) -> None:
        retrieval = self.policy["retrieval"]
        budgets = self.policy["budgets"]
        self.assertLessEqual(int(retrieval["maximum_provider_attempts"]), 2)
        self.assertLessEqual(int(budgets["maximum_tokens"]), 1600)

        maximum = int(budgets["maximum_tokens"])
        for route_name, route in self.routes.items():
            budget_name = route.get("budget")
            self.assertIn(budget_name, budgets, route_name)
            self.assertLessEqual(int(budgets[budget_name]), maximum, route_name)

    def test_every_routed_provider_has_runtime_configuration(self) -> None:
        configured = set(self.runtime.get("providers", {}))
        routed = {
            provider
            for route in self.routes.values()
            for provider in route.get("providers", [])
        }
        self.assertEqual(routed - configured, set())

    def test_runtime_specs_are_bounded_and_actionable(self) -> None:
        for name, spec in self.runtime.get("providers", {}).items():
            with self.subTest(provider=name):
                argv = spec.get("argv")
                self.assertIsInstance(argv, list)
                self.assertTrue(argv)
                self.assertTrue(all(isinstance(item, str) and item for item in argv))
                self.assertGreater(int(spec.get("timeout_seconds", 0)), 0)
                self.assertGreater(int(spec.get("maximum_output_chars", 0)), 0)
                self.assertGreater(int(spec.get("failure_threshold", 0)), 0)
                self.assertGreaterEqual(int(spec.get("cooldown_seconds", -1)), 0)


if __name__ == "__main__":
    unittest.main()
