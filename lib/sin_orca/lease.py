"""Exclusive semantic ownership for a running orchestration task."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class LeaseConflictError(RuntimeError):
    """Another live controller currently owns the task."""


class LeaseLostError(RuntimeError):
    """The caller no longer owns the controller lease."""


@dataclass(frozen=True)
class Lease:
    owner: str
    token: str
    acquired_at: float
    renewed_at: float
    expires_at: float


def controller_identity() -> str:
    configured = os.getenv("SIN_ORCA_CONTROLLER_ID")

    if configured:
        return configured

    session = (
        os.getenv("CODEX_THREAD_ID")
        or os.getenv("TERM_SESSION_ID")
        or os.getenv("TMUX_PANE")
        or str(os.getppid())
    )

    return (
        f"{socket.gethostname()}:"
        f"{os.getuid()}:"
        f"{session}"
    )


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}"
    )

    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, path)


class ControllerLease:
    def __init__(
        self,
        task_directory: Path,
        *,
        owner: str | None = None,
    ) -> None:
        self.task_directory = task_directory.resolve()
        self.owner = owner or controller_identity()
        self.lease_path = self.task_directory / "controller-lease.json"
        self.lock_path = self.task_directory / ".controller-lease.lock"

    def _load(self) -> Lease | None:
        if not self.lease_path.is_file():
            return None

        try:
            value = json.loads(
                self.lease_path.read_text(encoding="utf-8")
            )

            return Lease(
                owner=str(value["owner"]),
                token=str(value["token"]),
                acquired_at=float(value["acquired_at"]),
                renewed_at=float(value["renewed_at"]),
                expires_at=float(value["expires_at"]),
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ):
            return None

    def acquire(
        self,
        *,
        ttl_seconds: int = 180,
    ) -> Lease:
        if ttl_seconds < 30:
            raise ValueError("lease TTL must be at least 30 seconds")

        self.task_directory.mkdir(parents=True, exist_ok=True)

        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            now = time.time()
            current = self._load()

            if (
                current is not None
                and current.expires_at > now
                and current.owner != self.owner
            ):
                raise LeaseConflictError(
                    f"task is controlled by {current.owner!r} "
                    f"until {current.expires_at}"
                )

            if current is not None and current.owner == self.owner:
                token = current.token
                acquired_at = current.acquired_at
            else:
                token = uuid.uuid4().hex
                acquired_at = now

            lease = Lease(
                owner=self.owner,
                token=token,
                acquired_at=acquired_at,
                renewed_at=now,
                expires_at=now + ttl_seconds,
            )

            _atomic_write(
                self.lease_path,
                asdict(lease),
            )

            return lease

    def assert_owned(self, token: str) -> Lease:
        current = self._load()
        now = time.time()

        if current is None:
            raise LeaseLostError("controller lease does not exist")

        if current.expires_at <= now:
            raise LeaseLostError("controller lease has expired")

        if current.owner != self.owner:
            raise LeaseLostError(
                f"controller lease belongs to {current.owner!r}"
            )

        if current.token != token:
            raise LeaseLostError("controller lease token changed")

        return current

    def renew(
        self,
        token: str,
        *,
        ttl_seconds: int = 180,
    ) -> Lease:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            current = self.assert_owned(token)
            now = time.time()

            renewed = Lease(
                owner=current.owner,
                token=current.token,
                acquired_at=current.acquired_at,
                renewed_at=now,
                expires_at=now + ttl_seconds,
            )

            _atomic_write(
                self.lease_path,
                asdict(renewed),
            )

            return renewed

    def release(self, token: str) -> None:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            self.assert_owned(token)

            try:
                self.lease_path.unlink()
            except FileNotFoundError:
                pass
