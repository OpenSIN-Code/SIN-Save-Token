#!/usr/bin/env python3
"""
Unit tests for sin_orca package — state, events, scope, artifacts, lease.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sin_orca.state import (
    append_event,
    atomic_write_json,
    events_path,
    load_task,
    read_events,
    rebuild_ledger,
    save_task,
    sha256_json,
    state_root,
    task_dir,
    ZERO_HASH,
)
from sin_orca.verification import (
    actual_changed_files,
    path_allowed,
    validate_scope,
)
from sin_orca.lease import (
    ControllerLease,
    LeaseConflictError,
    LeaseLostError,
    controller_identity,
)
from sin_orca.artifacts import (
    ArtifactValidationError,
    ingest_artifact,
)


class TestEventLogAppendAndHash(unittest.TestCase):
    """Test 1: Event-Log append + SHA256 integrity."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch("sin_orca.state.state_root", lambda *a, **k: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_append_creates_valid_event_chain(self):
        task_id = "test-chain-001"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
        })

        append_event(task_id, "task.created", {"task_hash": "sha256:abc", "base_sha": "a" * 40, "role": "test"}, actor="codex")
        append_event(task_id, "worker.spawned", {"agent": "mimo", "terminal_handle": "t1", "worktree_path": "/tmp"}, actor="worker")
        append_event(task_id, "checkpoint.received", {"checkpoint": "plan-ready"}, actor="worker")

        events = read_events(task_id)
        self.assertEqual(len(events), 3)

        self.assertEqual(events[0]["previous_hash"], ZERO_HASH)
        self.assertEqual(events[1]["previous_hash"], events[0]["event_hash"])
        self.assertEqual(events[2]["previous_hash"], events[1]["event_hash"])

        for i, event in enumerate(events, 1):
            self.assertEqual(event["sequence"], i)

    def test_event_hash_tampering_blocks_rebuild(self):
        task_id = "test-tamper-001"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
        })

        append_event(task_id, "task.created", {"task_hash": "sha256:abc", "base_sha": "a" * 40, "role": "test"}, actor="codex")

        path = events_path(task_id)
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"] = {"tampered": True}
        lines[0] = json.dumps(event)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            read_events(task_id, verify=True)


