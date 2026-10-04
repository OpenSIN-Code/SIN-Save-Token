from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from sin_orca.dispatch import render_worker_prompt
from sin_orca.gates import execution_protocol_errors
from sin_orca.state import append_event, save_task


def test_read_only_no_checkpoints_accepts_ack_report_done(tmp_path):
    task = {"task_id": "optional-checkpoints", "task_hash": "sha256:test",
            "base_sha": "a" * 40, "role": "explorer", "allow_edits": False,
            "steps": [{"id": "S01", "instruction": "audit"}],
            "required_checkpoints": [], "approval_mode": "continuous-preauthorized"}
    with patch("sin_orca.state.state_root", return_value=tmp_path):
        save_task(task)
        append_event(task["task_id"], "task.created", task, actor="codex")
        append_event(task["task_id"], "worker.callback", {"callback_type": "ack"}, actor="worker")
        append_event(task["task_id"], "worker.report.received", {"status": "complete"}, actor="worker")
        append_event(task["task_id"], "worker.callback", {"callback_type": "done"}, actor="worker")
        assert execution_protocol_errors(task["task_id"]) == []
        # Required checkpoints remain mandatory, even for read-only audits.
        task["required_checkpoints"] = ["audit-complete"]
        save_task(task)
        assert "S01 checkpoint artifact missing" in execution_protocol_errors(task["task_id"])
        task["required_checkpoints"] = []
        task["role"] = "implementer"
        task["allow_edits"] = True
        save_task(task)
        assert "S01 checkpoint artifact missing" in execution_protocol_errors(task["task_id"])


def test_read_only_prompt_does_not_require_unlisted_checkpoints():
    task = {"task_id": "optional-prompt", "task_hash": "sha256:test", "base_sha": "a" * 40,
            "repository_head_sha": "b" * 40, "repository_root": "/tmp/public",
            "worktree_selector": "path:/tmp/public", "parent_terminal_handle": "parent",
            "role": "explorer", "allow_edits": False, "objective": "audit",
            "allowed_paths": ["input.json"], "acceptance_criteria": [],
            "steps": [{"id": "S01", "instruction": "audit"}],
            "required_checkpoints": [], "approval_mode": "continuous-preauthorized",
            "artifact_outbox": ".sin-worker/outbox"}
    prompt = render_worker_prompt(task, worker_terminal="worker")
    assert "No checkpoints are required" in prompt
    assert "After each step, atomically write its required checkpoint" not in prompt


import tempfile
import unittest


class OptionalCheckpointTests(unittest.TestCase):
    def test_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            test_read_only_no_checkpoints_accepts_ack_report_done(Path(directory))

    def test_prompt(self):
        test_read_only_prompt_does_not_require_unlisted_checkpoints()

    def test_stepwise_report_must_follow_approval(self):
        with tempfile.TemporaryDirectory() as directory, patch("sin_orca.state.state_root", return_value=Path(directory)):
            task = {"task_id": "stepwise-optional", "task_hash": "sha256:test",
                    "base_sha": "a" * 40, "role": "reviewer", "allow_edits": False,
                    "steps": [{"id": "S01", "instruction": "review"}],
                    "required_checkpoints": [], "approval_mode": "stepwise"}
            save_task(task)
            append_event(task["task_id"], "task.created", task, actor="codex")
            append_event(task["task_id"], "worker.callback", {"callback_type": "ack"}, actor="worker")
            append_event(task["task_id"], "worker.report.received", {"status": "complete"}, actor="worker")
            append_event(task["task_id"], "codex.approved", {"step_id": "S01"}, actor="codex")
            append_event(task["task_id"], "worker.callback", {"callback_type": "done"}, actor="worker")
            assert "worker report arrived before the final protocol boundary" in execution_protocol_errors(task["task_id"])

    def test_editable_explorer_does_not_bypass_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory, patch("sin_orca.state.state_root", return_value=Path(directory)):
            task = {"task_id": "editable-explorer", "task_hash": "sha256:test",
                    "base_sha": "a" * 40, "role": "explorer", "allow_edits": True,
                    "steps": [{"id": "S01", "instruction": "review"}],
                    "required_checkpoints": [], "approval_mode": "continuous-preauthorized"}
            save_task(task)
            append_event(task["task_id"], "task.created", task, actor="codex")
            append_event(task["task_id"], "worker.callback", {"callback_type": "ack"}, actor="worker")
            assert "S01 checkpoint artifact missing" in execution_protocol_errors(task["task_id"])
