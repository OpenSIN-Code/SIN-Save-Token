#!/usr/bin/env python3
"""Static contracts for canonical same-worktree Orca delegation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SameWorktreeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(
            (ROOT / "config" / "orca-orchestrator.json").read_text(
                encoding="utf-8"
            )
        )
        cls.dispatch = (ROOT / "lib" / "sin_orca" / "dispatch.py").read_text(
            encoding="utf-8"
        )
        cls.cli = (ROOT / "lib" / "sin_orca" / "cli.py").read_text(
            encoding="utf-8"
        )
        cls.review = (ROOT / "lib" / "sin_orca" / "review.py").read_text(
            encoding="utf-8"
        )
        cls.smoke = (ROOT / "scripts" / "live-orca-smoke.py").read_text(
            encoding="utf-8"
        )

    def test_policy_requires_same_worktree_terminal_callbacks(self) -> None:
        runtime = self.policy["team_runtime"]
        self.assertEqual(runtime["workspace_mode"], "same-worktree")
        self.assertEqual(runtime["spawn_primitive"], "orca-terminal-create")
        self.assertTrue(runtime["forbid_worker_worktrees"])
        self.assertEqual(runtime["callback_transport"], "parent-terminal")
        self.assertTrue(runtime["require_parent_terminal_handle"])
        self.assertTrue(runtime["forbid_sleep_wait"])
        self.assertEqual(
            runtime["artifact_root"],
            ".sin-worker/tasks/{task_id}/outbox",
        )

    def test_policy_serializes_repository_writers(self) -> None:
        runtime = self.policy["team_runtime"]
        self.assertEqual(
            runtime["parallel_write_policy"],
            "exclusive-repository-writer",
        )
        self.assertEqual(runtime["maximum_parallel_editors"], 1)
        self.assertEqual(
            runtime["baseline_ref_root"],
            "refs/sin-orca/baselines/{task_id}",
        )

    def test_runtime_never_calls_orca_worktree_create(self) -> None:
        self.assertNotIn('"worktree", "create"', self.dispatch)
        self.assertIn('"terminal",\n            "create"', self.dispatch)
        self.assertIn('selector = f"path:{root}"', self.dispatch)
        self.assertNotIn("--setup", self.dispatch)
        self.assertIn("continuous-preauthorized", self.dispatch)
        self.assertIn("--approval-mode", self.cli)

    def test_worker_prompt_requires_direct_callbacks(self) -> None:
        self.assertIn("sin-orca notify", self.dispatch)
        self.assertIn("Do not use sleep or polling", self.dispatch)
        self.assertIn(
            "Continue automatically after a healthy checkpoint",
            self.dispatch,
        )
        self.assertIn('root / ".sin-worker" / "tasks"', self.dispatch)
        self.assertIn("parent_terminal_handle", self.dispatch)

    def test_cli_exposes_notify_cancel_and_writer_status(self) -> None:
        self.assertIn('sub.add_parser("notify"', self.cli)
        self.assertIn('sub.add_parser("cancel"', self.cli)
        self.assertIn("writer_reservation", self.cli)
        self.assertIn("actor={args.actor}", self.cli)

    def test_reviewer_is_new_terminal_same_repository(self) -> None:
        self.assertIn("worker was not dispatched in same-worktree mode", self.review)
        self.assertIn("worker selector does not match task repository", self.review)
        self.assertIn("task does not own repository writer before review", self.review)
        self.assertIn('"terminal",\n            "create"', self.review)
        self.assertTrue(self.policy["review"]["different_agent"])
        self.assertIn("select_reviewer_agent", self.review)

    def test_live_smoke_proves_no_extra_git_worktree(self) -> None:
        self.assertIn('"worktree", "list", "--porcelain"', self.smoke)
        self.assertIn("worker changed repository HEAD", self.smoke)
        self.assertIn("required_callbacks", self.smoke)
        self.assertIn("same-worktree mode", self.smoke)


if __name__ == "__main__":
    result = unittest.main(exit=False)
    if not result.result.wasSuccessful():
        raise SystemExit(1)
