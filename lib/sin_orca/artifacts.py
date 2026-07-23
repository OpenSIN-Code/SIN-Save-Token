"""Validated and crash-safe worker artifact ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from sin_context.evidence_firewall import wrap_evidence

from .state import (
    append_event,
    atomic_write_json,
    load_task,
    read_events,
    rebuild_ledger,
    task_dir,
    utc_now,
)
from .verification import redact_argv, redact_text

ALLOWED_ARTIFACTS = {
    "checkpoint.json": "checkpoint.received",
    "report.json": "worker.report.received",
    "review.json": "review.completed",
}
ARTIFACT_ACTORS = {
    "checkpoint.json": "worker",
    "report.json": "worker",
    "review.json": "reviewer",
}

DEFAULT_MAX_ARTIFACT_BYTES = 1024 * 1024


class ArtifactValidationError(RuntimeError):
    """Worker artifact is malformed or belongs to another task."""


def _maximum_artifact_bytes() -> int:
    raw = os.getenv(
        "SIN_ORCA_MAX_ARTIFACT_BYTES",
        str(DEFAULT_MAX_ARTIFACT_BYTES),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_ARTIFACT_BYTES
    if value <= 0:
        value = DEFAULT_MAX_ARTIFACT_BYTES
    return min(value, 16 * 1024 * 1024)


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

        for field in ("step_id", "status"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise ArtifactValidationError(
                    f"checkpoint requires non-empty string {field}"
                )
        for field in ("changed_files", "commands", "unresolved"):
            if not isinstance(payload.get(field), list):
                raise ArtifactValidationError(
                    f"checkpoint requires list field {field}"
                )
        if not isinstance(payload.get("child_process_running"), bool):
            raise ArtifactValidationError(
                "checkpoint requires child_process_running boolean"
            )

    elif filename == "report.json":
        if payload.get("status") != "complete":
            raise ArtifactValidationError(
                "worker report status must be complete"
            )

        for field in (
            "changed_files",
            "evidence",
            "commands",
            "unresolved",
        ):
            if not isinstance(payload.get(field), list):
                raise ArtifactValidationError(
                    f"worker report requires list field {field}"
                )

        scope = payload.get("scope_compliance")
        if not isinstance(scope, dict):
            raise ArtifactValidationError(
                "worker report requires scope_compliance object"
            )
        for field in (
            "outside_allowlist_touched",
            "unrequested_dependencies_added",
            "architecture_decisions_made",
        ):
            if not isinstance(scope.get(field), bool):
                raise ArtifactValidationError(
                    f"scope_compliance requires boolean {field}"
                )

    elif filename == "review.json":
        if payload.get("verdict") not in {"accept", "reject"}:
            raise ArtifactValidationError(
                "review verdict must be accept or reject"
            )

        if not isinstance(payload.get("diff_sha256"), str) or not payload.get(
            "diff_sha256"
        ):
            raise ArtifactValidationError(
                "review requires diff_sha256 string"
            )
        if not isinstance(payload.get("scope_violation"), bool):
            raise ArtifactValidationError(
                "review requires scope_violation boolean"
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

        for criterion in payload["criteria"]:
            if not isinstance(criterion, dict):
                raise ArtifactValidationError(
                    "review criteria entries must be objects"
                )
            if criterion.get("status") not in {
                "proven", "failed", "unverified"
            }:
                raise ArtifactValidationError(
                    "review criterion status is invalid"
                )
            evidence = criterion.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ArtifactValidationError(
                    "review criterion requires non-empty evidence string"
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
    approved_steps = [
        str(message.get("step_id"))
        for message in ledger.get("controller_messages", [])
        if message.get("type") == "codex.approved"
        and message.get("step_id")
    ]
    approval_mode = task.get("approval_mode", "stepwise")
    if approval_mode == "stepwise":
        if len(approved_steps) != index:
            raise ArtifactValidationError(
                "checkpoint arrived before the preceding protected step approval"
            )
    elif approval_mode == "continuous-preauthorized":
        if approved_steps:
            raise ArtifactValidationError(
                "continuous-preauthorized task contains unexpected approvals"
            )
    else:
        raise ArtifactValidationError(
            f"unsupported task approval mode: {approval_mode!r}"
        )

    steps = [
        step for step in task.get("steps", [])
        if isinstance(step, dict) and step.get("id")
    ]
    if index < len(steps) and payload.get("step_id") != str(steps[index]["id"]):
        raise ArtifactValidationError(
            f"checkpoint step_id must be {steps[index]['id']!r}"
        )

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


def _validate_report_protocol(
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
    if received != required:
        raise ArtifactValidationError(
            "worker report arrived before all checkpoints"
        )

    expected_steps = [
        str(step.get("id"))
        for step in task.get("steps", [])
        if isinstance(step, dict) and step.get("id")
    ]
    approved_steps = [
        str(message.get("step_id"))
        for message in ledger.get("controller_messages", [])
        if message.get("type") == "codex.approved"
        and message.get("step_id")
    ]
    approval_mode = task.get("approval_mode", "stepwise")
    if approval_mode == "stepwise":
        if approved_steps != expected_steps:
            raise ArtifactValidationError(
                "worker report arrived before every protected step was approved in order"
            )
    elif approval_mode == "continuous-preauthorized":
        if approved_steps:
            raise ArtifactValidationError(
                "continuous-preauthorized task contains unexpected approvals"
            )
    else:
        raise ArtifactValidationError(
            f"unsupported task approval mode: {approval_mode!r}"
        )

    if task.get("role") == "implementer" and not payload.get("evidence"):
        raise ArtifactValidationError(
            "implementer report requires non-empty evidence"
        )


def _validate_review_protocol(
    task_id: str,
    payload: dict[str, Any],
) -> None:
    ledger = rebuild_ledger(task_id)
    worker = ledger.get("actors", {}).get("worker")
    reviewer = ledger.get("actors", {}).get("reviewer")

    if not isinstance(worker, dict) or not isinstance(reviewer, dict):
        raise ArtifactValidationError(
            "review actors are incomplete"
        )
    if reviewer.get("agent") == worker.get("agent"):
        raise ArtifactValidationError(
            "reviewer must use a different agent than implementer"
        )
    if reviewer.get("same_worktree") is not True:
        raise ArtifactValidationError(
            "reviewer must inspect the implementer worktree"
        )
    expected_diff = reviewer.get("diff_sha256")
    if not isinstance(expected_diff, str) or (
        payload.get("diff_sha256") != expected_diff
    ):
        raise ArtifactValidationError(
            "review diff_sha256 does not match reviewer assignment"
        )


def _command_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(redact_argv([str(item) for item in value]))[:2_000]
    return redact_text(str(value or ""))[:2_000]


def _record_command_history(
    task_id: str,
    payload: dict[str, Any],
) -> None:
    commands = payload.get("commands")
    if not isinstance(commands, list):
        return

    history_path = task_dir(task_id) / "command-history.json"
    history: list[dict[str, Any]] = []
    if history_path.is_file():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = [item for item in loaded if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            history = []

    for entry in commands[:100]:
        if not isinstance(entry, dict):
            continue
        exit_code = entry.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            continue
        error_text = str(entry.get("error") or entry.get("stderr") or "")
        history.append({
            "command": _command_text(entry.get("command")),
            "exit_code": exit_code,
            "error_hash": hashlib.sha256(
                error_text.encode("utf-8")
            ).hexdigest(),
            "recorded_at": utc_now(),
        })

    atomic_write_json(history_path, history[-500:])


def _record_artifact_activity(
    task_id: str,
    actor: str,
    payload: dict[str, Any],
) -> None:
    activity_path = task_dir(task_id) / "activity.json"
    activity: dict[str, Any] = {}
    if activity_path.is_file():
        try:
            loaded = json.loads(activity_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                activity = loaded
        except (OSError, json.JSONDecodeError):
            activity = {}

    activity[f"{actor}_last_active"] = utc_now()
    child_running = payload.get("child_process_running")
    if isinstance(child_running, bool):
        activity[f"{actor}_child_process_running"] = child_running
    atomic_write_json(activity_path, activity)


def _read_artifact_bytes(
    source: Path,
    maximum: int,
) -> bytes:
    try:
        metadata = source.lstat()
    except FileNotFoundError as error:
        raise ArtifactValidationError(
            "artifact disappeared before ingestion"
        ) from error

    if stat.S_ISLNK(metadata.st_mode):
        raise ArtifactValidationError(
            "artifact must not be a symbolic link"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise ArtifactValidationError(
            "artifact must be a regular file"
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ArtifactValidationError(
            "artifact could not be opened safely"
        ) from error

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ArtifactValidationError(
                "artifact changed type during ingestion"
            )
        if opened.st_size > maximum:
            raise ArtifactValidationError(
                f"artifact exceeds controller context limit: "
                f"{opened.st_size} > {maximum}"
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ArtifactValidationError(
                    "artifact grew beyond controller context limit"
                )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _archive_bytes(
    destination: Path,
    raw: bytes,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )

    with temporary.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass


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
                    "architecture_decisions_made",
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
    expected_actor = ARTIFACT_ACTORS[filename]
    if actor != expected_actor:
        raise ArtifactValidationError(
            f"{filename} must be produced by {expected_actor}"
        )

    expected_parent = outbox.resolve()
    source = expected_parent / filename

    try:
        source.relative_to(expected_parent)
    except ValueError as error:
        raise ArtifactValidationError(
            "artifact escaped the worker outbox"
        ) from error

    maximum = _maximum_artifact_bytes()
    raw = _read_artifact_bytes(source, maximum)
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
    elif filename == "report.json":
        _validate_report_protocol(task_id, payload)
    elif filename == "review.json":
        _validate_review_protocol(task_id, payload)

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
    _record_command_history(task_id, payload)
    _record_artifact_activity(task_id, actor, payload)

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
