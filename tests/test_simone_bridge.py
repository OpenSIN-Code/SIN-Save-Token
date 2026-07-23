from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sin_orca.cli import _sync_bound_task
from sin_orca.simone_bridge import (
    _parse_json_object,
    compact_event,
    sync_task,
)
from sin_orca.state import append_event, save_task


class TestSimoneBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        self.state_patch = patch(
            "sin_orca.state.state_root",
            lambda *args, **kwargs: self.state,
        )
        self.state_patch.start()

    def tearDown(self) -> None:
        self.state_patch.stop()

    def test_control_plane_json_parser_tolerates_short_preamble(self) -> None:
        parsed = _parse_json_object(
            "control-plane ready\n{\"ok\": true, \"result\": {}}\n"
        )
        self.assertTrue(parsed["ok"])

    def test_compact_text_is_redacted_and_bounded(self) -> None:
        compact = compact_event({
            "type": "codex.sent",
            "payload": {
                "text": "Authorization: Bearer secret-value",
            },
        })
        self.assertNotIn("secret-value", compact["text"])
        self.assertIn("<redacted>", compact["text"])

    def test_compact_verification_removes_raw_output(self) -> None:
        compact = compact_event(
            {
                "type": "verification.completed",
                "payload": {
                    "ok": True,
                    "changed_files": ["src/main.py"],
                    "results": [
                        {
                            "argv": ["pytest", "-q"],
                            "exit_code": 0,
                            "stdout": "raw stdout",
                            "stderr": "raw stderr",
                            "output_tail": "raw tail",
                            "stdout_sha256": "a" * 64,
                            "stderr_sha256": "b" * 64,
                        }
                    ],
                },
            }
        )

        result = compact["results"][0]
        self.assertEqual(result["argv"], ["pytest", "-q"])
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertNotIn("output_tail", result)
        self.assertEqual(result["stdout_sha256"], "a" * 64)

    def test_sync_replays_events_and_reference_only_artifacts(self) -> None:
        task_id = "bridge-task-001"
        save_task(
            {
                "task_id": task_id,
                "simone_task_id": "TASK-SIMONE-001",
                "task_hash": "sha256:bridge",
                "repository_root": "/repo",
                "base_sha": "a" * 40,
                "role": "implementer",
            }
        )
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": "sha256:bridge",
                "base_sha": "a" * 40,
                "role": "implementer",
            },
            actor="codex",
        )
        append_event(
            task_id,
            "worker.report.received",
            {
                "status": "complete",
                "changed_files": ["src/main.py"],
                "unresolved": [],
                "scope_compliance": {
                    "outside_allowlist_touched": False,
                },
                "_artifact": {
                    "filename": "report.json",
                    "sha256": "c" * 64,
                    "size_bytes": 128,
                    "archive_path": "/state/report-c.json",
                },
            },
            actor="worker",
        )

        calls: list[tuple[str, dict]] = []

        def fake_call(operation: str, payload: dict) -> dict:
            calls.append((operation, payload))
            return {
                "ok": True,
                "operation": operation,
                "result": {"duplicate": False},
            }

        with patch(
            "sin_orca.simone_bridge.call_control_plane",
            side_effect=fake_call,
        ):
            result = sync_task(task_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["events_synced"], 2)
        self.assertEqual(result["artifacts_synced"], 1)
        self.assertTrue(result["idempotent"])
        self.assertIsInstance(result["last_event_hash"], str)
        self.assertEqual(
            [operation for operation, _ in calls],
            [
                "execution.bind",
                "execution.event",
                "execution.event",
                "execution.artifact",
            ],
        )

        report_event = calls[2][1]
        self.assertNotIn("_artifact", report_event["payload"])
        artifact = calls[3][1]
        self.assertEqual(artifact["sha256"], "c" * 64)
        self.assertEqual(
            artifact["reference"],
            "/state/report-c.json",
        )
        self.assertNotIn("content", artifact)

    def test_automatic_sync_only_runs_for_explicit_binding(self) -> None:
        unbound_id = "bridge-unbound-001"
        save_task({
            "task_id": unbound_id,
            "task_hash": "sha256:unbound",
            "repository_root": "/repo",
            "base_sha": "a" * 40,
            "role": "implementer",
        })
        with patch(
            "sin_orca.cli.sync_task_to_simone",
        ) as sync_call:
            self.assertIsNone(_sync_bound_task(unbound_id))
            sync_call.assert_not_called()

        bound_id = "bridge-bound-001"
        save_task({
            "task_id": bound_id,
            "simone_task_id": "TASK-SIMONE-BOUND",
            "task_hash": "sha256:bound",
            "repository_root": "/repo",
            "base_sha": "b" * 40,
            "role": "implementer",
        })
        with patch(
            "sin_orca.cli.sync_task_to_simone",
            return_value={
                "ok": True,
                "simone_task_id": "TASK-SIMONE-BOUND",
                "events_synced": 1,
                "event_duplicates": 0,
                "artifacts_synced": 0,
                "artifact_duplicates": 0,
                "last_event_hash": "c" * 64,
                "idempotent": True,
            },
        ) as sync_call:
            status = _sync_bound_task(bound_id)

        self.assertTrue(status["ok"])
        self.assertEqual(status["last_event_hash"], "c" * 64)
        sync_call.assert_called_once_with(
            bound_id,
            simone_task_id="TASK-SIMONE-BOUND",
        )


if __name__ == "__main__":
    unittest.main()
