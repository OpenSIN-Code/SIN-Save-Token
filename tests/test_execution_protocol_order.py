#!/usr/bin/env python3
"""Hermetic event-order contracts for stepwise Orca execution."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from sin_orca.gates import execution_protocol_errors  # noqa: E402
from sin_orca.state import append_event, save_task  # noqa: E402


class ExecutionProtocolOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.patch = patch(
            "sin_orca.state.state_root",
            lambda *args, **kwargs: self.state,
        )
        self.patch.start()
        self.task_id = "order-task-001"
        self.task = {
            "task_id": self.task_id,
            "task_hash": "sha256:order",
            "base_sha": "a" * 40,
            "repository_root": "/tmp/order-repository",
            "role": "implementer",
            "objective": "prove event order",
            "steps": [
                {"id": "S01", "instruction": "first"},
                {"id": "S02", "instruction": "second"},
            ],
            "required_checkpoints": ["first-ready", "second-ready"],
            "allowed_paths": ["README.md"],
            "forbidden_paths": [],
            "acceptance_criteria": [{"id": "AC01", "text": "ordered"}],
        }
        save_task(self.task)
        append_event(
            self.task_id,
            "task.created",
            {
                "task_hash": self.task["task_hash"],
                "base_sha": self.task["base_sha"],
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            self.task_id,
            "worker.spawned",
            {
                "agent": "mimo-code",
                "terminal_handle": "worker-terminal",
                "worktree_path": "/tmp/order-repository",
            },
            actor="worker",
        )

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def callback(
        self,
        callback_type: str,
        *,
        step_id: str | None = None,
    ) -> None:
        append_event(
            self.task_id,
            "worker.callback",
            {
                "callback_type": callback_type,
                "step_id": step_id,
                "summary": callback_type,
                "changed_files": [],
                "verification_status": "not-run",
                "requested_action": "none",
            },
            actor="worker",
        )

    def checkpoint(self, sequence: int, name: str, step_id: str) -> None:
        append_event(
            self.task_id,
            "checkpoint.received",
            {
                "checkpoint": name,
                "sequence": sequence,
                "step_id": step_id,
                "status": "ready",
                "changed_files": [],
                "commands": [],
                "unresolved": [],
                "child_process_running": False,
            },
            actor="worker",
        )

    def approve(self, step_id: str) -> None:
        append_event(
            self.task_id,
            "codex.approved",
            {"step_id": step_id, "instruction": "continue"},
            actor="codex",
        )

    def valid_steps(self) -> None:
        self.callback("ack")
        self.checkpoint(1, "first-ready", "S01")
        self.callback("checkpoint", step_id="S01")
        self.approve("S01")
        self.checkpoint(2, "second-ready", "S02")
        self.callback("checkpoint", step_id="S02")
        self.approve("S02")

    def report(self) -> None:
        append_event(
            self.task_id,
            "worker.report.received",
            {
                "task_id": self.task_id,
                "task_hash": self.task["task_hash"],
                "base_sha": self.task["base_sha"],
                "status": "complete",
                "changed_files": ["README.md"],
                "evidence": ["README.md"],
                "commands": [],
                "unresolved": [],
                "scope_compliance": {
                    "outside_allowlist_touched": False,
                    "unrequested_dependencies_added": False,
                    "architecture_decisions_made": False,
                },
            },
            actor="worker",
        )

    def verify(self) -> None:
        append_event(
            self.task_id,
            "verification.completed",
            {
                "ok": True,
                "changed_files": ["README.md"],
                "results": [{"ok": True}],
                "diff_sha256": "d" * 64,
            },
            actor="controller",
        )

    def test_valid_two_step_window_is_accepted(self) -> None:
        self.valid_steps()
        self.assertEqual(execution_protocol_errors(self.task_id), [])

    def test_checkpoint_callback_after_approval_is_rejected(self) -> None:
        self.callback("ack")
        self.checkpoint(1, "first-ready", "S01")
        self.approve("S01")
        self.callback("checkpoint", step_id="S01")
        self.checkpoint(2, "second-ready", "S02")
        self.callback("checkpoint", step_id="S02")
        self.approve("S02")

        errors = execution_protocol_errors(self.task_id)
        self.assertIn(
            "S01 approval must follow its checkpoint callback",
            errors,
        )

    def test_checkpoint_callback_before_artifact_is_rejected(self) -> None:
        self.callback("ack")
        self.callback("checkpoint", step_id="S01")
        self.checkpoint(1, "first-ready", "S01")
        self.approve("S01")
        self.checkpoint(2, "second-ready", "S02")
        self.callback("checkpoint", step_id="S02")
        self.approve("S02")

        self.assertIn(
            "S01 checkpoint callback must follow its artifact",
            execution_protocol_errors(self.task_id),
        )

    def test_second_checkpoint_before_first_approval_is_rejected(self) -> None:
        self.callback("ack")
        self.checkpoint(1, "first-ready", "S01")
        self.callback("checkpoint", step_id="S01")
        self.checkpoint(2, "second-ready", "S02")
        self.callback("checkpoint", step_id="S02")
        self.approve("S01")
        self.approve("S02")

        errors = execution_protocol_errors(self.task_id)
        self.assertTrue(
            any(
                error.startswith("S02 checkpoint")
                and "out of step order" in error
                for error in errors
            )
        )

    def test_duplicate_ack_is_rejected(self) -> None:
        self.callback("ack")
        self.callback("ack")
        self.checkpoint(1, "first-ready", "S01")
        self.callback("checkpoint", step_id="S01")
        self.approve("S01")
        self.checkpoint(2, "second-ready", "S02")
        self.callback("checkpoint", step_id="S02")
        self.approve("S02")

        self.assertIn(
            "worker direct ack callback is duplicated",
            execution_protocol_errors(self.task_id),
        )

    def test_report_and_done_before_verification_are_accepted(self) -> None:
        self.valid_steps()
        self.report()
        self.callback("done")
        self.verify()
        self.assertEqual(execution_protocol_errors(self.task_id), [])

    def test_done_after_verification_is_rejected(self) -> None:
        self.valid_steps()
        self.report()
        self.verify()
        self.callback("done")

        self.assertIn(
            "worker done callback arrived after controller verification",
            execution_protocol_errors(self.task_id),
        )


if __name__ == "__main__":
    unittest.main()
