#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "sin_token_stack.py"
SPEC = importlib.util.spec_from_file_location("sin_token_stack", MODULE_PATH)
assert SPEC and SPEC.loader
STACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STACK)


class TokenOptimizerStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(
            (ROOT / "config" / "token-optimizer-stack.json").read_text(encoding="utf-8")
        )

    def test_all_sources_are_mit_and_have_distinct_roles(self) -> None:
        upstreams = self.config["upstreams"]
        self.assertEqual(set(upstreams), {"ponytail", "caveman", "pxpipe", "gigatoken"})
        self.assertTrue(all(item["license"] == "MIT" for item in upstreams.values()))
        self.assertEqual(len({item["role"] for item in upstreams.values()}), 4)

    def test_assessed_commits_are_full_immutable_hashes(self) -> None:
        for spec in self.config["upstreams"].values():
            commit = spec["assessed_commit"]
            self.assertEqual(len(commit), 40)
            self.assertTrue(all(char in "0123456789abcdef" for char in commit))

    def test_git_url_normalization_keeps_origin_check_stable(self) -> None:
        self.assertEqual(
            STACK.normalize_git_url("https://github.com/teamchong/pxpipe.git"),
            "https://github.com/teamchong/pxpipe",
        )

    def test_lossy_features_are_explicit_only(self) -> None:
        policy = self.config["policy"]
        self.assertIn("visual-context-compression", policy["explicit_only"])
        self.assertEqual(policy["pxpipe"]["default_mode"], "off")

    def test_safe_model_does_not_require_acknowledgement(self) -> None:
        allowed, reason = STACK.pxpipe_policy("claude-fable-5", False, self.config)
        self.assertTrue(allowed)
        self.assertEqual(reason, "validated-default")

    def test_sol_requires_explicit_lossy_acknowledgement(self) -> None:
        allowed, reason = STACK.pxpipe_policy("gpt-5.6-sol", False, self.config)
        self.assertFalse(allowed)
        self.assertIn("accept-lossy", reason)
        allowed, reason = STACK.pxpipe_policy("gpt-5.6-sol", True, self.config)
        self.assertTrue(allowed)
        self.assertEqual(reason, "lossy-opt-in")

    def test_unknown_model_fails_closed(self) -> None:
        allowed, reason = STACK.pxpipe_policy("gpt-5.6-terra", True, self.config)
        self.assertFalse(allowed)
        self.assertEqual(reason, "model is not allowlisted")

    def test_npx_fallback_names_the_pxpipe_binary_explicitly(self) -> None:
        from unittest import mock

        def fake_which(name: str):
            return "/opt/homebrew/bin/npx" if name == "npx" else None

        with mock.patch.object(STACK.shutil, "which", side_effect=fake_which):
            argv = STACK.pxpipe_argv(self.config)
        self.assertEqual(argv[:4], ["/opt/homebrew/bin/npx", "-y", "--package", "pxpipe-proxy@0.10.0"])
        self.assertEqual(argv[4], "pxpipe")

    def test_provider_routing_is_separate_from_compression_allowlist(self) -> None:
        self.assertEqual(STACK.resolve_pxpipe_route("gpt-5.6-sol"), "openai")
        self.assertEqual(STACK.resolve_pxpipe_route("claude-fable-5"), "default")
        self.assertEqual(STACK.resolve_pxpipe_route("grok-4.5", "openai"), "openai")
        env: dict[str, str] = {}
        STACK.configure_pxpipe_routing(env, "gpt-5.6-sol", "openai")
        self.assertEqual(env["OPENAI_MODELS"], "gpt-5.6-sol")
        self.assertNotIn("CLOUDFLARE_MODELS", env)

    def test_never_image_precision_categories_exist(self) -> None:
        blocked = set(self.config["policy"]["pxpipe"]["never_image"])
        self.assertTrue({"secrets", "hashes", "opaque-identifiers", "patch-anchors"} <= blocked)


    def test_gigatoken_is_explicit_and_never_assumes_provider_parity(self) -> None:
        self.assertEqual(self.config["upstreams"]["gigatoken"]["python_package"], "gigatoken==0.9.0")
        self.assertEqual(self.config["upstreams"]["gigatoken"]["validation_package"], "tokenizers==0.22.2")
        self.assertEqual(self.config["upstreams"]["gigatoken"]["runtime_project"], "runtime/gigatoken")
        self.assertEqual(self.config["upstreams"]["gigatoken"]["validation_group"], "validation")
        policy = self.config["policy"]["gigatoken"]
        self.assertEqual(policy["default_mode"], "off")
        self.assertTrue(policy["require_explicit_tokenizer"])
        self.assertTrue(policy["never_assume_provider_parity"])
        self.assertIn("model-bound-token-measurement", self.config["policy"]["explicit_only"])

    def test_chunk_planning_math_and_guards(self) -> None:
        self.assertEqual(STACK.planned_chunks(0, 1000, 100), 0)
        self.assertEqual(STACK.planned_chunks(800, 1000, 100), 1)
        self.assertEqual(STACK.planned_chunks(2500, 1000, 100), 3)
        self.assertIsNone(STACK.planned_chunks(2500, None, 0))
        with self.assertRaises(STACK.StackError):
            STACK.planned_chunks(100, 100, 100)

    def test_gigatoken_runtime_is_isolated_and_pinned(self) -> None:
        from unittest import mock

        source = Path("/tmp/reviewed-gigatoken")
        runtime = ROOT / "runtime" / "gigatoken"
        self.assertTrue((runtime / "pyproject.toml").is_file())
        self.assertTrue((runtime / "uv.lock").is_file())
        with mock.patch.object(STACK, "ensure_assessed_source", return_value=source), mock.patch.object(
            STACK, "assessed_gigatoken_version", return_value="0.9.0"
        ), mock.patch.object(STACK.shutil, "which", return_value="/opt/homebrew/bin/uv"):
            argv = STACK.gigatoken_runtime_argv(self.config)
        self.assertEqual(
            argv,
            [
                "/opt/homebrew/bin/uv",
                "run",
                "--quiet",
                "--frozen",
                "--project",
                str(runtime.resolve()),
            ],
        )

    def test_token_count_requires_explicit_input(self) -> None:
        import argparse

        args = argparse.Namespace(
            stdin=False,
            files=[],
            tokenizer="openai-community/gpt2",
            doc_separator=None,
            chunk_size=None,
            chunk_overlap=0,
            json=False,
        )
        with self.assertRaises(STACK.StackError):
            STACK.cmd_token_count(args)


if __name__ == "__main__":
    unittest.main()
