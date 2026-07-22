#!/usr/bin/env python3
"""
Unit tests für sin-orca v2.
5 Tests: Event-Log, Ledger, Scope-Check, Artifacts, Checkpoint-Statemachine.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import importlib.util
_orca_path = Path(__file__).resolve().parent.parent / "bin" / "sin-orca"
spec = importlib.util.spec_from_loader("sin_orca", loader=None, origin=str(_orca_path))
# .py extension needed for spec_from_file_location; use exec approach
_code = _orca_path.read_text()
sin_orca = type(sys)("sin_orca")
sin_orca.__file__ = str(_orca_path)
exec(compile(_code, str(_orca_path), "exec"), sin_orca.__dict__)


class TestEventLogAppendAndHash(unittest.TestCase):
    """Test 1: Event-Log append + SHA256-Integrität."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(sin_orca, "state_root", lambda: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_append_creates_valid_event_chain(self):
        task_id = "test-chain-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)

        sin_orca.append_event(task_id, "task.created", {"role": "test"})
        sin_orca.append_event(task_id, "task.dispatched", {"worker": "mimo"})
        sin_orca.append_event(task_id, "worker.checkpoint", {"checkpoint": "plan-ready"})

        events = sin_orca.read_events(task_id)
        self.assertEqual(len(events), 3)

        self.assertEqual(events[0]["sequence"], 1)
        self.assertEqual(events[0]["type"], "task.created")
        self.assertEqual(events[0]["previous_hash"], sin_orca.ZERO_HASH)

        self.assertEqual(events[1]["sequence"], 2)
        self.assertEqual(events[1]["previous_hash"], events[0]["event_hash"])

        self.assertEqual(events[2]["sequence"], 3)
        self.assertEqual(events[2]["previous_hash"], events[1]["event_hash"])

        for event in events:
            material = {
                "sequence": event["sequence"],
                "type": event["type"],
                "timestamp": event["timestamp"],
                "payload": event["payload"],
                "previous_hash": event["previous_hash"],
            }
            expected_hash = sin_orca.sha256_text(
                json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
            self.assertEqual(event["event_hash"], expected_hash)


class TestLedgerReconstruction(unittest.TestCase):
    """Test 2: Ledger fully reconstructable from events."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(sin_orca, "state_root", lambda: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_ledger_reconstructs_from_events(self):
        task_id = "test-ledger-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)

        sin_orca.append_event(task_id, "task.created", {"role": "impl", "objective": "fix bug"})
        sin_orca.append_event(task_id, "task.dispatched", {"worker": "mimo", "worktree_path": "/tmp/wt"})
        sin_orca.append_event(task_id, "worker.checkpoint", {"checkpoint": "plan-ready"})
        sin_orca.append_event(task_id, "codex.approved", {"step_id": "s1", "instruction": "go"})
        sin_orca.append_event(task_id, "worker.checkpoint", {"checkpoint": "implement-done"})
        sin_orca.append_event(task_id, "controller.verification", {"ok": True, "results": []})

        ledger = sin_orca.current_ledger(task_id)

        self.assertEqual(ledger["status"], "verification-complete")
        self.assertEqual(ledger["events_count"], 6)
        self.assertEqual(ledger["task"]["role"], "impl")
        self.assertEqual(ledger["worker"]["worktree_path"], "/tmp/wt")
        self.assertTrue(ledger["verification"]["ok"])

    def test_ledger_idempotent_rebuild(self):
        task_id = "test-rebuild-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)

        sin_orca.append_event(task_id, "task.created", {"role": "test"})
        sin_orca.append_event(task_id, "task.dispatched", {"worker": "mimo"})

        ledger1 = sin_orca.current_ledger(task_id)
        events = sin_orca.read_events(task_id)
        ledger2 = sin_orca.reduce_events(task_id, events)

        self.assertEqual(ledger1, ledger2)


class TestScopeCheck(unittest.TestCase):
    """Test 3: actual_changed_files uses git, not worker report."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.worktree = Path(self.tmpdir) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=self.worktree, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.worktree, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.worktree, capture_output=True)

        (self.worktree / "allowed.txt").write_text("allowed content")
        (self.worktree / ".sin-worker").mkdir()
        (self.worktree / ".sin-worker" / "secret.txt").write_text("should be excluded")

        subprocess.run(["git", "add", "allowed.txt"], cwd=self.worktree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.worktree, capture_output=True)

        (self.worktree / "new_file.txt").write_text("new content")
        (self.worktree / ".sin-worker" / "outbox").mkdir(parents=True)
        (self.worktree / ".sin-worker" / "outbox" / "checkpoint.json").write_text("{}")

    def test_excludes_sin_worker(self):
        task = {"base_sha": "HEAD"}
        ledger = {"worker": {"worktree_path": str(self.worktree)}}

        changed = sin_orca.actual_changed_files(task, ledger)

        self.assertIn("new_file.txt", changed)
        self.assertFalse(
            any(".sin-worker" in f for f in changed),
            f".sin-worker files should be excluded: {changed}",
        )


class TestArtifactConsumption(unittest.TestCase):
    """Test 4: consume_artifact reads + deletes artifact file."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(sin_orca, "state_root", lambda: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_consume_artifact_reads_and_deletes(self):
        task_id = "test-artifact-001"
        worktree = Path(tempfile.mkdtemp()) / "worktree"
        outbox = worktree / ".sin-worker" / "outbox"
        outbox.mkdir(parents=True)

        checkpoint_data = {"checkpoint": "plan-ready", "summary": "Plan erstellt"}
        (outbox / "checkpoint.json").write_text(json.dumps(checkpoint_data))

        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        sin_orca.append_event(
            task_id,
            "task.dispatched",
            {"worker": "mimo", "worktree_path": str(worktree)},
        )

        result = sin_orca.consume_artifact(task_id, "worker", "checkpoint.json")

        self.assertEqual(result["artifact"], "checkpoint.json")
        self.assertEqual(result["checkpoint"], "plan-ready")
        self.assertFalse((outbox / "checkpoint.json").exists())

    def test_consume_missing_artifact_returns_error(self):
        task_id = "test-artifact-002"
        worktree = Path(tempfile.mkdtemp()) / "worktree2"
        outbox = worktree / ".sin-worker" / "outbox"
        outbox.mkdir(parents=True)

        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        sin_orca.append_event(
            task_id,
            "task.dispatched",
            {"worker": "mimo", "worktree_path": str(worktree)},
        )

        result = sin_orca.consume_artifact(task_id, "worker", "nonexistent.json")
        self.assertEqual(result["error"], "not_found")


class TestCheckpointStateMachine(unittest.TestCase):
    """Test 5: Checkpoint state machine transitions."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(sin_orca, "state_root", lambda: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_full_state_machine(self):
        task_id = "test-statemachine-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)

        sin_orca.append_event(task_id, "task.created", {"role": "test"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "created")

        sin_orca.append_event(task_id, "task.dispatched", {"worker": "mimo"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "dispatched")

        sin_orca.append_event(task_id, "worker.checkpoint", {"checkpoint": "plan-ready"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "checkpoint:plan-ready")

        sin_orca.append_event(task_id, "codex.approved", {"step_id": "s1", "instruction": "go"})
        sin_orca.append_event(task_id, "worker.checkpoint", {"checkpoint": "implement-done"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "checkpoint:implement-done")

        sin_orca.append_event(task_id, "worker.report", {"summary": "done"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "report-received")

        sin_orca.append_event(task_id, "controller.verification", {"ok": True})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "verification-complete")

        sin_orca.append_event(task_id, "reviewer.verdict", {"verdict": "accept"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "review-received")

        sin_orca.append_event(task_id, "task.completed", {})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "completed")

    def test_suspend_resume_flow(self):
        task_id = "test-suspend-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)

        sin_orca.append_event(task_id, "task.created", {"role": "test"})
        sin_orca.append_event(task_id, "task.dispatched", {"worker": "mimo"})
        sin_orca.append_event(task_id, "worker.checkpoint", {"checkpoint": "plan-ready"})

        sin_orca.append_event(task_id, "codex.suspended", {"reason": "context limit"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "suspended")

        sin_orca.append_event(task_id, "codex.resumed", {"instruction": "continue"})
        ledger = sin_orca.current_ledger(task_id)
        self.assertEqual(ledger["status"], "resumed")


class TestRepeatedFailureDetection(unittest.TestCase):
    """Test: repeated_failure_policy erkennt identische Fehler."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(sin_orca, "state_root", lambda: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_no_failure(self):
        task_id = "test-fail-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"repeated_failure_policy": {"maximum_identical_failures_without_codex_intervention": 2}}}

        result = sin_orca.check_repeated_failures(task_id, config)
        self.assertFalse(result["blocked"])

    def test_different_failures_not_blocked(self):
        task_id = "test-fail-002"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"repeated_failure_policy": {"maximum_identical_failures_without_codex_intervention": 2}}}

        sin_orca.track_command_result(task_id, "npm test", 1, "hash_a")
        sin_orca.track_command_result(task_id, "npm run lint", 1, "hash_b")

        result = sin_orca.check_repeated_failures(task_id, config)
        self.assertFalse(result["blocked"])

    def test_identical_failures_blocked(self):
        task_id = "test-fail-003"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"repeated_failure_policy": {"maximum_identical_failures_without_codex_intervention": 2}}}

        sin_orca.track_command_result(task_id, "npm test", 1, "hash_x")
        sin_orca.track_command_result(task_id, "npm test", 1, "hash_x")

        result = sin_orca.check_repeated_failures(task_id, config)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "repeated_failure")
        self.assertTrue(result["requires_codex_intervention"])

    def test_success_resets_counter(self):
        task_id = "test-fail-004"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"repeated_failure_policy": {"maximum_identical_failures_without_codex_intervention": 2}}}

        sin_orca.track_command_result(task_id, "npm test", 1, "hash_x")
        sin_orca.track_command_result(task_id, "npm test", 0, "")
        sin_orca.track_command_result(task_id, "npm test", 1, "hash_x")

        result = sin_orca.check_repeated_failures(task_id, config)
        self.assertFalse(result["blocked"])


class TestStalledWorkerDetection(unittest.TestCase):
    """Test: stalled_worker_policy erkennt Inaktivität."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(sin_orca, "state_root", lambda: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_active_worker_not_stalled(self):
        task_id = "test-stall-001"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"stalled_worker_policy": {"enabled": True, "maximum_inactive_seconds": 1200, "ignore_while_child_process_running": True}}}

        sin_orca.update_activity_timestamp(task_id, "worker")

        result = sin_orca.check_stalled_worker(task_id, "worker", config)
        self.assertFalse(result["stalled"])

    def test_disabled_policy(self):
        task_id = "test-stall-002"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"stalled_worker_policy": {"enabled": False, "maximum_inactive_seconds": 0}}}

        result = sin_orca.check_stalled_worker(task_id, "worker", config)
        self.assertFalse(result["stalled"])

    def test_stalled_worker_detected(self):
        task_id = "test-stall-003"
        sin_orca.task_directory(task_id).mkdir(parents=True, exist_ok=True)
        config = {"worker_control": {"stalled_worker_policy": {"enabled": True, "maximum_inactive_seconds": 1, "ignore_while_child_process_running": False}}}

        activity_path = sin_orca.task_directory(task_id) / "activity.json"
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        with open(activity_path, "w") as f:
            json.dump({"worker_last_active": old_time, "worker_last_activity_type": "poll"}, f)

        result = sin_orca.check_stalled_worker(task_id, "worker", config)
        self.assertTrue(result["stalled"])
        self.assertGreater(result["idle_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
