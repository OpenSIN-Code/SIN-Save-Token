#!/usr/bin/env python3
"""Contracts for the fail-closed GitNexus fallback adapter."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "bin" / "gitnexus-query"


def load_adapter():
    loader = importlib.machinery.SourceFileLoader(
        "gitnexus_query_contract",
        str(ADAPTER_PATH),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {ADAPTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


ADAPTER = load_adapter()


class GitNexusAdapterTests(unittest.TestCase):
    def test_remote_normalization_matches_ssh_and_https(self) -> None:
        self.assertEqual(
            ADAPTER.canonical_remote("git@github.com:OpenSIN-Code/repo.git"),
            ADAPTER.canonical_remote("https://github.com/OpenSIN-Code/repo"),
        )

    def test_exact_repository_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = {
                "name": "repo",
                "path": str(root),
                "remoteUrl": "git@github.com:OpenSIN-Code/repo.git",
            }
            matched = ADAPTER.match_registry_entry(root, [entry])
        self.assertIs(matched, entry)

    def test_worktree_can_match_unique_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = {
                "name": "repo",
                "path": str(root.parent / "canonical-repo"),
                "remoteUrl": "git@github.com:OpenSIN-Code/repo.git",
            }
            with patch.object(
                ADAPTER,
                "run_git",
                return_value="https://github.com/OpenSIN-Code/repo.git",
            ):
                matched = ADAPTER.match_registry_entry(root, [entry])
        self.assertIs(matched, entry)

    def test_ambiguous_remote_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entries = [
                {
                    "name": name,
                    "path": str(root.parent / name),
                    "remoteUrl": "git@github.com:OpenSIN-Code/repo.git",
                }
                for name in ("repo-a", "repo-b")
            ]
            with patch.object(
                ADAPTER,
                "run_git",
                return_value="https://github.com/OpenSIN-Code/repo.git",
            ):
                with self.assertRaisesRegex(RuntimeError, "multiple GitNexus"):
                    ADAPTER.match_registry_entry(root, entries)

    def test_dirty_or_stale_index_is_rejected(self) -> None:
        entry = {"name": "repo", "lastCommit": "a" * 40}
        root = Path("/tmp/repo")
        with patch.object(
            ADAPTER,
            "run_git",
            side_effect=["b" * 40],
        ):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                ADAPTER.validate_fresh_index(root, entry)

        with patch.object(
            ADAPTER,
            "run_git",
            side_effect=["a" * 40, " M src/main.py"],
        ):
            with self.assertRaisesRegex(RuntimeError, "unindexed changes"):
                ADAPTER.validate_fresh_index(root, entry)

    def test_query_is_serialized_and_hides_controller_secret(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gitnexus"],
            0,
            stdout="graph result\n",
            stderr="",
        )
        with patch.dict(
            os.environ,
            {"SIN_MANIFEST_HMAC_KEY": "controller-only"},
        ), patch.object(
            ADAPTER.subprocess,
            "run",
            return_value=completed,
        ) as runner:
            output = ADAPTER.query_gitnexus(
                "/usr/local/bin/gitnexus",
                "auth flow",
                "repo",
                Path("/tmp/repo"),
            )

        self.assertEqual(output, "graph result")
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/usr/local/bin/gitnexus",
                "query",
                "auth flow",
                "--repo",
                "repo",
            ],
        )
        self.assertNotIn(
            "SIN_MANIFEST_HMAC_KEY",
            runner.call_args.kwargs["env"],
        )


if __name__ == "__main__":
    unittest.main()
