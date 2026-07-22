"""Validated and crash-safe worker artifact ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from sin_context.evidence_firewall import wrap_evidence

from .state import (
    append_event,
    load_task,
    read_events,
    rebuild_ledger,
    task_dir,
)

ALLOWED_ARTIFACTS = {
    "checkpoint.json": "checkpoint.received",
    "report.json": "worker.report.received",
    "review.json": "review.completed",
}

DEFAULT_MAX_ARTIFACT_BYTES = 1024 * 1024


class ArtifactValidationError(RuntimeError):
    """Worker artifact is malformed or belongs to another task."""


def _maximum_artifact_bytes() -> int:
    return int(
        os.getenv(
            "SIN_ORCA_MAX_ARTIFACT_BYTES",
            str(DEFAULT_MAX_ARTIFACT_BYTES),
        )
    )


def _artifact_already_recorded(
    task_id: str,
    digest: str,
    filename: str,
    actor: str,
) -> bool:
    for event in read_events(task_id):
        artifact = event.get("payload", {}).get("_artifact", {})

        if (
            artifact.get("sha256") == digest
            and artifact.get("filename") == filename
            and event.get("actor") == actor
        ):
            return True

    return False


def _validate_identity(
    task: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    expected = {
        "task_id": task["task_id"],
        "task_hash": task["task_hash"],
        "base_sha": task["base_sha"],
    }

    for field, expected_value in expected.items():
        actual = payload.get(field)

        if actual != expected_value:
            raise ArtifactValidationError(
                f"{field} mismatch: expected "
                f"{expected_value!r}, received {actual!r}"
            )


def _validate_shape(
    filename: str,
    payload: dict[str, Any],
) -> None:
    if filename == "checkpoint.json":
        if not isinstance(payload.get("checkpoint"), str):
            raise ArtifactValidationError(
                "checkpoint requires checkpoint string"
            )

        if not isinstance(payload.get("sequence"), int):
            raise ArtifactValidationError(
                "checkpoint requires integer sequence"
            )

    elif filename == "report.json":
        if payload.get("status") != "complete":
            raise ArtifactValidationError(
                "worker report status must be complete"
            )

        for field in (
            "changed_files",
            "evidence",
            "unresolved",
        ):
            if not isinstance(payload.get(field), list):
                raise ArtifactValidationError(
                    f"worker report requires list field {field}"
                )

    elif filename == "review.json":
        if payload.get("verdict") not in {"accept", "reject"}:
            raise ArtifactValidationError(
                "review verdict must be accept or reject"
            )

        for field in (
            "criteria",
            "regressions",
            "unverified",
        ):
            if not isinstance(payload.get(field), list):
                raise ArtifactValidationError(
                    f"review requires list field {field}"
                )


def _validate_checkpoint_order(
    task_id: str,
    payload: dict[str, Any],
) -> None:
    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)

    required = list(task.get("required_checkpoints", []))
    received = [
        item.get("checkpoint")
        for item in ledger.get("checkpoints", [])
        if item.get("actor") == "worker"
    ]

    index = len(received)

    if index >= len(required):
        raise ArtifactValidationError(
            "all required checkpoints were already received"
        )

    expected = required[index]

    if payload.get("checkpoint") != expected:
        raise ArtifactValidationError(
            f"checkpoint out of order: expected "
            f"{expected!r}, received "
            f"{payload.get('checkpoint')!r}"
        )

    if payload.get("sequence") != index + 1:
        raise ArtifactValidationError(
            f"checkpoint sequence must be {index + 1}"
        )


def _archive_bytes(
    destination: Path,
    raw: bytes,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}"
    )

    with temporary.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, destination)


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _bounded_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item[:500]
        for item in value[:100]
        if isinstance(item, str)
    ]


def _artifact_summary(
    filename: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if filename == "checkpoint.json":
        return {
            "checkpoint": payload.get("checkpoint"),
            "sequence": payload.get("sequence"),
            "step_id": payload.get("step_id"),
            "status": payload.get("status"),
            "changed_files": _bounded_string_list(
                payload.get("changed_files")
            ),
            "unresolved_count": _list_count(
                payload.get("unresolved")
            ),
        }
    if filename == "report.json":
        return {
            "status": payload.get("status"),
            "changed_files": _bounded_string_list(
                payload.get("changed_files")
            ),
            "evidence_count": _list_count(payload.get("evidence")),
            "unresolved_count": _list_count(
                payload.get("unresolved")
            ),
            "scope_compliance": {
                key: value
                for key, value in (
                    payload.get("scope_compliance") or {}
                ).items()
                if key in {
                    "outside_allowlist_touched",
                    "unrequested_dependencies_added",
                }
                and isinstance(value, bool)
            }
            if isinstance(payload.get("scope_compliance"), dict)
            else {},
        }
    return {
        "verdict": payload.get("verdict"),
        "criteria_count": _list_count(payload.get("criteria")),
        "scope_violation": payload.get("scope_violation"),
        "regression_count": _list_count(payload.get("regressions")),
        "unverified_count": _list_count(payload.get("unverified")),
    }


def ingest_artifact(
    *,
    task_id: str,
    actor: str,
    outbox: Path,
    filename: str,
) -> dict[str, Any]:
    if filename not in ALLOWED_ARTIFACTS:
        raise ArtifactValidationError(
            f"unsupported artifact: {filename}"
        )

    source = (outbox / filename).resolve()
    expected_parent = outbox.resolve()

    try:
        source.relative_to(expected_parent)
    except ValueError as error:
        raise ArtifactValidationError(
            "artifact escaped the worker outbox"
        ) from error

    metadata = source.lstat()

    if stat.S_ISLNK(metadata.st_mode):
        raise ArtifactValidationError(
            "artifact must not be a symbolic link"
        )

    if not stat.S_ISREG(metadata.st_mode):
        raise ArtifactValidationError(
            "artifact must be a regular file"
        )

    maximum = _maximum_artifact_bytes()

    if metadata.st_size > maximum:
        raise ArtifactValidationError(
            f"artifact exceeds controller context limit: "
            f"{metadata.st_size} > {maximum}"
        )

    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    if _artifact_already_recorded(
        task_id,
        digest,
        filename,
        actor,
    ):
        source.unlink(missing_ok=True)

        return {
            "ok": True,
            "duplicate": True,
            "filename": filename,
            "sha256": digest,
        }

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactValidationError(
            "artifact is not valid UTF-8 JSON"
        ) from error

    if not isinstance(payload, dict):
        raise ArtifactValidationError(
            "artifact root must be an object"
        )

    task = load_task(task_id)

    _validate_identity(task, payload)
    _validate_shape(filename, payload)

    if filename == "checkpoint.json":
        _validate_checkpoint_order(task_id, payload)

    archive = (
        task_dir(task_id)
        / "artifacts"
        / actor
        / f"{Path(filename).stem}-{digest}.json"
    )

    if not archive.exists():
        _archive_bytes(archive, raw)

    envelope = wrap_evidence(
        source=str(archive),
        source_type=f"{actor}-artifact",
        content=raw.decode("utf-8"),
        trust_level="external-untrusted",
        metadata={"filename": filename, "actor": actor},
    )
    event_payload = {
        **payload,
        "_artifact": {
            "filename": filename,
            "sha256": digest,
            "size_bytes": len(raw),
            "archive_path": str(archive),
        },
        "_evidence": {
            "trust_level": envelope.trust_level,
            "source_type": envelope.source_type,
            "sha256": envelope.sha256,
            "suspicious_instruction_spans": len(envelope.suspicious),
            "suspicious_lines": sorted(
                {span.line for span in envelope.suspicious}
            ),
        },
    }

    append_event(
        task_id,
        ALLOWED_ARTIFACTS[filename],
        event_payload,
        actor=actor,
    )

    source.unlink()

    return {
        "ok": True,
        "duplicate": False,
        "filename": filename,
        "sha256": digest,
        "archive_path": str(archive),
        "trust_level": envelope.trust_level,
        "suspicious_instruction_spans": len(envelope.suspicious),
        "payload_summary": _artifact_summary(filename, payload),
    }
