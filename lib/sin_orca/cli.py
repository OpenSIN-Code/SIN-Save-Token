"""Thin CLI entry point for sin-orca orchestration."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Any

from .artifacts import (
    ArtifactValidationError,
    ingest_artifact,
)
from .completion_manifest import (
    build_manifest,
    canonical_bytes,
    sha256_bytes,
    verify_manifest,
    write_manifest,
)
from .dispatch import (
    baseline_ref_is_valid,
    deep_values,
    dispatch_task,
    first_string,
    run_git,
    run_orca,
)
from .gates import completion_errors, execution_protocol_errors
from .lease import ControllerLease, LeaseConflictError, LeaseLostError
from .review import start_blind_review
from .simone_bridge import sync_task as sync_task_to_simone
from .state import (
    append_event,
    atomic_write_json,
    events_path,
    load_task,
    read_events,
    rebuild_ledger,
    repository_id,
    repository_root,
    state_root,
    task_dir,
    task_path,
)
from .verification import (
    actual_changed_files,
    bounded_diff,
    redact_text,
    run_controller_commands,
    validate_diff,
    validate_scope,
)
from .writer_reservation import release_writer, reservation_status

READY_PATTERN = re.compile(r"SIN_ARTIFACT_READY\s+(\S+\.json)")


def _controller_mutation(handler):
    """Serialize every state-changing controller command per task."""
    @wraps(handler)
    def wrapped(args: argparse.Namespace) -> int:
        lease_manager = ControllerLease(task_dir(args.task_id))
        try:
            lease = lease_manager.acquire()
        except LeaseConflictError as error:
            print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
            return 1

        try:
            result = int(handler(args))
        except Exception:
            try:
                lease_manager.release(lease.token)
            except LeaseLostError as release_error:
                print(
                    json.dumps({
                        "ok": False,
                        "error": (
                            "controller lease lost while handling failure: "
                            f"{release_error}"
                        ),
                    }),
                    file=sys.stderr,
                )
            raise

        try:
            lease_manager.release(lease.token)
        except LeaseLostError as error:
            print(
                json.dumps({
                    "ok": False,
                    "error": f"controller lease lost: {error}",
                }),
                file=sys.stderr,
            )
            return 1

        if handler.__name__ != "_cmd_sync_simone":
            sync_result = _sync_bound_task(args.task_id)
            if (
                isinstance(sync_result, dict)
                and sync_result.get("ok") is not True
            ):
                print(
                    json.dumps({
                        "ok": False,
                        "error": "automatic Simone synchronization failed",
                        "simone_sync": sync_result,
                    }),
                    file=sys.stderr,
                )
                if result == 0:
                    result = 1

        return result

    return wrapped


def _task_writer_status(task: dict[str, Any]) -> dict[str, Any] | None:
    repository = Path(task["repository_root"]).expanduser().resolve()
    return reservation_status(repository)


def _release_task_writer(task: dict[str, Any]) -> bool:
    if task.get("allow_edits") is not True:
        return False
    current = _task_writer_status(task)
    if not isinstance(current, dict) or current.get("task_id") != task.get(
        "task_id"
    ):
        return False
    return release_writer(
        Path(task["repository_root"]).expanduser().resolve(),
        task_id=str(task["task_id"]),
        allow_missing=True,
    )


def _simone_sync_status_path(task_id: str) -> Path:
    return task_dir(task_id) / "simone-sync-status.json"


def _write_simone_sync_status(
    task_id: str,
    value: dict[str, Any],
) -> None:
    atomic_write_json(_simone_sync_status_path(task_id), value)


def _read_simone_sync_status(task_id: str) -> dict[str, Any] | None:
    path = _simone_sync_status_path(task_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "status": "invalid-sync-status"}
    return value if isinstance(value, dict) else {
        "ok": False,
        "status": "invalid-sync-status",
    }


def _sync_bound_task(
    task_id: str,
    *,
    simone_task_id: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    task = load_task(task_id)
    bound_id = simone_task_id or task.get("simone_task_id")
    if not force and (
        not isinstance(bound_id, str) or not bound_id.strip()
    ):
        return None

    try:
        result = sync_task_to_simone(
            task_id,
            simone_task_id=(
                bound_id.strip()
                if isinstance(bound_id, str) and bound_id.strip()
                else simone_task_id
            ),
        )
        status = {
            "ok": True,
            "status": "synced",
            "simone_task_id": result.get("simone_task_id"),
            "events_synced": result.get("events_synced"),
            "event_duplicates": result.get("event_duplicates"),
            "artifacts_synced": result.get("artifacts_synced"),
            "artifact_duplicates": result.get("artifact_duplicates"),
            "last_event_hash": result.get("last_event_hash"),
            "idempotent": result.get("idempotent") is True,
        }
    except (OSError, RuntimeError, ValueError) as error:
        status = {
            "ok": False,
            "status": "sync-failed",
            "simone_task_id": bound_id,
            "error_type": type(error).__name__,
            "error": redact_text(str(error))[:2_000],
        }

    _write_simone_sync_status(task_id, status)
    return status


def _load_config(task_id: str | None = None) -> dict[str, Any]:
    if task_id is None:
        root = repository_root()
    else:
        task = load_task(task_id)
        root = Path(task["repository_root"]).expanduser().resolve()
    config_path = root / "config" / "orca-orchestrator.json"
    if config_path.is_file():
        return json.loads(
            config_path.read_text(encoding="utf-8")
        )
    return {}


def _actor_outbox(task_id: str, actor: str) -> Path:
    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)
    actor_data = ledger.get("actors", {}).get(actor)

    if not isinstance(actor_data, dict):
        raise RuntimeError(f"no worktree for actor {actor!r}")

    explicit = actor_data.get("outbox_path")
    if isinstance(explicit, str) and explicit:
        outbox = Path(explicit).expanduser().resolve()
    else:
        root = Path(task["repository_root"]).expanduser().resolve()
        outbox = root / ".sin-worker" / "tasks" / task_id / "outbox"

    repository = Path(task["repository_root"]).expanduser().resolve()
    try:
        outbox.relative_to(repository)
    except ValueError as error:
        raise RuntimeError("actor outbox escapes repository") from error
    return outbox


def _ensure_terminal(task_id: str, actor: str) -> str:
    ledger = rebuild_ledger(task_id)
    actor_data = ledger.get("actors", {}).get(actor)

    if not isinstance(actor_data, dict):
        raise RuntimeError(f"no actor {actor!r} in task {task_id!r}")

    terminal = actor_data.get("terminal_handle")

    if not terminal:
        raise RuntimeError(f"no terminal for actor {actor!r}")

    return terminal


def _output_text(result: dict[str, Any]) -> str:
    candidates = deep_values(
        result,
        {"text", "output", "content", "stdout", "screen", "terminalOutput"},
    )
    strings = [
        value for value in candidates
        if isinstance(value, str) and value.strip()
    ]
    return max(strings, key=len) if strings else ""


def _terminal_cursor_path(task_id: str) -> Path:
    return task_dir(task_id) / "terminal-cursors.json"


def _terminal_cursor(task_id: str, actor: str) -> str | None:
    path = _terminal_cursor_path(task_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cursor = value.get(actor) if isinstance(value, dict) else None
    return cursor if isinstance(cursor, str) and cursor else None


def _save_terminal_cursor(task_id: str, actor: str, cursor: str) -> None:
    path = _terminal_cursor_path(task_id)
    value: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, json.JSONDecodeError):
            value = {}
    value[actor] = cursor
    atomic_write_json(path, value)


def _check_repeated_failures(
    task_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    policy = (
        config.get("worker_control", {})
        .get("repeated_failure_policy", {})
    )
    max_identical = policy.get(
        "maximum_identical_failures_without_codex_intervention",
        2,
    )
    if not isinstance(max_identical, int) or isinstance(max_identical, bool):
        max_identical = 2
    max_identical = max(1, max_identical)

    history_path = task_dir(task_id) / "command-history.json"

    if not history_path.is_file():
        return {"blocked": False}

    try:
        history = json.loads(
            history_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {
            "blocked": True,
            "reason": "invalid_command_history",
            "requires_codex_intervention": True,
        }
    if not isinstance(history, list):
        return {
            "blocked": True,
            "reason": "invalid_command_history",
            "requires_codex_intervention": True,
        }

    if len(history) < max_identical:
        return {"blocked": False}

    recent = history[-max_identical:]

    groups: dict[str, list[int]] = {}

    for i, entry in enumerate(recent):
        if entry.get("exit_code") == 0:
            groups.clear()
            continue

        key = "|".join(
            [
                str(entry.get("command", "")),
                str(entry.get("exit_code", "")),
                str(entry.get("error_hash", "")),
            ]
        )

        groups.setdefault(key, []).append(i)

    for key, positions in groups.items():
        if len(positions) >= max_identical:
            return {
                "blocked": True,
                "reason": "repeated_failure",
                "requires_codex_intervention": True,
                "identical_failures": len(positions),
                "command": key.split("|")[0],
            }

    return {"blocked": False}


def _check_stalled(
    task_id: str,
    actor: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    policy = (
        config.get("worker_control", {})
        .get("stalled_worker_policy", {})
    )

    if not policy.get("enabled", True):
        return {"stalled": False}

    max_inactive = policy.get("maximum_inactive_seconds", 1200)
    if not isinstance(max_inactive, (int, float)) or isinstance(
        max_inactive, bool
    ):
        max_inactive = 1200
    max_inactive = max(1, float(max_inactive))

    activity_path = task_dir(task_id) / "activity.json"

    if not activity_path.is_file():
        return {"stalled": False}

    try:
        activity = json.loads(
            activity_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {
            "stalled": True,
            "reason": "invalid_activity_state",
            "action_required": "inspect_or_interrupt",
        }
    if not isinstance(activity, dict):
        return {
            "stalled": True,
            "reason": "invalid_activity_state",
            "action_required": "inspect_or_interrupt",
        }

    last_active_str = activity.get(f"{actor}_last_active")

    if not last_active_str:
        return {"stalled": False}

    from datetime import datetime, timezone

    try:
        last_active = datetime.fromisoformat(last_active_str)
    except (ValueError, TypeError):
        return {"stalled": False}

    now = datetime.now(timezone.utc)
    inactive = (now - last_active).total_seconds()

    if inactive < max_inactive:
        return {"stalled": False}

    child_running = activity.get(
        f"{actor}_child_process_running", False
    )

    if policy.get(
        "ignore_while_child_process_running", True
    ) and child_running:
        return {"stalled": False}

    return {
        "stalled": True,
        "idle_seconds": int(inactive),
        "action_required": "inspect_or_interrupt",
    }


def _record_observed_terminal_activity(
    task_id: str,
    actor: str,
) -> None:
    from datetime import datetime, timezone

    activity_path = task_dir(task_id) / "activity.json"
    activity: dict[str, Any] = {}

    if activity_path.is_file():
        try:
            loaded = json.loads(
                activity_path.read_text(encoding="utf-8")
            )
            if isinstance(loaded, dict):
                activity = loaded
        except (OSError, json.JSONDecodeError):
            activity = {}

    activity[f"{actor}_last_active"] = (
        datetime.now(timezone.utc).isoformat()
    )

    atomic_write_json(activity_path, activity)


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = repository_root()
    print(json.dumps({
        "status": "ok",
        "state_root": str(state_root()),
        "repository": str(root),
        "repository_id": repository_id(),
    }, indent=2))
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    verify_commands: list[list[str]] = []

    if args.verify_command:
        for raw in args.verify_command:
            parsed = shlex.split(raw)
            if not parsed:
                raise ValueError("verification command must not be empty")
            verify_commands.append(parsed)

    result = dispatch_task(
        role=args.role,
        objective=args.objective,
        steps=args.step or [],
        allowed_paths=args.allowed_path or [],
        forbidden_paths=args.forbidden_path or [],
        acceptance_criteria=args.acceptance or [],
        agent=args.agent,
        verification_commands=verify_commands,
        required_checkpoints=args.checkpoint or [],
        allow_edits=(args.role == "implementer" and not args.read_only),
        repository=args.repo,
        parent_terminal=args.parent_terminal,
        parent_task_id=args.parent_task_id,
        allow_child_delegation=args.allow_child_delegation,
        simone_task_id=args.simone_task_id,
    )

    sync_result = _sync_bound_task(result["task_id"])
    payload = dict(result)
    if sync_result is not None:
        payload["simone_sync"] = sync_result
    print(json.dumps(payload, indent=2))
    return 0 if sync_result is None or sync_result.get("ok") is True else 1


def _cmd_notify(args: argparse.Namespace) -> int:
    callback_types = {
        "ack",
        "checkpoint",
        "discovery",
        "question",
        "blocked",
        "child-dispatched",
        "done",
    }
    if args.type not in callback_types:
        raise ValueError(f"unsupported callback type: {args.type}")
    if args.actor not in {"worker", "reviewer"}:
        raise ValueError("callback actor must be worker or reviewer")

    task = load_task(args.task_id)
    step_id = args.step.strip() if isinstance(args.step, str) else None
    task_steps = [
        str(item.get("id"))
        for item in task.get("steps", [])
        if isinstance(item, dict) and item.get("id")
    ]
    if args.type == "checkpoint":
        if args.actor != "worker":
            raise ValueError("checkpoint callbacks must be sent by worker")
        if not step_id or step_id not in task_steps:
            raise ValueError(
                "checkpoint callback requires --step with a valid task step ID"
            )
    elif step_id is not None:
        raise ValueError("--step is only valid for checkpoint callbacks")
    ledger = rebuild_ledger(args.task_id)
    existing_callbacks = [
        item
        for item in ledger.get("callbacks", [])
        if isinstance(item, dict)
        and item.get("actor") == args.actor
        and item.get("callback_type") == args.type
        and item.get("step_id") == step_id
    ]
    if args.type in {"ack", "done"} or args.type == "checkpoint":
        if existing_callbacks:
            print(json.dumps({
                "ok": True,
                "status": "callback-already-sent",
                "task_id": args.task_id,
                "type": args.type,
                "step": step_id,
            }))
            return 0

    actor_data = ledger.get("actors", {}).get(args.actor)
    if not isinstance(actor_data, dict):
        raise RuntimeError(f"task has no {args.actor} actor")

    parent_terminal = actor_data.get("parent_terminal_handle") or task.get(
        "parent_terminal_handle"
    )
    actor_terminal = actor_data.get("terminal_handle")
    if not isinstance(parent_terminal, str) or not parent_terminal:
        raise RuntimeError("task has no parent terminal callback target")
    if parent_terminal == actor_terminal:
        raise RuntimeError("callback target must differ from actor terminal")

    outbox = _actor_outbox(args.task_id, args.actor)
    required_artifact = None
    if args.type == "checkpoint":
        required_artifact = outbox / "checkpoint.json"
    elif args.type == "done":
        required_artifact = outbox / (
            "review.json" if args.actor == "reviewer" else "report.json"
        )
    artifact_update: dict[str, Any] | None = None
    if required_artifact is not None:
        if required_artifact.is_file():
            try:
                artifact_update = ingest_artifact(
                    task_id=args.task_id,
                    actor=args.actor,
                    outbox=outbox,
                    filename=required_artifact.name,
                )
            except ArtifactValidationError as error:
                raise RuntimeError(
                    f"invalid callback artifact: {error}"
                ) from error
            ledger = rebuild_ledger(args.task_id)
        elif args.type == "checkpoint":
            archived = any(
                isinstance(item, dict)
                and item.get("actor") == args.actor
                and item.get("step_id") == step_id
                for item in ledger.get("checkpoints", [])
            )
            if not archived:
                raise RuntimeError(
                    f"callback checkpoint requires existing or archived artifact: {required_artifact}"
                )
        elif args.actor == "reviewer":
            if not isinstance(ledger.get("review"), dict):
                raise RuntimeError(
                    f"callback done requires existing or archived artifact: {required_artifact}"
                )
        elif not isinstance(ledger.get("report"), dict):
            raise RuntimeError(
                f"callback done requires existing or archived artifact: {required_artifact}"
            )

    summary = redact_text(args.summary).strip()[:500]
    verify = redact_text(args.verify).strip()[:200]
    action = redact_text(args.action).strip()[:300]
    changed: list[str] = []
    for raw in args.changed or []:
        for item in raw.split(","):
            rendered = item.strip()
            if not rendered or rendered.lower() in {"none", "null", "-"}:
                continue
            changed.append(rendered[:500])
    changed = list(dict.fromkeys(changed))[:100]
    rendered_changed = ",".join(changed) if changed else "none"

    message = (
        f"SIN_CALLBACK task={args.task_id} actor={args.actor} type={args.type} "
        f"step={json.dumps(step_id or 'none', ensure_ascii=False)} "
        f"summary={json.dumps(summary, ensure_ascii=False)} "
        f"changed={json.dumps(rendered_changed, ensure_ascii=False)} "
        f"verify={json.dumps(verify or 'unknown', ensure_ascii=False)} "
        f"action={json.dumps(action or 'none', ensure_ascii=False)}"
    )
    run_orca([
        "terminal",
        "send",
        "--terminal",
        parent_terminal,
        "--text",
        message,
        "--enter",
    ])
    append_event(
        args.task_id,
        f"{args.actor}.callback",
        {
            "callback_type": args.type,
            "step_id": step_id,
            "summary": summary,
            "changed_files": changed,
            "verification_status": verify or "unknown",
            "requested_action": action or "none",
            "parent_terminal_handle": parent_terminal,
            "artifact": artifact_update,
        },
        actor=args.actor,
    )
    print(json.dumps({
        "ok": True,
        "status": "callback-sent",
        "task_id": args.task_id,
        "type": args.type,
        "parent_terminal": parent_terminal,
        "artifact": artifact_update,
    }))
    return 0


@_controller_mutation
def _cmd_approve(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    if task.get("approval_mode", "stepwise") != "stepwise":
        print(json.dumps({
            "ok": False,
            "error": "explicit approval is only valid for stepwise tasks",
            "approval_mode": task.get(
                "approval_mode", "stepwise"
            ),
        }), file=sys.stderr)
        return 1
    ledger = rebuild_ledger(args.task_id)
    expected_steps = [
        str(step.get("id"))
        for step in task.get("steps", [])
        if isinstance(step, dict) and step.get("id")
    ]
    if args.step not in expected_steps:
        print(json.dumps({
            "ok": False,
            "error": f"unknown step {args.step!r}",
            "expected_steps": expected_steps,
        }), file=sys.stderr)
        return 1

    approved_steps = [
        str(message.get("step_id"))
        for message in ledger.get("controller_messages", [])
        if message.get("type") == "codex.approved"
        and message.get("step_id")
    ]
    if args.step in approved_steps:
        print(json.dumps({
            "status": "already-approved",
            "step": args.step,
        }))
        return 0

    next_step = (
        expected_steps[len(approved_steps)]
        if len(approved_steps) < len(expected_steps)
        else None
    )
    if args.step != next_step:
        print(json.dumps({
            "ok": False,
            "error": "steps must be approved in order",
            "next_step": next_step,
        }), file=sys.stderr)
        return 1

    required = list(task.get("required_checkpoints", []))
    received = [
        item.get("checkpoint")
        for item in ledger.get("checkpoints", [])
        if item.get("actor") == "worker"
    ]
    if required and len(received) <= len(approved_steps):
        print(json.dumps({
            "ok": False,
            "error": "a fresh worker checkpoint is required before approval",
            "received_checkpoints": received,
        }), file=sys.stderr)
        return 1

    terminal = _ensure_terminal(args.task_id, "worker")
    message = f"CODEX APPROVED. Step {args.step}. {args.instruction}"
    run_orca([
        "terminal", "send",
        "--terminal", terminal,
        "--text", message, "--enter",
    ])

    append_event(
        args.task_id,
        "codex.approved",
        {"step_id": args.step, "instruction": args.instruction},
        actor="codex",
    )

    print(json.dumps({"status": "approved", "step": args.step}))
    return 0


@_controller_mutation
def _cmd_send(args: argparse.Namespace) -> int:
    terminal = _ensure_terminal(args.task_id, "worker")
    run_orca([
        "terminal", "send",
        "--terminal", terminal,
        "--text", args.text, "--enter",
    ])
    append_event(
        args.task_id, "codex.sent",
        {"text": args.text},
        actor="codex",
    )
    print(json.dumps({"status": "sent"}))
    return 0


@_controller_mutation
def _cmd_interrupt(args: argparse.Namespace) -> int:
    terminal = _ensure_terminal(args.task_id, "worker")

    interrupted = False
    try:
        run_orca([
            "terminal", "send",
            "--terminal", terminal,
            "--text", "\u0003",
        ])
        interrupted = True
        time.sleep(0.25)
    except RuntimeError:
        pass

    message = (
        "CODEX INTERRUPT. Stop current work immediately. "
        + args.text
    )
    run_orca([
        "terminal", "send",
        "--terminal", terminal,
        "--text", message, "--enter",
    ])

    append_event(
        args.task_id, "codex.interrupted",
        {"text": args.text, "control_c_sent": interrupted},
        actor="codex",
    )
    print(json.dumps({"status": "interrupted", "control_c_sent": interrupted}))
    return 0


@_controller_mutation
def _cmd_followup(args: argparse.Namespace) -> int:
    terminal = _ensure_terminal(args.task_id, "worker")
    message = f"CODEX FOLLOWUP. Step {args.step}. {args.text}"
    run_orca([
        "terminal", "send",
        "--terminal", terminal,
        "--text", message, "--enter",
    ])
    append_event(
        args.task_id, "codex.followup",
        {"step_id": args.step, "instruction": args.text},
        actor="codex",
    )
    print(json.dumps({"status": "followup_sent", "step": args.step}))
    return 0


@_controller_mutation
def _cmd_suspend(args: argparse.Namespace) -> int:
    ledger = rebuild_ledger(args.task_id)
    status = ledger.get("status", "")

    safe = (
        status.startswith("checkpoint:")
        or status in {
            "report-received",
            "review-complete",
            "verification-complete",
        }
    )

    if not safe:
        print(
            "ERROR: suspend only at controller safe point",
            file=sys.stderr,
        )
        return 1

    append_event(
        args.task_id, "task.suspended",
        {"reason": args.reason},
        actor="codex",
    )
    print(json.dumps({"status": "suspended", "reason": args.reason}))
    return 0


@_controller_mutation
def _cmd_resume(args: argparse.Namespace) -> int:
    terminal = _ensure_terminal(args.task_id, "worker")
    message = f"CODEX RESUME. {args.text}"
    run_orca([
        "terminal", "send",
        "--terminal", terminal,
        "--text", message, "--enter",
    ])
    append_event(
        args.task_id, "task.resumed",
        {"instruction": args.text},
        actor="codex",
    )
    print(json.dumps({"status": "resumed"}))
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    terminal = _ensure_terminal(args.task_id, args.actor)
    run_orca([
        "terminal", "wait",
        "--terminal", terminal,
        "--for", "tui-idle",
        "--timeout-ms", "300000",
    ])
    print(json.dumps({"status": "waited", "actor": args.actor}))
    return 0


@_controller_mutation
def _cmd_mailbox(args: argparse.Namespace) -> int:
    terminal = _ensure_terminal(args.task_id, args.actor)

    read_arguments = [
        "terminal", "read",
        "--terminal", terminal,
        "--limit", "5000",
    ]
    cursor = _terminal_cursor(args.task_id, args.actor)
    if cursor:
        read_arguments.extend(["--cursor", cursor])

    result = run_orca(read_arguments)
    text = _output_text(result)
    next_cursor = first_string(
        result,
        {"nextCursor", "next_cursor", "cursor"},
    )
    if next_cursor:
        _save_terminal_cursor(args.task_id, args.actor, next_cursor)

    markers = READY_PATTERN.findall(text)
    outbox = _actor_outbox(args.task_id, args.actor)
    available = [
        name for name in ("checkpoint.json", "report.json", "review.json")
        if (outbox / name).is_file()
    ]

    updates: list[dict[str, Any]] = []

    for filename in sorted(set(markers) | set(available)):
        try:
            update = ingest_artifact(
                task_id=args.task_id,
                actor=args.actor,
                outbox=outbox,
                filename=filename,
            )
            updates.append(update)
        except ArtifactValidationError as error:
            updates.append({
                "ok": False,
                "filename": filename,
                "error": str(error),
            })

    config = _load_config(args.task_id)
    prior = rebuild_ledger(args.task_id)

    observed_update = any(
        update.get("ok") is True and not update.get("duplicate")
        for update in updates
    )
    cursor_advanced = bool(next_cursor) and next_cursor != cursor
    observed_activity = observed_update or (
        bool(text.strip()) and (cursor is None or cursor_advanced)
    )
    if observed_activity:
        _record_observed_terminal_activity(args.task_id, args.actor)

    warnings: list[dict[str, Any]] = []

    repeated = _check_repeated_failures(args.task_id, config)
    if repeated.get("blocked"):
        current = rebuild_ledger(args.task_id).get("worker_blocked")
        if current != repeated:
            append_event(
                args.task_id, "worker.blocked",
                repeated, actor="controller",
            )
        warnings.append(repeated)

    stalled = _check_stalled(args.task_id, args.actor, config)
    if stalled.get("stalled"):
        current = rebuild_ledger(args.task_id).get("worker_stalled")
        comparable = {
            key: value for key, value in stalled.items()
            if key != "idle_seconds"
        }
        previous = {
            key: value for key, value in current.items()
            if key != "idle_seconds"
        } if isinstance(current, dict) else None
        if previous != comparable:
            append_event(
                args.task_id, "worker.stalled",
                stalled, actor="controller",
            )
        warnings.append(stalled)

    if (
        observed_activity
        and not repeated.get("blocked")
        and not stalled.get("stalled")
        and (prior.get("worker_stalled") or prior.get("worker_blocked"))
    ):
        append_event(
            args.task_id,
            "worker.recovered",
            {"actor": args.actor},
            actor="controller",
        )

    failed_updates = [
        update
        for update in updates
        if isinstance(update, dict) and update.get("ok") is False
    ]
    print(json.dumps({
        "task_id": args.task_id,
        "actor": args.actor,
        "updates": updates,
        "discarded_terminal_chars": len(text),
        "warnings": warnings,
    }, indent=2))
    return 1 if failed_updates or warnings else 0


@_controller_mutation
def _cmd_verify(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    worker = ledger.get("actors", {}).get("worker", {})

    worktree = Path(worker.get("worktree_path", ""))

    if not worktree.is_dir():
        print(json.dumps({"ok": False, "error": "worker worktree not found"}))
        return 1

    if task.get("allow_edits") is True:
        writer = _task_writer_status(task)
        if not isinstance(writer, dict) or writer.get("task_id") != args.task_id:
            print(json.dumps({
                "ok": False,
                "writer_error": "task does not own repository writer",
                "writer": writer,
            }, indent=2))
            return 1

    protocol_errors = execution_protocol_errors(args.task_id)
    if not isinstance(ledger.get("report"), dict):
        protocol_errors.append("worker report missing")
    if protocol_errors:
        print(json.dumps({
            "ok": False,
            "protocol_errors": protocol_errors,
        }, indent=2))
        return 1

    current_head = run_git(worktree, "rev-parse", "HEAD")
    if current_head != task.get("repository_head_sha"):
        result = {
            "ok": False,
            "head_error": "worker changed repository HEAD",
            "expected_head": task.get("repository_head_sha"),
            "actual_head": current_head,
        }
        append_event(
            args.task_id,
            "verification.completed",
            result,
            actor="controller",
        )
        print(json.dumps(result, indent=2))
        return 1

    if not baseline_ref_is_valid(
        worktree,
        task.get("baseline_ref"),
        task["base_sha"],
    ):
        result = {
            "ok": False,
            "baseline_error": "task baseline reference is missing or changed",
        }
        append_event(
            args.task_id,
            "verification.completed",
            result,
            actor="controller",
        )
        print(json.dumps(result, indent=2))
        return 1

    changed = actual_changed_files(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    diff = bounded_diff(
        worktree=worktree,
        base_sha=task["base_sha"],
    )

    scope_errors = validate_scope(
        changed_files=changed,
        allowed_paths=task["allowed_paths"],
        forbidden_paths=task.get("forbidden_paths", []),
        allow_edits=task.get("allow_edits", True),
    )

    if scope_errors:
        result = {
            "ok": False,
            "changed_files": changed,
            "scope_errors": scope_errors,
        }
        append_event(
            args.task_id, "verification.completed",
            result, actor="controller",
        )
        print(json.dumps(result, indent=2))
        return 1

    commands = task.get("verification_commands", [])

    if args.verification_command:
        commands = [shlex.split(args.verification_command)]

    diff_check = validate_diff(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    verification = run_controller_commands(
        worktree=worktree,
        commands=commands,
    )
    verification["results"] = [
        diff_check,
        *verification.get("results", []),
    ]
    verification["ok"] = (
        diff_check.get("ok") is True
        and verification.get("ok") is True
    )

    verification["changed_files"] = changed
    verification["diff_sha256"] = diff["full_sha256"]

    append_event(
        args.task_id, "verification.completed",
        verification, actor="controller",
    )

    print(json.dumps(verification, indent=2))
    return 0 if verification.get("ok") else 1


@_controller_mutation
def _cmd_review(args: argparse.Namespace) -> int:
    config = _load_config(args.task_id)
    preferred = (
        config.get("review", {})
        .get("preferred_agents", ["opencode", "mimo-code"])
    )

    result = start_blind_review(
        task_id=args.task_id,
        preferred_agents=preferred,
    )

    print(json.dumps(result, indent=2))
    return 0


def _archived_artifacts(task_id: str) -> list[Path]:
    artifacts: dict[str, Path] = {}

    for event in read_events(task_id):
        metadata = event.get("payload", {}).get("_artifact", {})
        if not isinstance(metadata, dict):
            continue

        raw_path = metadata.get("archive_path") or metadata.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue

        path = Path(raw_path).expanduser().resolve()
        artifacts[str(path)] = path

    return [artifacts[key] for key in sorted(artifacts)]


def _build_completion_manifest(
    *,
    task_id: str,
    task: dict[str, Any],
    ledger: dict[str, Any],
    worktree: Path,
) -> dict[str, Any]:
    verification = ledger.get("verification")
    if not isinstance(verification, dict):
        raise RuntimeError("controller verification missing")

    review = ledger.get("review")
    if review is not None and not isinstance(review, dict):
        raise RuntimeError("invalid review payload")

    return build_manifest(
        task=task,
        worktree=worktree,
        events_file=events_path(task_id),
        artifacts=_archived_artifacts(task_id),
        verification=verification,
        review=review,
    )


@_controller_mutation
def _cmd_cancel(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    if ledger.get("status") == "completed":
        print(json.dumps({
            "ok": False,
            "error": "completed task cannot be cancelled",
        }), file=sys.stderr)
        return 1
    if ledger.get("status") == "cancelled":
        print(json.dumps({
            "status": "cancelled",
            "writer_released": _release_task_writer(task),
            "reused": True,
        }))
        return 0

    interrupted: list[str] = []
    for actor in ("worker", "reviewer"):
        actor_data = ledger.get("actors", {}).get(actor)
        if not isinstance(actor_data, dict):
            continue
        terminal = actor_data.get("terminal_handle")
        if not isinstance(terminal, str) or not terminal:
            continue
        try:
            run_orca([
                "terminal",
                "send",
                "--terminal",
                terminal,
                "--text",
                "\u0003",
            ])
            run_orca([
                "terminal",
                "send",
                "--terminal",
                terminal,
                "--text",
                "CODEX CANCELLED TASK. Stop immediately and make no further changes.",
                "--enter",
            ])
            interrupted.append(actor)
        except RuntimeError:
            continue

    append_event(
        args.task_id,
        "task.cancelled",
        {
            "reason": redact_text(args.reason)[:2_000],
            "interrupted_actors": interrupted,
        },
        actor="codex",
    )
    writer_released = _release_task_writer(task)
    print(json.dumps({
        "status": "cancelled",
        "reason": redact_text(args.reason)[:2_000],
        "interrupted_actors": interrupted,
        "writer_released": writer_released,
        "reused": False,
    }, indent=2))
    return 0


@_controller_mutation
def _cmd_complete(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    worker = ledger.get("actors", {}).get("worker", {})

    worktree = Path(worker.get("worktree_path", ""))

    if not worktree.is_dir():
        print(json.dumps({"ok": False, "error": "worker worktree not found"}))
        return 1

    if task.get("allow_edits") is True and ledger.get("status") != "completed":
        writer = _task_writer_status(task)
        if not isinstance(writer, dict) or writer.get("task_id") != args.task_id:
            print(json.dumps({
                "status": "incomplete",
                "errors": ["task does not own repository writer"],
                "writer": writer,
            }, indent=2))
            return 1

    current_head = run_git(worktree, "rev-parse", "HEAD")
    if current_head != task.get("repository_head_sha"):
        print(json.dumps({
            "status": "incomplete",
            "errors": ["worker changed repository HEAD"],
            "expected_head": task.get("repository_head_sha"),
            "actual_head": current_head,
        }, indent=2))
        return 1

    if not baseline_ref_is_valid(
        worktree,
        task.get("baseline_ref"),
        task["base_sha"],
    ):
        print(json.dumps({
            "status": "incomplete",
            "errors": ["task baseline reference is missing or changed"],
        }, indent=2))
        return 1

    changed = actual_changed_files(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    diff = bounded_diff(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    manifest_path = task_dir(args.task_id) / "completion-manifest.json"

    if ledger.get("status") == "completed" and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_errors = verify_manifest(manifest)
        manifest_errors.extend(completion_errors(
            args.task_id,
            actual_changed_files=changed,
            actual_diff_sha256=diff["full_sha256"],
        ))
        body = manifest.get("body")
        if not isinstance(body, dict):
            manifest_errors.append("manifest body missing")
        else:
            if body.get("task_id") != args.task_id:
                manifest_errors.append("manifest task_id mismatch")
            if body.get("task_hash") != task.get("task_hash"):
                manifest_errors.append("manifest task_hash mismatch")
            if body.get("base_sha") != task.get("base_sha"):
                manifest_errors.append("manifest base_sha mismatch")
            if Path(str(body.get("repository_root", ""))).resolve() != worktree.resolve():
                manifest_errors.append("manifest worktree mismatch")

            event_block = body.get("events")
            expected_events_path = events_path(args.task_id).resolve()
            if not isinstance(event_block, dict) or Path(
                str(event_block.get("path", ""))
            ).resolve() != expected_events_path:
                manifest_errors.append("manifest events path mismatch")

            expected_artifacts = {
                str(path.resolve())
                for path in _archived_artifacts(args.task_id)
            }
            artifact_block = body.get("artifacts")
            manifest_artifacts = {
                str(Path(str(item.get("path", ""))).resolve())
                for item in artifact_block
                if isinstance(item, dict)
            } if isinstance(artifact_block, list) else set()
            if manifest_artifacts != expected_artifacts:
                manifest_errors.append("manifest artifact set mismatch")

            verification = ledger.get("verification")
            if not isinstance(verification, dict) or body.get(
                "verification_sha256"
            ) != sha256_bytes(canonical_bytes(verification)):
                manifest_errors.append("manifest verification hash mismatch")

            review = ledger.get("review")
            expected_review_hash = (
                sha256_bytes(canonical_bytes(review))
                if isinstance(review, dict)
                else None
            )
            if body.get("review_sha256") != expected_review_hash:
                manifest_errors.append("manifest review hash mismatch")
        if manifest_errors:
            print(json.dumps({
                "status": "manifest-invalid",
                "errors": manifest_errors,
            }, indent=2))
            return 1

        writer_released = _release_task_writer(task)
        print(json.dumps({
            "status": "completed",
            "manifest": str(manifest_path),
            "writer_released": writer_released,
            "reused": True,
        }, indent=2))
        return 0

    errors = completion_errors(
        args.task_id,
        actual_changed_files=changed,
        actual_diff_sha256=diff["full_sha256"],
    )

    if errors:
        print(json.dumps({
            "status": "incomplete",
            "errors": errors,
        }, indent=2))
        return 1

    # Validate every manifest input before making task.completed final.
    _build_completion_manifest(
        task_id=args.task_id,
        task=task,
        ledger=ledger,
        worktree=worktree,
    )

    if ledger.get("status") != "completed":
        append_event(
            args.task_id, "task.completed",
            {"changed_files": changed},
            actor="controller",
        )
        ledger = rebuild_ledger(args.task_id)

    manifest = _build_completion_manifest(
        task_id=args.task_id,
        task=task,
        ledger=ledger,
        worktree=worktree,
    )
    write_manifest(manifest_path, manifest)
    writer_released = _release_task_writer(task)

    print(json.dumps({
        "status": "completed",
        "manifest": str(manifest_path),
        "integrity": manifest["integrity"],
        "writer_released": writer_released,
        "reused": False,
    }, indent=2))
    return 0


@_controller_mutation
def _cmd_sync_simone(args: argparse.Namespace) -> int:
    result = _sync_bound_task(
        args.task_id,
        simone_task_id=args.simone_task_id,
        force=True,
    )
    assert isinstance(result, dict)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") is True else 1


def _cmd_status(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    events = read_events(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    manifest_path = task_dir(args.task_id) / "completion-manifest.json"
    print(json.dumps({
        "task_id": args.task_id,
        "status": ledger.get("status"),
        "role": task.get("role"),
        "approval_mode": task.get("approval_mode", "stepwise"),
        "simone_task_id": task.get("simone_task_id"),
        "events_count": len(events),
        "last_event_hash": (
            events[-1].get("event_hash") if events else None
        ),
        "checkpoints": [
            item.get("checkpoint")
            for item in ledger.get("checkpoints", [])
            if isinstance(item, dict)
        ],
        "verification_ok": (
            ledger.get("verification", {}).get("ok")
            if isinstance(ledger.get("verification"), dict)
            else None
        ),
        "report_received": isinstance(ledger.get("report"), dict),
        "review_received": isinstance(ledger.get("review"), dict),
        "review_verdict": (
            ledger.get("review", {}).get("verdict")
            if isinstance(ledger.get("review"), dict)
            else None
        ),
        "writer_reservation": _task_writer_status(task),
        "callbacks": [
            {
                "actor": item.get("actor"),
                "type": item.get("callback_type"),
                "step_id": item.get("step_id"),
                "summary": item.get("summary"),
                "verification_status": item.get("verification_status"),
                "requested_action": item.get("requested_action"),
            }
            for item in ledger.get("callbacks", [])
            if isinstance(item, dict)
        ],
        "completion_manifest": (
            str(manifest_path) if manifest_path.is_file() else None
        ),
        "simone_sync": _read_simone_sync_status(args.task_id),
    }, indent=2))
    return 0


@_controller_mutation
def _cmd_rebuild(args: argparse.Namespace) -> int:
    events = read_events(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    print(json.dumps({
        "status": "rebuilt",
        "events_count": len(events),
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sin-orca",
        description="SIN Orca Orchestrator",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="System check")

    p = sub.add_parser("dispatch", help="Create and dispatch task")
    p.add_argument("--role", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--agent", default="mimo-code")
    p.add_argument("--step", action="append")
    p.add_argument("--allowed-path", action="append")
    p.add_argument("--forbidden-path", action="append")
    p.add_argument("--acceptance", action="append")
    p.add_argument("--verify-command", action="append")
    p.add_argument("--checkpoint", action="append")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--repo")
    p.add_argument("--parent-terminal")
    p.add_argument("--parent-task-id")
    p.add_argument("--allow-child-delegation", action="store_true")
    p.add_argument("--simone-task-id")

    p = sub.add_parser("notify", help="Push a worker callback to its parent terminal")
    p.add_argument("task_id")
    p.add_argument(
        "--type",
        required=True,
        choices=[
            "ack",
            "checkpoint",
            "discovery",
            "question",
            "blocked",
            "child-dispatched",
            "done",
        ],
    )
    p.add_argument("--summary", required=True)
    p.add_argument("--step")
    p.add_argument("--changed", action="append")
    p.add_argument("--verify", default="unknown")
    p.add_argument("--action", default="none")
    p.add_argument("--actor", choices=["worker", "reviewer"], default="worker")

    p = sub.add_parser("approve", help="Approve step")
    p.add_argument("task_id")
    p.add_argument("--step", required=True)
    p.add_argument("--instruction", required=True)

    p = sub.add_parser("send", help="Send message")
    p.add_argument("task_id")
    p.add_argument("--text", required=True)

    p = sub.add_parser("interrupt", help="Interrupt worker")
    p.add_argument("task_id")
    p.add_argument("--text", required=True)

    p = sub.add_parser("followup", help="Follow-up")
    p.add_argument("task_id")
    p.add_argument("--step", required=True)
    p.add_argument("--text", required=True)

    p = sub.add_parser("suspend", help="Suspend at safe point")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("resume", help="Resume workflow")
    p.add_argument("task_id")
    p.add_argument("--text", required=True)

    p = sub.add_parser("wait", help="Wait for actor")
    p.add_argument("task_id")
    p.add_argument("--actor", default="worker")

    p = sub.add_parser("mailbox", help="Consume artifacts")
    p.add_argument("task_id")
    p.add_argument("--actor", default="worker")

    p = sub.add_parser("verify", help="Run controller verification")
    p.add_argument("task_id")
    p.add_argument("--command", dest="verification_command")

    p = sub.add_parser("review", help="Start blind reviewer")
    p.add_argument("task_id")

    p = sub.add_parser("cancel", help="Cancel task and release repository writer")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("complete", help="Complete task")
    p.add_argument("task_id")

    p = sub.add_parser("status", help="Show task and sync status")
    p.add_argument("task_id")

    p = sub.add_parser(
        "sync-simone",
        help="Replay compact execution facts into Simone",
    )
    p.add_argument("task_id")
    p.add_argument("--simone-task-id")

    p = sub.add_parser("rebuild", help="Rebuild ledger")
    p.add_argument("task_id")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    handlers = {
        "doctor": _cmd_doctor,
        "dispatch": _cmd_dispatch,
        "notify": _cmd_notify,
        "approve": _cmd_approve,
        "send": _cmd_send,
        "interrupt": _cmd_interrupt,
        "followup": _cmd_followup,
        "suspend": _cmd_suspend,
        "resume": _cmd_resume,
        "wait": _cmd_wait,
        "mailbox": _cmd_mailbox,
        "verify": _cmd_verify,
        "review": _cmd_review,
        "cancel": _cmd_cancel,
        "complete": _cmd_complete,
        "status": _cmd_status,
        "sync-simone": _cmd_sync_simone,
        "rebuild": _cmd_rebuild,
    }

    try:
        return int(handlers[args.command](args))
    except (RuntimeError, ValueError, OSError) as error:
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "command": args.command,
        }), file=sys.stderr)
        return 2
