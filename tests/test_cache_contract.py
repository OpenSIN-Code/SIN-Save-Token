#!/usr/bin/env python3
"""Hermetic cache-key contracts for repository state and token budgets."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


def load_context_cli():
    path = ROOT / "bin" / "sin-context"
    loader = importlib.machinery.SourceFileLoader("sin_context_cache_contract_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


CONTEXT = load_context_cli()


class CacheKeyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required for repository fingerprint contracts")
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self._git("init")
        self._git("config", "user.email", "contract@example.invalid")
        self._git("config", "user.name", "Contract Test")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "initial")

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        process = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {process.stderr}")

    def key(self, query: str = "Find Symbol", budget: int = 650) -> str:
        return CONTEXT.cache_key(
            "code_symbol",
            "simone",
            query,
            str(self.repo),
            budget,
        )

    def test_dirty_status_changes_cache_key(self) -> None:
        clean = self.key()
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertNotEqual(clean, self.key())

    def test_git_head_changes_cache_key(self) -> None:
        first = self.key()
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "second")
        self.assertNotEqual(first, self.key())

    def test_token_budget_changes_cache_key(self) -> None:
        self.assertNotEqual(self.key(budget=350), self.key(budget=650))

    def test_equivalent_whitespace_and_case_share_key(self) -> None:
        self.assertEqual(
            self.key(query="  Find   Symbol  "),
            self.key(query="find symbol"),
        )


if __name__ == "__main__":
    unittest.main()
