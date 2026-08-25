"""Fail-closed SIN memory governance over the canonical OpenViking backend.

OpenViking is the only semantic memory/context source of truth.  This module is
intentionally a control plane, not a second brain: it validates evidence and
secret/speculation policy, submits a bounded OpenViking session commit, waits
for the exact commit task to finish, and returns an opaque persistence receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

SCHEMA_VERSION = 2
SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

SPECULATIVE_MARKERS = (
    "speculative",
    "unverified",
    "guess",
    "maybe",
    "i think",
    "probably",
    "not sure",
    "tbd",
)
SECRET_SHAPED_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|passwd|password|token|bearer)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class RecordStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INACTIVE = "inactive"


class ReceiptStatus(str, Enum):
    COMMITTED = "committed"
    FAILED = "failed"


class GatewayError(Exception):
    """Base class for fail-closed gateway errors."""


class RejectedRecordError(GatewayError):
    """Record failed validation; no backend call is permitted."""


class BackendFailureError(GatewayError):
    """Canonical backend did not prove a completed operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "SIN_MANIFEST_HMAC_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ):
        environment.pop(key, None)
    return environment


@dataclass(frozen=True)
class CanonicalMemoryRecord:
    record_id: str
    content: str
    provenance: dict[str, str]
    created_at: str
    status: RecordStatus = RecordStatus.ACTIVE
    schema_version: int = SCHEMA_VERSION
    supersedes: Optional[str] = None

    def canonical_bytes(self) -> bytes:
        payload = {
            "record_id": self.record_id,
            "content": self.content,
            "provenance": dict(sorted(self.provenance.items())),
            "created_at": self.created_at,
            "status": self.status.value,
            "schema_version": self.schema_version,
            "supersedes": self.supersedes,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def validate_provenance(provenance: Any) -> dict[str, str]:
    if not isinstance(provenance, dict):
        raise RejectedRecordError("provenance must be a mapping")
    for key in ("source", "evidence_sha256", "actor"):
        if key not in provenance:
            raise RejectedRecordError(f"provenance missing required field: {key}")
    for key in ("source", "actor"):
        value = provenance[key]
        if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
            raise RejectedRecordError(f"provenance.{key} must be a safe identifier")
    evidence = provenance["evidence_sha256"]
    if not isinstance(evidence, str) or not HEX_SHA256.fullmatch(evidence):
        raise RejectedRecordError(
            "provenance.evidence_sha256 must be a 64-char lowercase hex sha256"
        )
    extra = set(provenance) - {"source", "evidence_sha256", "actor"}
    if extra:
        raise RejectedRecordError(f"provenance has unexpected fields: {sorted(extra)}")
    return {key: provenance[key] for key in ("source", "evidence_sha256", "actor")}


def validate_content(content: Any) -> str:
    if not isinstance(content, str):
        raise RejectedRecordError("content must be a string")
    stripped = content.strip()
    if not stripped:
        raise RejectedRecordError("content must not be empty")
    lowered = stripped.lower()
    for marker in SPECULATIVE_MARKERS:
        if marker in lowered:
            raise RejectedRecordError(
                f"speculative content rejected (marker: {marker!r})"
            )
    for pattern in SECRET_SHAPED_PATTERNS:
        if pattern.search(stripped):
            raise RejectedRecordError(
                "secret-shaped content rejected; redact before committing"
            )
    return stripped


def build_canonical_record(
    record_id: str,
    content: str,
    provenance: dict[str, str],
    supersedes: Optional[str] = None,
) -> CanonicalMemoryRecord:
    if not isinstance(record_id, str) or not SAFE_IDENTIFIER.fullmatch(record_id):
        raise RejectedRecordError("record_id must be a safe identifier")
    if supersedes is not None and (
        not isinstance(supersedes, str) or not SAFE_IDENTIFIER.fullmatch(supersedes)
    ):
        raise RejectedRecordError("supersedes must be a safe identifier")
    return CanonicalMemoryRecord(
        record_id=record_id,
        content=validate_content(content),
        provenance=validate_provenance(provenance),
        created_at=_utc_now(),
        supersedes=supersedes,
    )


@dataclass(frozen=True)
class PersistenceReceipt:
    receipt_id: str
    record_id: str
    record_hash: str
    status: ReceiptStatus
    backend: str
    committed_at: Optional[str] = None
    backend_ref: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status is ReceiptStatus.COMMITTED


@dataclass
class CommitResult:
    accepted: bool
    receipt: Optional[PersistenceReceipt]
    reason: Optional[str] = None


class MemoryBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    def commit(self, record: CanonicalMemoryRecord) -> PersistenceReceipt:
        """Persist a record and return a typed receipt."""

    @abstractmethod
    def recall(
        self,
        query: str,
        statuses: tuple[RecordStatus, ...] = (RecordStatus.ACTIVE,),
    ) -> list[CanonicalMemoryRecord]:
        """Return matching records limited to the requested lifecycle states."""


class InMemoryBackend(MemoryBackend):
    """Hermetic backend used only by tests."""

    name = "in-memory"

    def __init__(self, fail_commits: bool = False) -> None:
        self.fail_commits = fail_commits
        self.records: dict[str, CanonicalMemoryRecord] = {}

    def commit(self, record: CanonicalMemoryRecord) -> PersistenceReceipt:
        if self.fail_commits:
            raise BackendFailureError("simulated backend outage")
        self.records[record.record_id] = record
        return PersistenceReceipt(
            receipt_id=str(uuid.uuid4()),
            record_id=record.record_id,
            record_hash=record.content_hash(),
            status=ReceiptStatus.COMMITTED,
            backend=self.name,
            committed_at=_utc_now(),
            backend_ref=f"memory:{record.record_id}",
        )

    def recall(
        self,
        query: str,
        statuses: tuple[RecordStatus, ...] = (RecordStatus.ACTIVE,),
    ) -> list[CanonicalMemoryRecord]:
        needle = query.strip().lower()
        matches = []
        for record in self.records.values():
            if record.status not in statuses:
                continue
            if needle in record.content.lower() or needle in record.record_id.lower():
                matches.append(record)
        return sorted(matches, key=lambda record: record.created_at)


class OpenVikingCLIBackend(MemoryBackend):
    """Canonical backend adapter using OpenViking's public ``ov`` CLI.

    A write is not acknowledged merely because the HTTP commit was accepted.
    The adapter waits for the exact OpenViking session-commit task and returns a
    COMMITTED receipt only after that task reaches ``completed``.
    """

    name = "openviking"
    TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}

    def __init__(
        self,
        *,
        executable: str | None = None,
        account: str = "sin-fleet",
        user: str = "default",
        agent_id: str = "sin-memory-gateway",
        timeout_seconds: int = 180,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        resolved = executable or shutil.which("ov")
        if not resolved:
            raise BackendFailureError("OpenViking `ov` CLI is not installed on PATH")
        for label, value in (("account", account), ("user", user), ("agent_id", agent_id)):
            if not SAFE_IDENTIFIER.fullmatch(value):
                raise BackendFailureError(f"OpenViking {label} is not a safe identifier")
        self.executable = resolved
        self.account = account
        self.user = user
        self.agent_id = agent_id
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def _base(self) -> list[str]:
        return [
            self.executable,
            "-o",
            "json",
            "--account",
            self.account,
            "--user",
            self.user,
            "--agent-id",
            self.agent_id,
        ]

    def _run_json(self, args: list[str], *, timeout: int | None = None) -> Any:
        try:
            process = subprocess.run(
                [*self._base(), *args],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout or self.timeout_seconds,
                env=_safe_child_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendFailureError(f"OpenViking transport failed: {error}") from error
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "unknown error"
            raise BackendFailureError(f"OpenViking command failed: {detail}")
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise BackendFailureError("OpenViking returned non-JSON output") from error
        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("status") in {"error", "failed"}:
                raise BackendFailureError(f"OpenViking rejected operation: {payload}")
            if "result" in payload:
                return payload["result"]
        return payload

    @staticmethod
    def _extract_field(payload: Any, field: str) -> str:
        if isinstance(payload, dict):
            value = payload.get(field)
            if value is not None:
                return str(value)
            nested = payload.get("result")
            if isinstance(nested, dict) and nested.get(field) is not None:
                return str(nested[field])
        return ""

    def _wait_for_task(self, task_id: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendFailureError(
                    f"OpenViking commit task timed out: {task_id}"
                )
            task = self._run_json(
                ["task", "status", task_id],
                timeout=max(1, min(int(remaining) + 1, 30)),
            )
            status = self._extract_field(task, "status").lower()
            if status == "completed":
                return
            if status in {"failed", "cancelled"}:
                error = self._extract_field(task, "error") or status
                raise BackendFailureError(
                    f"OpenViking commit task {task_id} ended {status}: {error}"
                )
            time.sleep(min(self.poll_interval_seconds, max(0.05, remaining)))

    def commit(self, record: CanonicalMemoryRecord) -> PersistenceReceipt:
        session = self._run_json(["session", "new"])
        session_id = self._extract_field(session, "session_id")
        if not session_id:
            raise BackendFailureError("OpenViking did not return a session_id")

        message = json.dumps(
            {
                "schema": "sin-memory-record/v2",
                "record_id": record.record_id,
                "statement": record.content,
                "provenance": record.provenance,
                "supersedes": record.supersedes,
                "instruction": (
                    "Store the verified statement as durable long-term memory. "
                    "If supersedes is set, treat the new statement as current and the "
                    "superseded statement as historical."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self._run_json(
            [
                "session",
                "add-message",
                session_id,
                "--role",
                "user",
                "--content",
                message,
            ]
        )
        committed = self._run_json(["session", "commit", session_id])
        commit_status = self._extract_field(committed, "status").lower()
        task_id = self._extract_field(committed, "task_id")
        if commit_status == "skipped":
            raise BackendFailureError("OpenViking skipped a non-empty memory commit")
        if not task_id:
            raise BackendFailureError("OpenViking commit returned no task_id")
        self._wait_for_task(task_id)

        return PersistenceReceipt(
            receipt_id=str(uuid.uuid4()),
            record_id=record.record_id,
            record_hash=record.content_hash(),
            status=ReceiptStatus.COMMITTED,
            backend=self.name,
            committed_at=_utc_now(),
            backend_ref=f"session:{session_id};task:{task_id}",
        )

    def recall(
        self,
        query: str,
        statuses: tuple[RecordStatus, ...] = (RecordStatus.ACTIVE,),
    ) -> list[CanonicalMemoryRecord]:
        if RecordStatus.ACTIVE not in statuses:
            return []
        result = self._run_json(
            ["search", query, "--node-limit", "10", "--level", "0", "--level", "1"]
        )
        memories = result.get("memories", []) if isinstance(result, dict) else []
        records: list[CanonicalMemoryRecord] = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri") or "")
            content = str(
                item.get("overview")
                or item.get("abstract")
                or item.get("content")
                or uri
            ).strip()
            if not content:
                continue
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
            records.append(
                CanonicalMemoryRecord(
                    record_id="ov-" + hashlib.sha256(uri.encode("utf-8")).hexdigest()[:20],
                    content=content,
                    provenance={
                        "source": "openviking",
                        "evidence_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        "actor": self.agent_id,
                    },
                    created_at=_utc_now(),
                )
            )
        return records


class SinMemoryGateway:
    """Validation and receipt gate around exactly one canonical backend."""

    def __init__(self, backend: MemoryBackend) -> None:
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def commit_record(
        self,
        record_id: str,
        content: str,
        provenance: dict[str, str],
        supersedes: Optional[str] = None,
    ) -> CommitResult:
        try:
            record = build_canonical_record(
                record_id=record_id,
                content=content,
                provenance=provenance,
                supersedes=supersedes,
            )
        except RejectedRecordError as exc:
            return CommitResult(accepted=False, receipt=None, reason=str(exc))
        try:
            receipt = self._backend.commit(record)
        except Exception as exc:  # fail closed on every backend error
            return CommitResult(
                accepted=False,
                receipt=None,
                reason=f"backend failure: {exc}",
            )
        if receipt.status is not ReceiptStatus.COMMITTED:
            return CommitResult(
                accepted=False,
                receipt=receipt,
                reason="backend did not confirm commit",
            )
        return CommitResult(accepted=True, receipt=receipt)

    def recall_records(
        self,
        query: str,
        statuses: tuple[RecordStatus, ...] = (RecordStatus.ACTIVE,),
    ) -> list[CanonicalMemoryRecord]:
        try:
            return self._backend.recall(query, statuses=statuses)
        except Exception as exc:
            raise BackendFailureError(f"recall failed: {exc}") from exc

    def supersede_record(
        self,
        record_id: str,
        new_record_id: str,
        new_content: str,
        provenance: dict[str, str],
    ) -> CommitResult:
        result = self.commit_record(
            record_id=new_record_id,
            content=new_content,
            provenance=provenance,
            supersedes=record_id,
        )
        if result.accepted:
            old = getattr(self._backend, "records", {}).get(record_id)
            if old is not None:
                self._backend.records[record_id] = CanonicalMemoryRecord(
                    record_id=old.record_id,
                    content=old.content,
                    provenance=old.provenance,
                    created_at=old.created_at,
                    status=RecordStatus.SUPERSEDED,
                    supersedes=None,
                )
        return result
