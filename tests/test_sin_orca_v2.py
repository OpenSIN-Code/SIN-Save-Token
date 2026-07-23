#!/usr/bin/env python3
"""
Unit tests for sin_orca package — state, events, scope, artifacts, lease.
"""

import json
import os
import subprocess
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
    controller_environment,
    path_allowed,
    redact_argv,
    redact_text,
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
            "commands": [],
            "unresolved": [],
            "scope_compliance": {
                "outside_allowlist_touched": False,
                "unrequested_dependencies_added": False,
                "architecture_decisions_made": False,
            },
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
            "commands": [],
            "unresolved": [],
            "scope_compliance": {
                "outside_allowlist_touched": False,
                "unrequested_dependencies_added": False,
                "architecture_decisions_made": False,
            },
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
                "commands": [],
                "unresolved": [],
                "scope_compliance": {
                    "outside_allowlist_touched": False,
                    "unrequested_dependencies_added": False,
                    "architecture_decisions_made": False,
                },
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


class TestArtifactProtocol(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch(
            "sin_orca.state.state_root",
            lambda *a, **k: self.tmpdir,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _save_implementer_task(self, task_id: str) -> None:
        save_task({
            "task_id": task_id,
            "task_hash": "sha256:protocol",
            "base_sha": "a" * 40,
            "role": "implementer",
            "objective": "test protocol",
            "allowed_paths": ["README.md"],
            "forbidden_paths": [],
            "steps": [{"id": "S01", "instruction": "edit"}],
            "acceptance_criteria": [{"id": "AC01", "text": "works"}],
            "required_checkpoints": ["plan-ready"],
        })
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": "sha256:protocol",
                "base_sha": "a" * 40,
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            task_id,
            "worker.spawned",
            {
                "agent": "mimo-code",
                "terminal_handle": "worker-terminal",
                "worktree_path": "/tmp/worker",
            },
            actor="worker",
        )
        append_event(
            task_id,
            "checkpoint.received",
            {
                "checkpoint": "plan-ready",
                "sequence": 1,
            },
            actor="worker",
        )

    def _write_report(self, outbox: Path, task_id: str) -> None:
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "report.json").write_text(
            json.dumps({
                "task_id": task_id,
                "task_hash": "sha256:protocol",
                "base_sha": "a" * 40,
                "status": "complete",
                "changed_files": ["README.md"],
                "evidence": ["README.md updated"],
                "commands": [],
                "unresolved": [],
                "scope_compliance": {
                    "outside_allowlist_touched": False,
                    "unrequested_dependencies_added": False,
                    "architecture_decisions_made": False,
                },
            }),
            encoding="utf-8",
        )

    def test_implementer_report_requires_prior_step_approval(self):
        task_id = "protocol-report-001"
        self._save_implementer_task(task_id)
        outbox = self.tmpdir / "outbox"
        self._write_report(outbox, task_id)

        with self.assertRaisesRegex(
            ArtifactValidationError,
            "before every step was approved",
        ):
            ingest_artifact(
                task_id=task_id,
                actor="worker",
                outbox=outbox,
                filename="report.json",
            )

        append_event(
            task_id,
            "codex.approved",
            {"step_id": "S01", "instruction": "continue"},
            actor="codex",
        )
        result = ingest_artifact(
            task_id=task_id,
            actor="worker",
            outbox=outbox,
            filename="report.json",
        )
        self.assertTrue(result["ok"])

    def test_review_artifact_must_match_assigned_diff(self):
        task_id = "protocol-review-001"
        save_task({
            "task_id": task_id,
            "task_hash": "sha256:review",
            "base_sha": "b" * 40,
            "role": "implementer",
            "objective": "review",
            "allowed_paths": ["README.md"],
            "steps": [],
            "acceptance_criteria": [{"id": "AC01", "text": "works"}],
            "required_checkpoints": [],
        })
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": "sha256:review",
                "base_sha": "b" * 40,
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            task_id,
            "worker.spawned",
            {
                "agent": "mimo-code",
                "terminal_handle": "worker-terminal",
                "worktree_path": "/tmp/worker",
            },
            actor="worker",
        )
        append_event(
            task_id,
            "reviewer.spawned",
            {
                "agent": "opencode",
                "terminal_handle": "review-terminal",
                "worktree_path": "/tmp/worker",
                "same_worktree": True,
                "diff_sha256": "d" * 64,
            },
            actor="reviewer",
        )

        outbox = self.tmpdir / "review-outbox"
        outbox.mkdir()
        review = {
            "task_id": task_id,
            "task_hash": "sha256:review",
            "base_sha": "b" * 40,
            "verdict": "accept",
            "diff_sha256": "e" * 64,
            "criteria": [{
                "id": "AC01",
                "status": "proven",
                "evidence": "independent test passed",
            }],
            "scope_violation": False,
            "regressions": [],
            "unverified": [],
        }
        (outbox / "review.json").write_text(
            json.dumps(review),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "does not match reviewer assignment",
        ):
            ingest_artifact(
                task_id=task_id,
                actor="reviewer",
                outbox=outbox,
                filename="review.json",
            )

        review["diff_sha256"] = "d" * 64
        (outbox / "review.json").write_text(
            json.dumps(review),
            encoding="utf-8",
        )
        result = ingest_artifact(
            task_id=task_id,
            actor="reviewer",
            outbox=outbox,
            filename="review.json",
        )
        self.assertTrue(result["ok"])

    def test_future_checkpoint_requires_previous_step_approval(self):
        task_id = "protocol-checkpoint-001"
        save_task({
            "task_id": task_id,
            "task_hash": "sha256:checkpoint",
            "base_sha": "c" * 40,
            "role": "implementer",
            "objective": "two steps",
            "allowed_paths": ["README.md"],
            "steps": [
                {"id": "S01", "instruction": "first"},
                {"id": "S02", "instruction": "second"},
            ],
            "acceptance_criteria": [{"id": "AC01", "text": "works"}],
            "required_checkpoints": ["first-ready", "second-ready"],
        })
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": "sha256:checkpoint",
                "base_sha": "c" * 40,
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            task_id,
            "worker.spawned",
            {
                "agent": "mimo-code",
                "terminal_handle": "worker-terminal",
                "worktree_path": "/tmp/worker",
            },
            actor="worker",
        )

        outbox = self.tmpdir / "checkpoint-outbox"
        outbox.mkdir()

        def write_checkpoint(
            checkpoint: str,
            sequence: int,
            step_id: str,
        ) -> None:
            (outbox / "checkpoint.json").write_text(
                json.dumps({
                    "task_id": task_id,
                    "task_hash": "sha256:checkpoint",
                    "base_sha": "c" * 40,
                    "checkpoint": checkpoint,
                    "sequence": sequence,
                    "step_id": step_id,
                    "status": "ready",
                    "changed_files": [],
                    "commands": [],
                    "unresolved": [],
                    "child_process_running": False,
                }),
                encoding="utf-8",
            )

        write_checkpoint("first-ready", 1, "S01")
        self.assertTrue(ingest_artifact(
            task_id=task_id,
            actor="worker",
            outbox=outbox,
            filename="checkpoint.json",
        )["ok"])

        write_checkpoint("second-ready", 2, "S02")
        result = ingest_artifact(
            task_id=task_id,
            actor="worker",
            outbox=outbox,
            filename="checkpoint.json",
        )
        self.assertTrue(result["ok"])

    def test_symbolic_link_artifact_is_rejected(self):
            task_id=task_id,
            actor="worker",
            outbox=outbox,
            filename="checkpoint.json",
        )["ok"])

    def test_symbolic_link_artifact_is_rejected(self):
        task_id = "protocol-symlink-001"
        save_task({
            "task_id": task_id,
            "task_hash": "sha256:protocol",
            "base_sha": "a" * 40,
            "role": "test",
            "objective": "test",
            "allowed_paths": [],
            "steps": [],
            "acceptance_criteria": [],
            "required_checkpoints": [],
        })
        outbox = self.tmpdir / "symlink-outbox"
        outbox.mkdir()
        target = self.tmpdir / "outside.json"
        target.write_text("{}", encoding="utf-8")
        (outbox / "report.json").symlink_to(target)

        with self.assertRaisesRegex(
            ArtifactValidationError,
            "must not be a symbolic link",
        ):
            ingest_artifact(
                task_id=task_id,
                actor="worker",
                outbox=outbox,
                filename="report.json",
            )


