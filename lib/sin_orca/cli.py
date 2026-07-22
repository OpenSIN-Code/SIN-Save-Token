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
from pathlib import Path
from typing import Any

from .artifacts import (
    ArtifactValidationError,
    ingest_artifact,
)
from .completion_manifest import (
    build_manifest,
    verify_manifest,
    write_manifest,
)
from .dispatch import dispatch_task, run_orca
from .gates import completion_errors
from .lease import ControllerLease, LeaseConflictError
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
    run_controller_commands,
    validate_scope,
)

READY_PATTERN = re.compile(r"SIN_ARTIFACT_READY\s+(\S+\.json)")


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
    ledger = rebuild_ledger(task_id)
    actor_data = ledger.get("actors", {}).get(actor)

    if not isinstance(actor_data, dict):
        raise RuntimeError(f"no worktree for actor {actor!r}")

    worktree_path = actor_data.get("worktree_path")

    if not worktree_path:
        raise RuntimeError(f"no worktree path for actor {actor!r}")

    return Path(worktree_path) / ".sin-worker" / "outbox"


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
    return result.get("result", {}).get("text", "")


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

    history_path = task_dir(task_id) / "command-history.json"

    if not history_path.is_file():
        return {"blocked": False}

    history = json.loads(
        history_path.read_text(encoding="utf-8")
    )

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

    activity_path = task_dir(task_id) / "activity.json"

    if not activity_path.is_file():
        return {"stalled": False}

    activity = json.loads(
        activity_path.read_text(encoding="utf-8")
    )

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


def _update_activity_after_check(
    task_id: str,
    actor: str,
) -> None:
    from datetime import datetime, timezone

    activity_path = task_dir(task_id) / "activity.json"
    activity: dict[str, Any] = {}

    if activity_path.is_file():
        activity = json.loads(
            activity_path.read_text(encoding="utf-8")
        )

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
            verify_commands.append(shlex.split(raw))

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
        allow_edits=not args.read_only,
        repository=args.repo,
        setup=args.setup,
        simone_task_id=args.simone_task_id,
    )

    print(json.dumps(result, indent=2))
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    lease_mgr = ControllerLease(task_dir(args.task_id))

    try:
        lease = lease_mgr.acquire()
    except LeaseConflictError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1

    try:
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
    finally:
        lease_mgr.release(lease.token)


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


def _cmd_mailbox(args: argparse.Namespace) -> int:
    ledger = rebuild_ledger(args.task_id)
    terminal = _ensure_terminal(args.task_id, args.actor)

    result = run_orca([
        "terminal", "read",
        "--terminal", terminal,
        "--limit", "5000",
    ])
    text = _output_text(result)

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

    warnings: list[dict[str, Any]] = []

    repeated = _check_repeated_failures(args.task_id, config)
    if repeated.get("blocked"):
        append_event(
            args.task_id, "worker.blocked",
            repeated, actor="controller",
        )
        warnings.append(repeated)

    stalled = _check_stalled(args.task_id, args.actor, config)
    if stalled.get("stalled"):
        append_event(
            args.task_id, "worker.stalled",
            stalled, actor="controller",
        )
        warnings.append(stalled)

    _update_activity_after_check(args.task_id, args.actor)

    print(json.dumps({
        "task_id": args.task_id,
        "actor": args.actor,
        "updates": updates,
        "discarded_terminal_chars": len(text),
        "warnings": warnings,
    }, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    worker = ledger.get("actors", {}).get("worker", {})

    worktree = Path(worker.get("worktree_path", ""))

    if not worktree.is_dir():
        print(json.dumps({"ok": False, "error": "worker worktree not found"}))
        return 1

    changed = actual_changed_files(
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

    verification = run_controller_commands(
        worktree=worktree,
        commands=commands,
    )

    verification["changed_files"] = changed

    append_event(
        args.task_id, "verification.completed",
        verification, actor="controller",
    )

    print(json.dumps(verification, indent=2))
    return 0 if verification.get("ok") else 1


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


def _cmd_complete(args: argparse.Namespace) -> int:
    task = load_task(args.task_id)
    ledger = rebuild_ledger(args.task_id)
    worker = ledger.get("actors", {}).get("worker", {})

    worktree = Path(worker.get("worktree_path", ""))

    if not worktree.is_dir():
        print(json.dumps({"ok": False, "error": "worker worktree not found"}))
        return 1

    changed = actual_changed_files(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    manifest_path = task_dir(args.task_id) / "completion-manifest.json"

    if ledger.get("status") == "completed" and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_errors = verify_manifest(manifest)
        if manifest_errors:
            print(json.dumps({
                "status": "manifest-invalid",
                "errors": manifest_errors,
            }, indent=2))
            return 1

        print(json.dumps({
            "status": "completed",
            "manifest": str(manifest_path),
            "reused": True,
        }, indent=2))
        return 0

    errors = completion_errors(
        args.task_id,
        actual_changed_files=changed,
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

    print(json.dumps({
        "status": "completed",
        "manifest": str(manifest_path),
        "integrity": manifest["integrity"],
        "reused": False,
    }, indent=2))
    return 0


def _cmd_sync_simone(args: argparse.Namespace) -> int:
    result = sync_task_to_simone(
        args.task_id,
        simone_task_id=args.simone_task_id,
    )
    print(json.dumps(result, indent=2))
    return 0


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
    p.add_argument("--setup", default="none")
    p.add_argument("--simone-task-id")

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

    p = sub.add_parser("complete", help="Complete task")
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
        "complete": _cmd_complete,
        "sync-simone": _cmd_sync_simone,
        "rebuild": _cmd_rebuild,
    }

    return handlers[args.command](args)