class TestLedgerReconstruction(unittest.TestCase):
    """Test 2: Ledger reconstruction from events."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch("sin_orca.state.state_root", lambda *a, **k: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_ledger_reconstructs_from_events(self):
        task_id = "test-ledger-001"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "role": "implementer",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
        })

        append_event(task_id, "task.created", {"task_hash": "sha256:abc", "base_sha": "a" * 40, "role": "implementer"}, actor="codex")
        append_event(task_id, "worker.spawned", {"agent": "mimo-code", "terminal_handle": "t1"}, actor="worker")

        ledger = rebuild_ledger(task_id)
        self.assertEqual(ledger["status"], "awaiting-ack")
        self.assertIn("worker", ledger["actors"])

    def test_ledger_idempotent_rebuild(self):
        task_id = "test-ledger-002"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
        })

        append_event(task_id, "task.created", {"task_hash": "sha256:abc", "base_sha": "a" * 40, "role": "test"}, actor="codex")

        first = rebuild_ledger(task_id)
        second = rebuild_ledger(task_id)
        self.assertEqual(first, second)


class TestCliParsing(unittest.TestCase):
    def test_verify_command_does_not_replace_subcommand(self):
        from sin_orca.cli import main

        argv = [
            "sin-orca",
            "verify",
            "task-001",
            "--command",
            "pytest -q",
        ]
        with patch.object(sys, "argv", argv), patch(
            "sin_orca.cli._cmd_verify",
            return_value=0,
        ) as handler:
            result = main()

        self.assertEqual(result, 0)
        parsed = handler.call_args.args[0]
        self.assertEqual(parsed.command, "verify")
        self.assertEqual(parsed.verification_command, "pytest -q")


class TestCrossRepositoryTaskLookup(unittest.TestCase):
    def test_task_is_found_outside_dispatch_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "state"
            repo_a = Path(temporary) / "repo-a"
            repo_b = Path(temporary) / "repo-b"
            repo_a.mkdir()
            repo_b.mkdir()

            with patch.dict(
                os.environ,
                {"SIN_ORCA_STATE_ROOT": str(base)},
            ):
                save_task(
                    {
                        "task_id": "cross-repo-task",
                        "task_hash": "sha256:cross",
                        "base_sha": "a" * 40,
                    },
                    root=repo_a,
                )
                with patch(
                    "sin_orca.state.repository_root",
                    return_value=repo_b,
                ):
                    loaded = load_task("cross-repo-task")

            self.assertEqual(loaded["task_hash"], "sha256:cross")

    def test_ambiguous_task_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "state"
            repos = [
                Path(temporary) / name
                for name in ("repo-a", "repo-b", "repo-c")
            ]
            for repository in repos:
                repository.mkdir()

            with patch.dict(
                os.environ,
                {"SIN_ORCA_STATE_ROOT": str(base)},
            ):
                for repository in (repos[0], repos[1]):
                    save_task(
                        {
                            "task_id": "ambiguous-task",
                            "task_hash": "sha256:ambiguous",
                            "base_sha": "a" * 40,
                        },
                        root=repository,
                    )
                with patch(
                    "sin_orca.state.repository_root",
                    return_value=repos[2],
                ):
                    with self.assertRaises(RuntimeError):
                        load_task("ambiguous-task")


class TestScopeCheck(unittest.TestCase):
    """Test 3: Scope validation."""

    def test_excludes_sin_worker(self):
        files = [".sin-worker/outbox/checkpoint.json", "src/main.py", "README.md"]
        filtered = [f for f in files if not f.startswith(".sin-worker/")]
        self.assertEqual(filtered, ["src/main.py", "README.md"])

    def test_path_allowed_exact(self):
        self.assertTrue(path_allowed("src/main.py", ["src/main.py"]))

    def test_path_allowed_prefix(self):
        self.assertTrue(path_allowed("src/sub/deep.py", ["src"]))

    def test_path_not_allowed(self):
        self.assertFalse(path_allowed("src/secret.py", ["docs"]))

    def test_forbidden_path_caught(self):
        errors = validate_scope(
            changed_files=["src/main.py", "config/secrets.env"],
            allowed_paths=["src", "config"],
            forbidden_paths=["config/secrets.env"],
            allow_edits=True,
        )
        self.assertTrue(any("forbidden" in e for e in errors))


class TestArtifactValidation(unittest.TestCase):
    """Test 4: Artifact identity and shape validation."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch("sin_orca.state.state_root", lambda *a, **k: self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_artifact_with_wrong_task_hash_is_rejected(self):
        task_id = "test-art-001"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:correct",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
            "required_checkpoints": [],
        })

        outbox = self.tmpdir / "outbox"
        outbox.mkdir(parents=True)
        artifact = outbox / "report.json"
        artifact.write_text(json.dumps({
            "task_id": task_id,
            "task_hash": "sha256:WRONG",
            "base_sha": "a" * 40,
            "status": "complete",
            "changed_files": [],
            "evidence": [],
            "unresolved": [],
        }), encoding="utf-8")

        with self.assertRaises(ArtifactValidationError):
            ingest_artifact(
                task_id=task_id,
                actor="worker",
                outbox=outbox,
                filename="report.json",
            )

    def test_artifact_is_not_deleted_before_archival(self):
        task_id = "test-art-002"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
            "required_checkpoints": [],
        })

        outbox = self.tmpdir / "outbox2"
        outbox.mkdir(parents=True)
        artifact = outbox / "report.json"
        artifact.write_text(json.dumps({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "status": "complete",
            "changed_files": [],
            "evidence": [],
            "unresolved": [],
        }), encoding="utf-8")

        result = ingest_artifact(
            task_id=task_id,
            actor="worker",
            outbox=outbox,
            filename="report.json",
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["duplicate"])
        self.assertIn("archive_path", result)
        self.assertTrue(Path(result["archive_path"]).is_file())
        self.assertFalse(artifact.exists())

    def test_duplicate_artifact_is_idempotent(self):
        task_id = "test-art-003"
        task_dir(task_id).mkdir(parents=True, exist_ok=True)

        save_task({
            "task_id": task_id,
            "task_hash": "sha256:abc",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
            "required_checkpoints": [],
        })

        outbox = self.tmpdir / "outbox3"
        outbox.mkdir(parents=True)

        for i in range(2):
            artifact = outbox / "report.json"
            artifact.write_text(json.dumps({
                "task_id": task_id,
                "task_hash": "sha256:abc",
                "base_sha": "a" * 40,
                "status": "complete",
                "changed_files": [],
                "evidence": [],
                "unresolved": [],
            }), encoding="utf-8")

            result = ingest_artifact(
                task_id=task_id,
                actor="worker",
                outbox=outbox,
                filename="report.json",
            )

            if i == 0:
                self.assertTrue(result["ok"])
                self.assertFalse(result["duplicate"])
            else:
                self.assertTrue(result["ok"])
                self.assertTrue(result["duplicate"])


class TestControllerLease(unittest.TestCase):
    """Test 5: Controller lease exclusivity."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def test_second_controller_cannot_mutate_live_task(self):
        lease1 = ControllerLease(self.tmpdir, owner="controller-A")
        lease_a = lease1.acquire(ttl_seconds=300)

        lease2 = ControllerLease(self.tmpdir, owner="controller-B")

        with self.assertRaises(LeaseConflictError):
            lease2.acquire(ttl_seconds=300)

        lease1.release(lease_a.token)

    def test_expired_controller_lease_can_be_recovered(self):
        import json
        import time

        lease1 = ControllerLease(self.tmpdir, owner="controller-A")
        lease_a = lease1.acquire(ttl_seconds=30)

        # Manually expire the lease by writing an expired timestamp
        lease_data = json.loads(
            (self.tmpdir / "controller-lease.json").read_text()
        )
        lease_data["expires_at"] = time.time() - 1
        (self.tmpdir / "controller-lease.json").write_text(
            json.dumps(lease_data) + "\n"
        )

        lease2 = ControllerLease(self.tmpdir, owner="controller-B")
        lease_b = lease2.acquire(ttl_seconds=300)

        self.assertEqual(lease_b.owner, "controller-B")

    def test_renew_extends_lease(self):
        lease1 = ControllerLease(self.tmpdir, owner="controller-A")
        lease_a = lease1.acquire(ttl_seconds=60)

        renewed = lease1.renew(lease_a.token, ttl_seconds=120)

        self.assertGreater(renewed.expires_at, lease_a.expires_at)


if __name__ == "__main__":
    unittest.main()