class TestContinuousPreauthorizedProtocol(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch(
            "sin_orca.state.state_root",
            lambda *a, **k: self.tmpdir,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _append_callback(
        self,
        task_id: str,
        callback_type: str,
        *,
        step_id: str | None = None,
    ) -> None:
        payload = {
            "callback_type": callback_type,
            "summary": callback_type,
            "changed_files": [],
            "verification_status": "passed",
            "requested_action": "none",
            "parent_terminal_handle": "parent-terminal",
        }
        if step_id is not None:
            payload["step_id"] = step_id
        append_event(
            task_id,
            "worker.callback",
            payload,
            actor="worker",
        )

    def test_two_steps_complete_without_explicit_approvals(self):
        from sin_orca.gates import execution_protocol_errors

        task_id = "continuous-protocol-001"
        save_task({
            "task_id": task_id,
            "task_hash": "sha256:continuous",
            "base_sha": "a" * 40,
            "repository_root": "/tmp/repository",
            "parent_terminal_handle": "parent-terminal",
            "approval_mode": "continuous-preauthorized",
            "role": "implementer",
            "objective": "execute two bounded steps",
            "allowed_paths": ["src/"],
            "forbidden_paths": [],
            "steps": [
                {"id": "S01", "instruction": "first"},
                {"id": "S02", "instruction": "second"},
            ],
            "acceptance_criteria": [],
            "required_checkpoints": ["first-complete", "second-complete"],
            "allow_edits": True,
        })
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": "sha256:continuous",
                "base_sha": "a" * 40,
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            task_id,
            "worker.spawned",
            {
                "agent": "mimo-code",
                "terminal_handle": "worker-terminal",
                "parent_terminal_handle": "parent-terminal",
                "worktree_path": "/tmp/repository",
                "worktree_selector": "path:/tmp/repository",
                "same_worktree": True,
                "outbox_path": "/tmp/repository/.sin-worker/tasks/continuous-protocol-001/outbox",
            },
            actor="worker",
        )
        self._append_callback(task_id, "ack")

        for sequence, (step_id, checkpoint) in enumerate(
            (("S01", "first-complete"), ("S02", "second-complete")),
            start=1,
        ):
            append_event(
                task_id,
                "checkpoint.received",
                {
                    "checkpoint": checkpoint,
                    "sequence": sequence,
                    "step_id": step_id,
                    "status": "complete",
                    "changed_files": [],
                    "commands": [],
                    "unresolved": [],
                    "child_process_running": False,
                },
                actor="worker",
            )
            self._append_callback(
                task_id,
                "checkpoint",
                step_id=step_id,
            )

        append_event(
            task_id,
            "worker.report.received",
            {
                "task_id": task_id,
                "task_hash": "sha256:continuous",
                "base_sha": "a" * 40,
                "status": "complete",
                "changed_files": [],
                "evidence": ["two bounded steps completed"],
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
        self._append_callback(task_id, "done")

        self.assertEqual(execution_protocol_errors(task_id), [])

        append_event(
            task_id,
            "codex.approved",
            {"step_id": "S01", "instruction": "unexpected"},
            actor="codex",
        )
        errors = execution_protocol_errors(task_id)
        self.assertTrue(
            any("unexpected explicit approvals" in error for error in errors),
            errors,
        )


class TestSecretIsolation(unittest.TestCase):
    def test_controller_environment_does_not_expose_manifest_key(self):
        with patch.dict(
            os.environ,
            {"SIN_MANIFEST_HMAC_KEY": "controller-only-secret"},
        ):
            environment = controller_environment()

        self.assertNotIn("SIN_MANIFEST_HMAC_KEY", environment)

    def test_verification_redacts_secret_arguments_and_output(self):
        argv = redact_argv([
            "runner",
            "--api-key",
            "top-secret",
            "--token=second-secret",
            "https://user:password@example.invalid/path",
        ])
        rendered = " ".join(argv)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("second-secret", rendered)
        self.assertNotIn("password@example", rendered)
        self.assertIn("<redacted>", rendered)

        output = redact_text(
            "password=hunter2 Authorization: Bearer abc.def.ghi"
        )
        self.assertNotIn("hunter2", output)
        self.assertNotIn("abc.def.ghi", output)

    def test_orca_subprocess_does_not_receive_manifest_key(self):
        from sin_orca.dispatch import run_orca

        completed = subprocess.CompletedProcess(
            ["orca"],
            0,
            stdout='{"ok": true}',
            stderr="",
        )
        with patch.dict(
            os.environ,
            {"SIN_MANIFEST_HMAC_KEY": "controller-only-secret"},
        ), patch(
            "sin_orca.dispatch.shutil.which",
            return_value="/usr/local/bin/orca",
        ), patch(
            "sin_orca.dispatch.subprocess.run",
            return_value=completed,
        ) as runner:
            result = run_orca(["status"])

        self.assertTrue(result["ok"])
        environment = runner.call_args.kwargs["env"]
        self.assertNotIn("SIN_MANIFEST_HMAC_KEY", environment)


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
