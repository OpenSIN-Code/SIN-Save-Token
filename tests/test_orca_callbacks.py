#!/usr/bin/env python3
"""Direct Orca parent-callback contracts."""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from sin_orca.cli import _cmd_notify  # noqa: E402
from sin_orca.state import append_event, rebuild_ledger, save_task  # noqa: E402


class OrcaCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.task_id = "callback-task-001"
        self.outbox = (
            self.repository
            / ".sin-worker"
            / "tasks"
            / self.task_id
            / "outbox"
        )
        self.outbox.mkdir(parents=True)
        self.state_patch = patch(
            "sin_orca.state.state_root",
            lambda *args, **kwargs: self.state,
        )
        self.state_patch.start()
        save_task({
            "task_id": self.task_id,
            "task_hash": "sha256:callback",
            "repository_root": str(self.repository),
            "base_sha": "a" * 40,
            "parent_terminal_handle": "parent-terminal",
            "artifact_outbox": str(self.outbox.relative_to(self.repository)),
            "role": "implementer",
            "steps": [{"id": "S01", "instruction": "test callback"}],
        })
        append_event(
            self.task_id,
            "task.created",
            {
                "task_hash": "sha256:callback",
                "base_sha": "a" * 40,
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
                "parent_terminal_handle": "parent-terminal",
                "worktree_path": str(self.repository),
                "worktree_selector": f"path:{self.repository}",
                "same_worktree": True,
                "outbox_path": str(self.outbox),
            },
            actor="worker",
        )

    def tearDown(self) -> None:
        self.state_patch.stop()
        self.temporary.cleanup()

    def args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "task_id": self.task_id,
            "type": "ack",
            "summary": "scope understood",
            "step": None,
            "changed": ["none"],
            "verify": "not-run",
            "action": "none",
            "actor": "worker",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_ack_is_sent_to_parent_and_recorded(self) -> None:
        with patch(
            "sin_orca.cli.run_orca",
            return_value={"ok": True},
        ) as sender:
            result = _cmd_notify(self.args())

        self.assertEqual(result, 0)
        argv = sender.call_args.args[0]
        self.assertEqual(argv[argv.index("--terminal") + 1], "parent-terminal")
        message = argv[argv.index("--text") + 1]
        self.assertIn(
            f"SIN_CALLBACK task={self.task_id} actor=worker type=ack",
            message,
        )
        callback = rebuild_ledger(self.task_id)["callbacks"][-1]
        self.assertEqual(callback["callback_type"], "ack")
        self.assertEqual(callback["changed_files"], [])

    def test_done_requires_report_artifact(self) -> None:
        with patch("sin_orca.cli.run_orca", return_value={"ok": True}):
            with self.assertRaisesRegex(RuntimeError, "requires existing artifact"):
                _cmd_notify(self.args(type="done"))

        (self.outbox / "report.json").write_text("{}\n", encoding="utf-8")
        with patch("sin_orca.cli.run_orca", return_value={"ok": True}):
            self.assertEqual(_cmd_notify(self.args(type="done")), 0)

    def test_checkpoint_requires_checkpoint_artifact(self) -> None:
        with patch("sin_orca.cli.run_orca", return_value={"ok": True}):
            with self.assertRaisesRegex(RuntimeError, "requires existing artifact"):
                _cmd_notify(self.args(type="checkpoint", step="S01"))

        (self.outbox / "checkpoint.json").write_text("{}\n", encoding="utf-8")
        with patch("sin_orca.cli.run_orca", return_value={"ok": True}):
            self.assertEqual(
                _cmd_notify(self.args(type="checkpoint", step="S01")),
                0,
            )

    def test_checkpoint_requires_valid_step_id(self) -> None:
        (self.outbox / "checkpoint.json").write_text("{}\n", encoding="utf-8")
        with patch("sin_orca.cli.run_orca", return_value={"ok": True}):
            with self.assertRaisesRegex(ValueError, "valid task step ID"):
                _cmd_notify(self.args(type="checkpoint", step="S99"))

    def test_callback_redacts_secrets(self) -> None:
        with patch(
            "sin_orca.cli.run_orca",
            return_value={"ok": True},
        ) as sender:
            result = _cmd_notify(self.args(
                summary="Authorization: Bearer secret-token",
                verify="password=hunter2",
                action="use https://user:pass@example.invalid/path",
            ))

        self.assertEqual(result, 0)
        message = sender.call_args.args[0][
            sender.call_args.args[0].index("--text") + 1
        ]
        self.assertNotIn("secret-token", message)
        self.assertNotIn("hunter2", message)
        self.assertNotIn("pass@example", message)
        self.assertIn("<redacted>", message)

    def test_callback_cannot_target_actor_terminal(self) -> None:
        task = {
            "task_id": "self-target",
            "task_hash": "sha256:self",
            "repository_root": str(self.repository),
            "base_sha": "b" * 40,
            "parent_terminal_handle": "same-terminal",
            "artifact_outbox": ".sin-worker/tasks/self-target/outbox",
            "role": "implementer",
        }
        save_task(task)
        append_event(
            "self-target",
            "task.created",
            {
                "task_hash": task["task_hash"],
                "base_sha": task["base_sha"],
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            "self-target",
            "worker.spawned",
            {
                "agent": "mimo-code",
                "terminal_handle": "same-terminal",
                "parent_terminal_handle": "same-terminal",
                "worktree_path": str(self.repository),
            },
            actor="worker",
        )
        with self.assertRaisesRegex(RuntimeError, "must differ"):
            _cmd_notify(self.args(task_id="self-target"))


if __name__ == "__main__":
    unittest.main()
