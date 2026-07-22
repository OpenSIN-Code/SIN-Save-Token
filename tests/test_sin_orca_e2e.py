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

if args[:2] == ["worktree", "create"]:
    name = args[args.index("--name") + 1]
    worktree = state / name
    (worktree / ".sin-worker" / "outbox").mkdir(parents=True, exist_ok=True)
    print(json.dumps({
        "worktreeId": name,
        "worktreePath": str(worktree),
        "branch": name,
    }))
    raise SystemExit(0)

if args[:2] == ["terminal", "list"]:
    print(json.dumps({
        "terminals": [{"handle": "terminal-001"}]
    }))
    raise SystemExit(0)

if args[:2] == ["terminal", "send"]:
    log = state / "terminal-send.jsonl"
    with log.open("a") as handle:
        handle.write(json.dumps(args) + "\n")
    print(json.dumps({"ok": True}))
    raise SystemExit(0)

if args[:2] == ["terminal", "read"]:
    print(json.dumps({
        "result": {"text": ""},
        "nextCursor": "cursor-1",
    }))
    raise SystemExit(0)

if args[:2] == ["terminal", "wait"]:
    print(json.dumps({"status": "idle"}))
    raise SystemExit(0)

print(json.dumps({"error": "unsupported", "args": args}))
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
                "--objective", "Change README",
                "--step", "Change README safely",
                "--allowed-path", "README.md",
                "--acceptance", "README contains the requested text",
                "--verify-command", "git diff --check",
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
        self.assertEqual(output["terminal"], "terminal-001")

        task_id = output["task_id"]
        task_files = list(self.state_root.rglob(f"{task_id}/task.json"))
        self.assertEqual(len(task_files), 1)

        task = json.loads(task_files[0].read_text(encoding="utf-8"))
        self.assertEqual(task["task_id"], task_id)
        self.assertTrue(task["task_hash"].startswith("sha256:"))
        self.assertEqual(task["base_sha"], output["base_sha"])

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

    def test_dispatch_calls_orca_worktree_create(self):
        process = subprocess.run(
            [
                self.sin_orca_bin, "dispatch",
                "--role", "explorer",
                "--agent", "opencode",
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

        worktrees = list(self.fake_state.iterdir())
        self.assertTrue(len(worktrees) > 0)

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
