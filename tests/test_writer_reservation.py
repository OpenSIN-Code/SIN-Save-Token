#!/usr/bin/env python3
"""Hermetic repository writer-reservation contracts."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from sin_orca.state import append_event, events_path, save_task  # noqa: E402
from sin_orca.writer_reservation import (  # noqa: E402
    WriterReservationConflict,
    WriterReservationLost,
    acquire_writer,
    release_writer,
    reservation_path,
    reservation_status,
)


class WriterReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.patch = patch(
            "sin_orca.state.state_root",
            lambda *args, **kwargs: self.state,
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def create_live_task(self, task_id: str) -> None:
        save_task({
            "task_id": task_id,
            "task_hash": f"sha256:{task_id}",
            "repository_root": str(self.repository),
            "base_sha": "a" * 40,
            "role": "implementer",
        })
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": f"sha256:{task_id}",
                "base_sha": "a" * 40,
                "role": "implementer",
            },
            actor="codex",
        )

    def test_second_live_writer_is_rejected(self) -> None:
        self.create_live_task("writer-a")
        self.create_live_task("writer-b")
        first = acquire_writer(self.repository, task_id="writer-a")
        self.assertEqual(first["task_id"], "writer-a")

        with self.assertRaisesRegex(
            WriterReservationConflict,
            "writer-a",
        ):
            acquire_writer(self.repository, task_id="writer-b")

    def test_cancelled_owner_is_reclaimed(self) -> None:
        self.create_live_task("writer-a")
        self.create_live_task("writer-b")
        acquire_writer(self.repository, task_id="writer-a")
        append_event(
            "writer-a",
            "task.cancelled",
            {"reason": "test"},
            actor="codex",
        )

        second = acquire_writer(self.repository, task_id="writer-b")
        self.assertEqual(second["task_id"], "writer-b")
        self.assertEqual(reservation_status(self.repository)["task_id"], "writer-b")

    def test_missing_owner_state_is_reclaimed(self) -> None:
        acquire_writer(self.repository, task_id="missing-owner")
        self.create_live_task("writer-b")

        second = acquire_writer(self.repository, task_id="writer-b")
        self.assertEqual(second["task_id"], "writer-b")

    def test_corrupt_owner_state_blocks_reclaim(self) -> None:
        self.create_live_task("writer-a")
        self.create_live_task("writer-b")
        acquire_writer(self.repository, task_id="writer-a")
        path = events_path("writer-a")
        path.write_text("{not-json}\n", encoding="utf-8")

        with self.assertRaisesRegex(
            WriterReservationConflict,
            "owner state is invalid",
        ):
            acquire_writer(self.repository, task_id="writer-b")
        self.assertEqual(
            reservation_status(self.repository)["task_id"],
            "writer-a",
        )

    def test_reservation_repository_mismatch_is_rejected(self) -> None:
        self.create_live_task("writer-a")
        acquire_writer(self.repository, task_id="writer-a")
        path = reservation_path(self.repository)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["repository_root"] = str(self.root / "other")
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(
            WriterReservationConflict,
            "repository identity mismatch",
        ):
            reservation_status(self.repository)

    def test_completed_owner_is_reclaimed(self) -> None:
        self.create_live_task("writer-a")
        self.create_live_task("writer-b")
        acquire_writer(self.repository, task_id="writer-a")
        append_event(
            "writer-a",
            "task.completed",
            {"changed_files": []},
            actor="controller",
        )

        second = acquire_writer(self.repository, task_id="writer-b")
        self.assertEqual(second["task_id"], "writer-b")

    def test_wrong_owner_cannot_release(self) -> None:
        self.create_live_task("writer-a")
        acquire_writer(self.repository, task_id="writer-a")
        with self.assertRaises(WriterReservationLost):
            release_writer(self.repository, task_id="writer-b")
        self.assertEqual(reservation_status(self.repository)["task_id"], "writer-a")

    def test_release_is_idempotent_for_owner(self) -> None:
        self.create_live_task("writer-a")
        acquire_writer(self.repository, task_id="writer-a")
        self.assertTrue(release_writer(self.repository, task_id="writer-a"))
        self.assertFalse(release_writer(self.repository, task_id="writer-a"))
        self.assertIsNone(reservation_status(self.repository))

    def test_state_is_external_and_private(self) -> None:
        self.create_live_task("writer-a")
        acquire_writer(self.repository, task_id="writer-a")
        path = reservation_path(self.repository)
        self.assertTrue(path.is_file())
        self.assertFalse(path.is_relative_to(self.repository))
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
