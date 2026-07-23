"""Repository-wide single-writer reservation for same-worktree Orca tasks."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import state


class WriterReservationConflict(RuntimeError):
    """Another live task already owns the repository writer."""


class WriterReservationLost(RuntimeError):
    """The caller attempted to release another task's reservation."""


def reservation_path(repository: Path) -> Path:
    return state.state_root(repository) / "writer-reservation.json"


def lock_path(repository: Path) -> Path:
    return state.state_root(repository) / "writer-reservation.lock"


@contextmanager
def reservation_lock(repository: Path) -> Iterator[None]:
    path = lock_path(repository)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def read_reservation(repository: Path) -> dict[str, Any] | None:
    repository = repository.resolve()
    path = reservation_path(repository)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WriterReservationConflict(
            "writer reservation state is unreadable; inspect it manually"
        ) from error
    if not isinstance(value, dict):
        raise WriterReservationConflict(
            "writer reservation state is invalid; inspect it manually"
        )
    if value.get("repository_root") != str(repository):
        raise WriterReservationConflict(
            "writer reservation repository identity mismatch"
        )
    owner = value.get("task_id")
    if not isinstance(owner, str) or not owner:
        raise WriterReservationConflict(
            "writer reservation owner is invalid"
        )
    if value.get("mode") != "exclusive-writer":
        raise WriterReservationConflict(
            "writer reservation mode is invalid"
        )
    return value


def _owner_is_terminal(owner_task_id: str) -> bool:
    try:
        ledger = state.rebuild_ledger(owner_task_id)
    except FileNotFoundError:
        return True
    except (RuntimeError, ValueError) as error:
        raise WriterReservationConflict(
            "writer owner state is invalid; explicit inspection or cancellation is required"
        ) from error
    return ledger.get("status") in {"completed", "cancelled"}


def acquire_writer(
    repository: Path,
    *,
    task_id: str,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    repository = repository.resolve()
    with reservation_lock(repository):
        current = read_reservation(repository)
        if current is not None:
            owner = current.get("task_id")
            if owner == task_id:
                return current
            if isinstance(owner, str) and owner and not _owner_is_terminal(owner):
                raise WriterReservationConflict(
                    "repository writer is already reserved by live task "
                    f"{owner!r}"
                )

        reservation = {
            "schema_version": 1,
            "reservation_id": uuid.uuid4().hex,
            "repository_root": str(repository),
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "acquired_at": state.utc_now(),
            "mode": "exclusive-writer",
        }
        state.atomic_write_json(reservation_path(repository), reservation)
        return reservation


def release_writer(
    repository: Path,
    *,
    task_id: str,
    allow_missing: bool = True,
) -> bool:
    repository = repository.resolve()
    with reservation_lock(repository):
        current = read_reservation(repository)
        if current is None:
            if allow_missing:
                return False
            raise WriterReservationLost("writer reservation is missing")
        owner = current.get("task_id")
        if owner != task_id:
            raise WriterReservationLost(
                f"writer reservation belongs to {owner!r}, not {task_id!r}"
            )
        reservation_path(repository).unlink(missing_ok=True)
        return True


def reservation_status(repository: Path) -> dict[str, Any] | None:
    repository = repository.resolve()
    with reservation_lock(repository):
        current = read_reservation(repository)
        if current is None:
            return None
        return dict(current)
