"""Idempotent replay of compact sin-orca execution facts into Simone."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .state import load_task, read_events
from .verification import redact_text

SOURCE = "sin-orca"
MAX_STRING_CHARS = 2_000
MAX_LIST_ITEMS = 100

_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "task.created": ("task_hash", "base_sha", "role"),
    "worker.spawned": (
        "agent",
        "worktree_id",
        "branch",
        "worktree_selector",
        "terminal_handle",
    ),
    "checkpoint.received": (
        "checkpoint",
        "sequence",
        "step_id",
        "status",
        "changed_files",
        "scope_compliance",
    ),
    "codex.approved": ("step_id", "instruction"),
    "codex.sent": ("text",),
    "codex.interrupted": ("text", "control_c_sent"),
    "codex.followup": ("step_id", "instruction"),
    "worker.callback": (
        "callback_type",
        "step_id",
        "summary",
        "changed_files",
        "verification_status",
        "requested_action",
    ),
    "reviewer.callback": (
        "callback_type",
        "step_id",
        "summary",
        "changed_files",
        "verification_status",
        "requested_action",
    ),
    "task.suspended": ("reason",),
    "task.resumed": ("instruction",),
    "worker.report.received": (
        "status",
        "changed_files",
        "scope_compliance",
    ),
    "verification.completed": (
        "ok",
        "changed_files",
        "scope_errors",
        "results",
    ),
    "review.completed": (
        "verdict",
        "scope_violation",
    ),
    "worker.blocked": (
        "reason",
        "requires_codex_intervention",
        "identical_failures",
        "command",
    ),
    "worker.stalled": (
        "idle_seconds",
        "action_required",
    ),
    "worker.recovered": ("actor",),
    "task.completed": ("changed_files",),
    "task.cancelled": ("reason", "interrupted_actors"),
    "task.failed": ("stage", "error"),
}

_FORBIDDEN_KEYS = {
    "diff",
    "full_diff",
    "raw_diff",
    "log",
    "raw_log",
    "stdout",
    "stderr",
    "terminal_transcript",
    "raw_terminal_transcript",
    "transcript",
    "output_tail",
    "_artifact",
}


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)[:MAX_STRING_CHARS]
    if isinstance(value, list):
        return [_bounded(item) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        return {
            str(key): _bounded(child)
            for key, child in value.items()
            if str(key).strip().lower() not in _FORBIDDEN_KEYS
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_STRING_CHARS]


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    fields = _EVENT_FIELDS.get(event_type, ())
    compact = {
        field: _bounded(payload[field])
        for field in fields
        if field in payload
    }

    if event_type in {
        "checkpoint.received",
        "worker.report.received",
    }:
        unresolved = payload.get("unresolved")
        compact["unresolved_count"] = (
            len(unresolved) if isinstance(unresolved, list) else 0
        )
        scope = payload.get("scope_compliance")
        compact["scope_compliance"] = {
            key: value
            for key, value in scope.items()
            if key in {
                "outside_allowlist_touched",
                "unrequested_dependencies_added",
                "architecture_decisions_made",
            }
            and isinstance(value, bool)
        } if isinstance(scope, dict) else {}

    if event_type == "worker.report.received":
        evidence = payload.get("evidence")
        compact["evidence_count"] = (
            len(evidence) if isinstance(evidence, list) else 0
        )

    if event_type == "review.completed":
        criteria = payload.get("criteria")
        compact["criteria"] = [
            {
                key: item[key]
                for key in ("id", "criterion_id", "status")
                if key in item
            }
            for item in criteria[:100]
            if isinstance(item, dict)
        ] if isinstance(criteria, list) else []
        for field in ("regressions", "unverified", "unresolved"):
            value = payload.get(field)
            compact[f"{field}_count"] = (
                len(value) if isinstance(value, list) else 0
            )

    if event_type == "verification.completed":
        results = compact.get("results")
        if isinstance(results, list):
            compact["results"] = [
                {
                    key: item[key]
                    for key in (
                        "argv",
                        "exit_code",
                        "ok",
                        "timed_out",
                        "output_sha256",
                        "stdout_sha256",
                        "stderr_sha256",
                    )
                    if isinstance(item, dict) and key in item
                }
                for item in results
                if isinstance(item, dict)
            ]

    return compact


def _control_plane_command() -> list[str]:
    configured = os.getenv(
        "SIN_SIMONE_CONTROL_PLANE_COMMAND",
        "simone-control-plane",
    )
    command = shlex.split(configured)
    if not command:
        raise RuntimeError(
            "SIN_SIMONE_CONTROL_PLANE_COMMAND is empty"
        )
    return command


def _parse_json_object(value: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = value.strip()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("control-plane command returned no JSON object")


def call_control_plane(
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)

    process = subprocess.run(
        [
            *_control_plane_command(),
            operation,
            "--payload-json",
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env=environment,
    )

    if process.returncode != 0:
        message = (
            process.stderr.strip()
            or process.stdout.strip()
            or f"control-plane command exited with {process.returncode}"
        )
        raise RuntimeError(message[:MAX_STRING_CHARS])

    result = _parse_json_object(process.stdout)
    if result.get("ok") is not True:
        raise RuntimeError(
            str(result.get("error") or "control-plane command reported failure")
        )
    return result


def _artifact_from_event(
    event: dict[str, Any],
) -> dict[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("_artifact")
    if not isinstance(metadata, dict):
        return None

    digest = metadata.get("sha256")
    size_bytes = metadata.get("size_bytes")
    reference = metadata.get("archive_path") or metadata.get("path")
    filename = metadata.get("filename")
    if not (
        isinstance(digest, str)
        and isinstance(size_bytes, int)
        and isinstance(reference, str)
        and isinstance(filename, str)
    ):
        return None

    return {
        "kind": Path(filename).stem,
        "reference": reference,
        "sha256": digest,
        "size_bytes": size_bytes,
        "metadata": {
            "actor": event.get("actor"),
            "external_event_hash": event.get("event_hash"),
        },
    }


def sync_task(
    task_id: str,
    *,
    simone_task_id: str | None = None,
) -> dict[str, Any]:
    task = load_task(task_id)
    target_task_id = (
        simone_task_id
        or task.get("simone_task_id")
        or os.getenv("SIN_SIMONE_TASK_ID")
    )
    if not isinstance(target_task_id, str) or not target_task_id.strip():
        raise ValueError(
            "Simone task ID is required via --simone-task-id, "
            "task specification, or SIN_SIMONE_TASK_ID"
        )
    target_task_id = target_task_id.strip()

    binding = call_control_plane(
        "execution.bind",
        {
            "task_id": target_task_id,
            "source": SOURCE,
            "external_task_id": task_id,
            "metadata": {
                "repository_root": task.get("repository_root"),
                "base_sha": task.get("base_sha"),
                "task_hash": task.get("task_hash"),
                "role": task.get("role"),
            },
        },
    )

    event_results: list[dict[str, Any]] = []
    artifact_results: list[dict[str, Any]] = []
    last_event_hash: str | None = None

    for event in read_events(task_id):
        event_hash = str(event["event_hash"])
        last_event_hash = event_hash
        result = call_control_plane(
            "execution.event",
            {
                "task_id": target_task_id,
                "source": SOURCE,
                "external_event_id": f"{task_id}:{event_hash}",
                "external_sequence": event["sequence"],
                "external_hash": event_hash,
                "event_type": event["type"],
                "actor": event["actor"],
                "payload": compact_event(event),
            },
        )
        event_results.append(result["result"])

        artifact = _artifact_from_event(event)
        if artifact is not None:
            artifact_result = call_control_plane(
                "execution.artifact",
                {
                    "task_id": target_task_id,
                    "source": SOURCE,
                    **artifact,
                },
            )
            artifact_results.append(artifact_result["result"])

    return {
        "ok": True,
        "task_id": task_id,
        "simone_task_id": target_task_id,
        "binding": binding["result"],
        "events_synced": len(event_results),
        "event_duplicates": sum(
            1 for result in event_results if result.get("duplicate")
        ),
        "artifacts_synced": len(artifact_results),
        "artifact_duplicates": sum(
            1
            for result in artifact_results
            if result.get("duplicate")
        ),
        "last_event_hash": last_event_hash,
        "idempotent": True,
    }
