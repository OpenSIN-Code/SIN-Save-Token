#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "bin" / "sin-context"

# sin-context has no .py extension, load it manually
MODULE = ModuleType("sin_context")
MODULE.__file__ = str(MODULE_PATH)
sys.modules["sin_context"] = MODULE
exec(compile(MODULE_PATH.read_text(encoding="utf-8"), str(MODULE_PATH), "exec"), MODULE.__dict__)


class ContextBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = json.loads(
            (ROOT / "config" / "context-policy.json").read_text(
                encoding="utf-8"
            )
        )

    def test_routes_symbol_question_to_simone_first(self):
        route = MODULE.select_route(
            "Which function calls create_commit?",
            self.policy,
        )
        self.assertEqual(route["name"], "code_symbol")
        self.assertEqual(route["providers"], ["simone", "graphify"])

    def test_routes_architecture_to_graphify_then_gitnexus(self):
        route = MODULE.select_route(
            "Explain the architecture and module data flow",
            self.policy,
        )
        self.assertEqual(route["name"], "code_architecture")
        self.assertEqual(route["providers"], ["graphify", "sin-code"])

    def test_routes_decision_to_cognee(self):
        route = MODULE.select_route(
            "Warum haben wir Cognee statt einer zweiten SQLite-Datei gewählt?",
            self.policy,
        )
        self.assertEqual(route["name"], "domain_memory")
        self.assertEqual(route["providers"], ["cognee"])

    def test_fallback_is_agent_grep(self):
        route = MODULE.select_route(
            "Find FROBNICATOR_VALUE",
            self.policy,
        )
        self.assertEqual(route["name"], "text_search")

    def test_truncation_respects_budget(self):
        text = "abcd " * 5000
        result = MODULE.truncate_to_tokens(text, 100)
        self.assertLessEqual(MODULE.approximate_tokens(result), 110)
        self.assertIn("context budget reached", result)

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MODULE.ContextCache(Path(directory) / "cache.sqlite3")
            result = MODULE.ProviderResult(provider="graphify", text="compact result")
            cache.put("key", result, provider="graphify")

            restored = cache.get("key", ttl=100, negative_ttl=30)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.provider, "graphify")
            self.assertEqual(restored.text, "compact result")
            self.assertFalse(restored.is_negative)
            cache.close()

    def test_cache_key_changes_with_config_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = MODULE.cache_key(
                "code_symbol",
                "simone",
                "find symbol",
                str(root),
                650,
                policy_fingerprint="policy-a",
                provider_fingerprint="provider-a",
            )
            changed_policy = MODULE.cache_key(
                "code_symbol",
                "simone",
                "find symbol",
                str(root),
                650,
                policy_fingerprint="policy-b",
                provider_fingerprint="provider-a",
            )
            changed_provider = MODULE.cache_key(
                "code_symbol",
                "simone",
                "find symbol",
                str(root),
                650,
                policy_fingerprint="policy-a",
                provider_fingerprint="provider-b",
            )

        self.assertNotEqual(first, changed_policy)
        self.assertNotEqual(first, changed_provider)

    def test_invalid_provider_attempt_limit_is_rejected(self):
        invalid = json.loads(json.dumps(self.policy))
        invalid["retrieval"]["maximum_provider_attempts"] = 0
        with self.assertRaises(ValueError):
            MODULE.validate_policy(invalid)

    def test_every_routed_provider_has_runtime_config(self):
        specs = MODULE.load_provider_specs(
            ROOT / "config" / "provider-runtime.json"
        )
        routed = {
            provider
            for route in self.policy["routes"]
            for provider in route["providers"]
        }
        self.assertEqual(routed - set(specs), set())

    def test_infrastructure_failure_is_not_negative_cached(self):
        outcome = MODULE.ProviderOutcome(
            result=None,
            status="circuit-open",
            cache_negative=False,
        )
        self.assertFalse(outcome.cache_negative)


if __name__ == "__main__":
    unittest.main()
