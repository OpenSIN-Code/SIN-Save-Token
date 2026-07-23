#!/usr/bin/env python3
"""
E2E tests for sin-orca with a fake orca binary.
Tests the real flow: dispatch → Orca → Worker → Review → Complete.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import unittest

FAKE_ORCA_SCRIPT = textwrap.dedent(r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_ORCA_STATE"])
state.mkdir(parents=True, exist_ok=True)
with (state / "commands.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\n")

terminals_path = state / "terminals.json"
if terminals_path.is_file():
    terminals = json.loads(terminals_path.read_text())
else:
    terminals = ["parent-terminal"]

def save_terminals():
    terminals_path.write_text(json.dumps(terminals))

if args[:2] == ["worktree", "create"]:
    print(json.dumps({"ok": False, "error": "worktree creation forbidden"}))
    raise SystemExit(9)

if args[:2] == ["terminal", "list"]:
    print(json.dumps({
        "ok": True,
        "result": {"terminals": [{"handle": item} for item in terminals]},
    }))
    raise SystemExit(0)

if args[:2] == ["terminal", "create"]:
    handle = f"worker-terminal-{len(terminals)}"
    terminals.append(handle)
    save_terminals()
    print(json.dumps({"ok": True, "result": {"handle": handle}}))
    raise SystemExit(0)

if args[:2] == ["terminal", "send"]:
    log = state / "terminal-send.jsonl"
    with log.open("a") as handle:
        handle.write(json.dumps(args) + "\n")
    print(json.dumps({"ok": True}))
    raise SystemExit(0)

if args[:2] == ["terminal", "read"]:
    print(json.dumps({
        "ok": True,
        "result": {"text": ""},
        "nextCursor": "cursor-1",
    }))
    raise SystemExit(0)

if args[:2] == ["terminal", "wait"]:
    print(json.dumps({"ok": True, "status": "idle"}))
    raise SystemExit(0)

print(json.dumps({"ok": False, "error": "unsupported", "args": args}))
raise SystemExit(1)
''')


def initialize_repository(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


class TestE2EDispatch(unittest.TestCase):
    """E2E: dispatch creates real worker state via fake Orca."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.repository = self.tmpdir / "repo"
        self.repository.mkdir()
        initialize_repository(self.repository)

        self.fake_bin = self.tmpdir / "bin"
        self.fake_bin.mkdir()
        self.fake_orca = self.fake_bin / "orca"
        self.fake_orca.write_text(
            f"#!/bin/sh\nexec python3 -c '\n{FAKE_ORCA_SCRIPT}' \"$@\"\n",
            encoding="utf-8",
        )
        self.fake_orca.chmod(0o755)

        self.state_root = self.tmpdir / "controller-state"
        self.fake_state = self.tmpdir / "fake-orca-state"

        self.env = {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "SIN_ORCA_STATE_ROOT": str(self.state_root),
            "FAKE_ORCA_STATE": str(self.fake_state),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "lib"),
        }

        self.sin_orca_bin = str(
            Path(__file__).resolve().parent.parent / "bin" / "sin-orca"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dispatch_creates_real_worker_state(self):
        process = subprocess.run(
            [
                self.sin_orca_bin, "dispatch",
                "--role", "implementer",
                "--agent", "mimo-code",
                "--parent-terminal", "parent-terminal",
                "--objective", "Change README",
                "--step", "Change README safely",
                "--allowed-path", "README.md",
                "--acceptance", "README contains the requested text",
                "--verify-command", "git diff --check",
                "--checkpoint", "plan-ready",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

        self.assertEqual(process.returncode, 0, process.stderr)

        output = json.loads(process.stdout)
        self.assertNotEqual(output["base_sha"], "HEAD")
        self.assertEqual(len(output["base_sha"]), 40)
        self.assertEqual(output["terminal"], "worker-terminal-1")
        self.assertEqual(
            output["approval_mode"], "continuous-preauthorized"
        )

        task_id = output["task_id"]
        self.assertTrue(
            (
                self.repository
                / ".sin-worker"
                / "tasks"
                / task_id
                / "outbox"
            ).is_dir()
        )
        task_files = list(self.state_root.rglob(f"{task_id}/task.json"))
        self.assertEqual(len(task_files), 1)

        task = json.loads(task_files[0].read_text(encoding="utf-8"))
        self.assertEqual(task["task_id"], task_id)
        self.assertTrue(task["task_hash"].startswith("sha256:"))
        self.assertEqual(task["base_sha"], output["base_sha"])
        self.assertEqual(
            task["approval_mode"], "continuous-preauthorized"
        )
        prompt = (task_files[0].parent / "worker-prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Approval mode: continuous-preauthorized", prompt)
        self.assertIn("continue automatically", prompt)
        self.assertIn("type <ack|checkpoint|discovery|question", prompt)

        events_file = task_files[0].parent / "events.jsonl"
        events = [
            json.loads(line)
            for line in events_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertEqual(
            [event["type"] for event in events],
            ["task.created", "worker.spawned"],
        )

    def test_stepwise_dispatch_is_explicit_and_not_default(self):
        process = subprocess.run(
            [
                self.sin_orca_bin, "dispatch",
                "--role", "implementer",
                "--agent", "mimo-code",
                "--parent-terminal", "parent-terminal",
                "--approval-mode", "stepwise",
                "--objective", "Change README at a protected boundary",
                "--step", "Change README safely",
                "--allowed-path", "README.md",
                "--acceptance", "README contains the requested text",
                "--verify-command", "git diff --check",
                "--checkpoint", "approval-boundary",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        output = json.loads(process.stdout)
        self.assertEqual(output["approval_mode"], "stepwise")
        task_file = next(
            self.state_root.rglob(f"{output['task_id']}/task.json")
        )
        task = json.loads(task_file.read_text(encoding="utf-8"))
        self.assertEqual(task["approval_mode"], "stepwise")
        prompt = (task_file.parent / "worker-prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Approval mode: stepwise", prompt)
        self.assertIn("explicit high-risk boundary", prompt)

    def test_discovery_callback_reaches_parent_and_event_log(self):
        dispatched = subprocess.run(
            [
                self.sin_orca_bin, "dispatch",
                "--role", "explorer",
                "--agent", "opencode",
                "--parent-terminal", "parent-terminal",
                "--objective", "Inspect project gaps",
                "--step", "Inspect README",
                "--allowed-path", "README.md",
                "--acceptance", "Gaps are reported",
                "--read-only",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        task_id = json.loads(dispatched.stdout)["task_id"]
        notified = subprocess.run(
            [
                self.sin_orca_bin, "notify", task_id,
                "--type", "discovery",
                "--summary", "Missing recovery path discovered",
                "--action", "add a bounded recovery task",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(notified.returncode, 0, notified.stderr)
        payload = json.loads(notified.stdout)
        self.assertEqual(payload["type"], "discovery")
        events_file = next(
            self.state_root.rglob(f"{task_id}/events.jsonl")
        )
        events = [
            json.loads(line)
            for line in events_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        callbacks = [
            event for event in events
            if event["type"] == "worker.callback"
        ]
        self.assertEqual(
            callbacks[-1]["payload"]["callback_type"], "discovery"
        )
        sends = [
            json.loads(line)
            for line in (self.fake_state / "terminal-send.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(
                "type=discovery" in command[command.index("--text") + 1]
                for command in sends
                if command[:2] == ["terminal", "send"]
                and "--text" in command
            )
        )

    def test_checkpoint_notify_archives_artifact_before_direct_callback(self):
        dispatched = subprocess.run(
            [
                self.sin_orca_bin, "dispatch",
                "--role", "implementer",
                "--agent", "mimo-code",
                "--parent-terminal", "parent-terminal",
                "--objective", "Exercise transactional checkpoint callback",
                "--step", "Inspect README without changing it",
                "--allowed-path", "README.md",
                "--acceptance", "Checkpoint is archived before callback",
                "--verify-command", "git diff --check",
                "--checkpoint", "inspection-complete",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        output = json.loads(dispatched.stdout)
        task_id = output["task_id"]
        task_file = next(self.state_root.rglob(f"{task_id}/task.json"))
        task = json.loads(task_file.read_text(encoding="utf-8"))

        acknowledged = subprocess.run(
            [
                self.sin_orca_bin, "notify", task_id,
                "--type", "ack",
                "--summary", "scope understood",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)

        outbox = Path(output["artifact_outbox"])
        checkpoint_path = outbox / "checkpoint.json"
        checkpoint_path.write_text(
            json.dumps({
                "task_id": task_id,
                "task_hash": task["task_hash"],
                "base_sha": task["base_sha"],
                "checkpoint": "inspection-complete",
                "sequence": 1,
                "step_id": "S01",
                "status": "complete",
                "changed_files": [],
                "commands": [],
                "unresolved": [],
                "child_process_running": False,
            }),
            encoding="utf-8",
        )

        notified = subprocess.run(
            [
                self.sin_orca_bin, "notify", task_id,
                "--type", "checkpoint",
                "--step", "S01",
                "--summary", "inspection complete",
                "--verify", "passed",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(notified.returncode, 0, notified.stderr)
        payload = json.loads(notified.stdout)
        self.assertTrue(payload["artifact"]["ok"])
        self.assertEqual(payload["type"], "checkpoint")
        self.assertFalse(checkpoint_path.exists())

        events = [
            json.loads(line)
            for line in (task_file.parent / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        artifact_event = next(
            event for event in events
            if event["type"] == "checkpoint.received"
        )
        callback_event = next(
            event for event in events
            if event["type"] == "worker.callback"
            and event["payload"].get("callback_type") == "checkpoint"
        )
        self.assertLess(
            artifact_event["sequence"], callback_event["sequence"]
        )
        self.assertEqual(
            callback_event["payload"]["artifact"]["filename"],
            "checkpoint.json",
        )

        sends = [
            json.loads(line)
            for line in (self.fake_state / "terminal-send.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(
                "type=checkpoint" in command[command.index("--text") + 1]
                and "step=S01" in command[command.index("--text") + 1]
                for command in sends
                if command[:2] == ["terminal", "send"]
                and "--text" in command
            )
        )

    def test_dispatch_without_parent_terminal_fails_closed(self):
        process = subprocess.run(
            [
                self.sin_orca_bin,
                "dispatch",
                "--role",
                "explorer",
                "--agent",
                "opencode",
                "--objective",
                "Read code",
                "--step",
                "Read the code",
                "--allowed-path",
                "README.md",
                "--acceptance",
                "Code is read",
                "--read-only",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env={
                key: value
                for key, value in self.env.items()
                if key not in {
                    "SIN_ORCA_PARENT_TERMINAL",
                    "ORCA_TERMINAL_HANDLE",
                    "ORCA_CURRENT_TERMINAL",
                }
            },
            check=False,
        )

        self.assertNotEqual(process.returncode, 0)
        self.assertIn("parent-terminal", process.stderr)

    def test_dispatch_uses_terminal_create_and_forbids_worktree_create(self):
        process = subprocess.run(
            [
                self.sin_orca_bin, "dispatch",
                "--role", "explorer",
                "--agent", "opencode",
                "--parent-terminal", "parent-terminal",
                "--objective", "Read code",
                "--step", "Read the code",
                "--allowed-path", "src/",
                "--acceptance", "Code is read",
                "--read-only",
            ],
            cwd=self.repository,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

        self.assertEqual(process.returncode, 0, process.stderr)

        commands = [
            json.loads(line)
            for line in (self.fake_state / "commands.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertTrue(any(command[:2] == ["terminal", "create"] for command in commands))
        self.assertFalse(any(command[:2] == ["worktree", "create"] for command in commands))
        self.assertTrue(
            any(
                command[:2] == ["terminal", "create"]
                and command[command.index("--worktree") + 1]
                == f"path:{self.repository}"
                for command in commands
            )
        )

    def test_two_cache_entries_can_share_one_blob(self):
        from sin_cache import SinCache

        cache = SinCache(db_path=self.tmpdir / "test.db")
        cache.put("route1", "prov1", "query1", "repo1", "shared content")
        cache.put("route2", "prov2", "query2", "repo2", "shared content")

        blob_count = cache.conn.execute(
            "SELECT COUNT(*) FROM cache_blobs"
        ).fetchone()[0]

        self.assertEqual(blob_count, 1)

        cache.close()

    def test_evidence_validation_uses_current_worktree(self):
        from sin_cache import SinCache

        repo = self.tmpdir / "evidence-repo"
        repo.mkdir()
        (repo / "auth.py").write_text("token = 'secret'\n", encoding="utf-8")

        cache = SinCache(db_path=self.tmpdir / "test.db")
        cache.put_evidence(
            "code_symbol", "graphify", "where is token",
            "repo1", "Token is in auth.py",
            evidence=[{"path": "auth.py", "content_sha256": ""}],
            repository_path=repo,
        )

        result = cache.get(
            "code_symbol", "graphify", "where is token",
            "repo1",
            repository_path=repo,
        )
        self.assertIsNotNone(result)

        (repo / "auth.py").write_text("token = 'changed'\n", encoding="utf-8")

        result_stale = cache.get(
            "code_symbol", "graphify", "where is token",
            "repo1",
            repository_path=repo,
        )
        self.assertIsNone(result_stale)

        cache.close()

    def test_verification_preserves_quoted_arguments(self):
        from sin_orca.verification import run_controller_commands

        repo = self.tmpdir / "verify-repo"
        repo.mkdir()
        initialize_repository(repo)

        result = run_controller_commands(
            worktree=repo,
            commands=[["git", "log", "--oneline", "-1"]],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["argv"], ["git", "log", "--oneline", "-1"])

    def test_bounded_diff_includes_untracked_files(self):
        from sin_orca.verification import bounded_diff

        repo = self.tmpdir / "untracked-diff-repo"
        repo.mkdir()
        initialize_repository(repo)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        (repo / "new_module.py").write_text(
            "VALUE = 42\n",
            encoding="utf-8",
        )

        result = bounded_diff(worktree=repo, base_sha=base_sha)

        self.assertIn("new_module.py", result["text"])
        self.assertIn("VALUE = 42", result["text"])
        self.assertGreater(result["full_chars"], 0)

    def test_same_worktree_baseline_preserves_head_index_and_dirty_state(self):
        from sin_orca.dispatch import (
            baseline_ref_is_valid,
            create_baseline_commit,
        )
        from sin_orca.verification import actual_changed_files, bounded_diff

        repo = self.tmpdir / "baseline-repo"
        repo.mkdir()
        initialize_repository(repo)
        (repo / "README.md").write_text(
            "preexisting staged state\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (repo / "preexisting.txt").write_text(
            "already here\n",
            encoding="utf-8",
        )

        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        index_before = subprocess.run(
            ["git", "write-tree"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        status_before = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

        repository_head, baseline_sha, baseline_ref = create_baseline_commit(
            repo,
            "baseline-contract-001",
        )
        self.assertEqual(repository_head, head_before)
        self.assertTrue(baseline_ref_is_valid(repo, baseline_ref, baseline_sha))
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            head_before,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "write-tree"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            index_before,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout,
            status_before,
        )
        self.assertEqual(
            actual_changed_files(worktree=repo, base_sha=baseline_sha),
            [],
        )

        (repo / "README.md").write_text("worker state\n", encoding="utf-8")
        (repo / "worker.txt").write_text("new work\n", encoding="utf-8")
        runtime_file = (
            repo
            / ".sin-worker"
            / "tasks"
            / "baseline-contract-001"
            / "outbox"
            / "checkpoint.json"
        )
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("{}\n", encoding="utf-8")

        self.assertEqual(
            actual_changed_files(worktree=repo, base_sha=baseline_sha),
            ["README.md", "worker.txt"],
        )
        diff = bounded_diff(worktree=repo, base_sha=baseline_sha)
        self.assertIn("worker state", diff["text"])
        self.assertIn("worker.txt", diff["text"])
        self.assertNotIn(".sin-worker", diff["text"])

    def test_complete_writes_reproducible_manifest(self):
        import argparse
        import contextlib
        import io
        from sin_orca.cli import _cmd_complete
        from sin_orca.state import append_event, save_task
        from sin_orca.verification import bounded_diff
        from sin_orca.writer_reservation import acquire_writer
        from unittest.mock import patch

        repo = self.tmpdir / "manifest-repo"
        repo.mkdir()
        initialize_repository(repo)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        (repo / "README.md").write_text("updated\n", encoding="utf-8")
        diff_sha256 = bounded_diff(
            worktree=repo,
            base_sha=base_sha,
        )["full_sha256"]

        state = self.tmpdir / "manifest-state"
        task_id = "manifest-test-001"
        baseline_ref = f"refs/sin-orca/baselines/{task_id}"
        subprocess.run(
            ["git", "update-ref", baseline_ref, base_sha],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        task = {
            "task_id": task_id,
            "task_hash": "sha256:manifest-test",
            "base_sha": base_sha,
            "baseline_ref": baseline_ref,
            "repository_root": str(repo),
            "repository_head_sha": base_sha,
            "role": "explorer",
            "objective": "Verify completion manifest",
            "allowed_paths": ["README.md"],
            "forbidden_paths": [],
            "steps": [],
            "acceptance_criteria": [],
            "required_checkpoints": [],
            "allow_edits": True,
        }

        with patch("sin_orca.state.state_root", lambda *a, **k: state):
            save_task(task)
            acquire_writer(repo, task_id=task_id)
            append_event(
                task_id,
                "task.created",
                {
                    "task_hash": task["task_hash"],
                    "base_sha": base_sha,
                    "role": "explorer",
                },
                actor="codex",
            )
            append_event(
                task_id,
                "worker.spawned",
                {
                    "agent": "mimo-code",
                    "terminal_handle": "terminal-001",
                    "worktree_path": str(repo),
                },
                actor="worker",
            )
            append_event(
                task_id,
                "worker.callback",
                {
                    "callback_type": "ack",
                    "summary": "scope understood",
                    "changed_files": [],
                    "verification_status": "not-run",
                    "requested_action": "none",
                },
                actor="worker",
            )
            append_event(
                task_id,
                "worker.report.received",
                {
                    "task_id": task_id,
                    "task_hash": task["task_hash"],
                    "base_sha": base_sha,
                    "status": "complete",
                    "changed_files": ["README.md"],
                    "evidence": [],
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
            append_event(
                task_id,
                "worker.callback",
                {
                    "callback_type": "done",
                    "summary": "exploration complete",
                    "changed_files": ["README.md"],
                    "verification_status": "passed",
                    "requested_action": "verify",
                },
                actor="worker",
            )
            append_event(
                task_id,
                "verification.completed",
                {
                    "ok": True,
                    "results": [{"ok": True, "argv": ["git", "diff", "--check"]}],
                    "changed_files": ["README.md"],
                    "diff_sha256": diff_sha256,
                },
                actor="controller",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = _cmd_complete(argparse.Namespace(task_id=task_id))

            self.assertEqual(result, 0, output.getvalue())
            manifest_path = state / task_id / "completion-manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["body"]["changed_files"], ["README.md"])
            self.assertTrue(manifest["integrity"]["hash"])

            second_output = io.StringIO()
            with contextlib.redirect_stdout(second_output):
                second_result = _cmd_complete(
                    argparse.Namespace(task_id=task_id)
                )
            self.assertEqual(second_result, 0, second_output.getvalue())
            self.assertTrue(json.loads(second_output.getvalue())["reused"])

            (repo / "README.md").write_text(
                "changed after completion\n",
                encoding="utf-8",
            )
            stale_output = io.StringIO()
            with contextlib.redirect_stdout(stale_output):
                stale_result = _cmd_complete(
                    argparse.Namespace(task_id=task_id)
                )
            self.assertEqual(stale_result, 1, stale_output.getvalue())
            stale_payload = json.loads(stale_output.getvalue())
            self.assertEqual(stale_payload["status"], "manifest-invalid")
            self.assertTrue(
                any(
                    "worktree" in error
                    for error in stale_payload["errors"]
                )
            )

    def test_stall_detection_does_not_refresh_before_check(self):
        from datetime import datetime, timedelta, timezone
        from sin_orca.cli import _check_stalled

        task_id = "stall-test"
        task_dir_path = self.state_root / "stall" / task_id
        task_dir_path.mkdir(parents=True, exist_ok=True)

        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=2000)
        ).isoformat()

        activity = {"worker_last_active": old_time}
        (task_dir_path / "activity.json").write_text(
            json.dumps(activity), encoding="utf-8"
        )

        with unittest.mock.patch(
            "sin_orca.cli.task_dir",
            lambda *a, **k: task_dir_path,
        ):
            result = _check_stalled(
                task_id, "worker",
                {"worker_control": {"stalled_worker_policy": {"enabled": True, "maximum_inactive_seconds": 1200}}},
            )

        self.assertTrue(result["stalled"])

    def test_blind_reviewer_uses_new_terminal_in_implementer_worktree(self):
        from sin_orca.review import start_blind_review
        from sin_orca.state import append_event, rebuild_ledger, save_task
        from sin_orca.verification import bounded_diff
        from sin_orca.writer_reservation import acquire_writer
        from unittest.mock import patch

        repo = self.tmpdir / "review-worktree"
        repo.mkdir()
        initialize_repository(repo)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        (repo / "README.md").write_text(
            "test\nreview change\n",
            encoding="utf-8",
        )
        diff_sha256 = bounded_diff(
            worktree=repo,
            base_sha=base_sha,
        )["full_sha256"]

        state = self.tmpdir / "review-state"
        task_id = "review-terminal-001"
        baseline_ref = f"refs/sin-orca/baselines/{task_id}"
        subprocess.run(
            ["git", "update-ref", baseline_ref, base_sha],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        task = {
            "task_id": task_id,
            "task_hash": "sha256:review-terminal",
            "base_sha": base_sha,
            "baseline_ref": baseline_ref,
            "repository_root": str(repo),
            "repository_head_sha": base_sha,
            "worktree_selector": f"path:{repo}",
            "parent_terminal_handle": "parent-terminal",
            "artifact_outbox": ".sin-worker/tasks/review-terminal-001/outbox",
            "role": "implementer",
            "objective": "review README",
            "allowed_paths": ["README.md"],
            "forbidden_paths": [],
            "steps": [{"id": "S01", "instruction": "edit"}],
            "acceptance_criteria": [{"id": "AC01", "text": "review change exists"}],
            "required_checkpoints": ["plan-ready"],
            "allow_edits": True,
        }

        with patch("sin_orca.state.state_root", lambda *a, **k: state):
            save_task(task)
            acquire_writer(repo, task_id=task_id)
            append_event(
                task_id,
                "task.created",
                {
                    "task_hash": task["task_hash"],
                    "base_sha": base_sha,
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
                    "worktree_path": str(repo),
                    "worktree_selector": f"path:{repo}",
                    "same_worktree": True,
                    "outbox_path": str(
                        repo / ".sin-worker/tasks/review-terminal-001/outbox"
                    ),
                },
                actor="worker",
            )
            append_event(
                task_id,
                "worker.callback",
                {
                    "callback_type": "ack",
                    "summary": "scope understood",
                    "changed_files": [],
                    "verification_status": "not-run",
                    "requested_action": "none",
                },
                actor="worker",
            )
            append_event(
                task_id,
                "checkpoint.received",
                {
                    "checkpoint": "plan-ready",
                    "sequence": 1,
                    "step_id": "S01",
                    "status": "ready",
                    "changed_files": [],
                    "commands": [],
                    "unresolved": [],
                    "child_process_running": False,
                },
                actor="worker",
            )
            append_event(
                task_id,
                "worker.callback",
                {
                    "callback_type": "checkpoint",
                    "step_id": "S01",
                    "summary": "plan ready",
                    "changed_files": [],
                    "verification_status": "not-run",
                    "requested_action": "approve S01",
                },
                actor="worker",
            )
            append_event(
                task_id,
                "codex.approved",
                {"step_id": "S01", "instruction": "continue"},
                actor="codex",
            )
            append_event(
                task_id,
                "worker.report.received",
                {
                    "task_id": task_id,
                    "task_hash": task["task_hash"],
                    "base_sha": base_sha,
                    "status": "complete",
                    "changed_files": ["README.md"],
                    "evidence": ["README.md contains review change"],
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
            append_event(
                task_id,
                "worker.callback",
                {
                    "callback_type": "done",
                    "summary": "implementation complete",
                    "changed_files": ["README.md"],
                    "verification_status": "passed",
                    "requested_action": "verify",
                },
                actor="worker",
            )
            append_event(
                task_id,
                "verification.completed",
                {
                    "ok": True,
                    "results": [{"ok": True, "argv": ["git", "diff", "--check"]}],
                    "changed_files": ["README.md"],
                    "diff_sha256": diff_sha256,
                },
                actor="controller",
            )

            calls = [
                {"result": {"terminals": [{"handle": "worker-terminal"}]}},
                {"ok": True, "result": {"handle": "reviewer-terminal"}},
                {"ok": True},
            ]
            with patch(
                "sin_orca.review.ReviewContextBuilder.build_review_context",
                return_value={
                    "schema_version": 1,
                    "base_sha": base_sha,
                    "worktree": str(repo),
                    "changed_files": [{"path": "README.md"}],
                    "changed_symbols": [],
                    "affected_flows": [],
                    "test_gaps": [],
                    "risk_signals": [],
                    "crg_advisory": {
                        "ok": False,
                        "provider": "code-review-graph",
                        "status": "test-fixture",
                        "authoritative": False,
                    },
                    "crg_authoritative": False,
                    "graphify_paths": [],
                    "uncertainties": [],
                    "recommended_review_order": [],
                    "total_risk_score": 0.0,
                    "diff_hash": diff_sha256,
                    "diff_length": 1,
                },
            ), patch(
                "sin_orca.review.run_orca",
                side_effect=calls,
            ):
                result = start_blind_review(
                    task_id=task_id,
                    preferred_agents=["opencode", "mimo-code"],
                )

            self.assertEqual(result["terminal"], "reviewer-terminal")
            self.assertTrue(result["same_worktree"])
            self.assertEqual(result["worktree_path"], str(repo))
            self.assertEqual(result["diff_sha256"], diff_sha256)

            reviewer = rebuild_ledger(task_id)["actors"]["reviewer"]
            self.assertEqual(reviewer["terminal_handle"], "reviewer-terminal")
            self.assertNotEqual(
                reviewer["terminal_handle"],
                "worker-terminal",
            )
            self.assertEqual(reviewer["worktree_path"], str(repo))

    def test_completion_rejects_unverified_criterion(self):
        from sin_orca.gates import completion_errors
        from sin_orca.state import save_task, append_event
        from unittest.mock import patch

        tmpdir = self.tmpdir / "completion-test"
        tmpdir.mkdir()

        with patch("sin_orca.state.state_root", lambda *a, **k: tmpdir):
            task_id = "comp-test-001"
            save_task({
                "task_id": task_id,
                "task_hash": "sha256:abc",
                "base_sha": "a" * 40,
                "role": "implementer",
                "objective": "test",
                "allowed_paths": ["src/"],
                "forbidden_paths": [],
                "steps": [],
                "acceptance_criteria": [{"id": "AC01", "text": "It works"}],
                "required_checkpoints": [],
                "allow_edits": True,
            })

            append_event(
                task_id, "task.created",
                {"task_hash": "sha256:abc", "base_sha": "a" * 40, "role": "implementer"},
                actor="codex",
            )

            errors = completion_errors(task_id, actual_changed_files=["src/main.py"])

            self.assertTrue(any("not proven" in e or "review" in e.lower() for e in errors))

    def test_completion_rejects_directory_scope_escape(self):
        from sin_orca.gates import completion_errors
        from sin_orca.state import save_task, append_event
        from unittest.mock import patch

        tmpdir = self.tmpdir / "scope-test"
        tmpdir.mkdir()

        with patch("sin_orca.state.state_root", lambda *a, **k: tmpdir):
            task_id = "scope-test-001"
            save_task({
                "task_id": task_id,
                "task_hash": "sha256:abc",
                "base_sha": "a" * 40,
                "role": "implementer",
                "objective": "test",
                "allowed_paths": ["src/"],
                "forbidden_paths": [],
                "steps": [],
                "acceptance_criteria": [{"id": "AC01", "text": "It works"}],
                "required_checkpoints": [],
                "allow_edits": True,
            })

            append_event(
                task_id, "task.created",
                {"task_hash": "sha256:abc", "base_sha": "a" * 40, "role": "implementer"},
                actor="codex",
            )

            errors = completion_errors(
                task_id,
                actual_changed_files=["src/main.py", "etc/passwd"],
            )

            self.assertTrue(any("outside allowlist" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
