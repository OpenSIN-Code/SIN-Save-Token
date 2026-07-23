#!/usr/bin/env python3
"""Real same-worktree Orca callback and completion smoke test.

The script creates a disposable Git repository under ~/.local/state, opens a
parent Orca terminal in that existing repository, dispatches a worker into a
second terminal in the same worktree, verifies direct callbacks, runs the
controller gates, starts an independent reviewer terminal, and validates the
completion manifest. No source repository is modified and no worker worktree is
allowed to appear.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"command returned no JSON object: {stripped[-1_000:]}")


def bounded(value: str, limit: int = 2_000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return "...[truncated]\n" + value[-limit:]


def safe_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    return environment


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 300,
    require_ok: bool = True,
) -> dict[str, Any]:
    try:
        process = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"command timed out: {' '.join(argv)}\n"
            + bounded(str(error.stderr or error.stdout or ""))
        ) from error

    if process.returncode != 0 and require_ok:
        raise RuntimeError(
            f"command failed ({process.returncode}): {' '.join(argv)}\n"
            + bounded(process.stderr or process.stdout)
        )
    payload = parse_json(process.stdout or process.stderr)
    payload["_exit_code"] = process.returncode
    return payload


def run_git(repository: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=safe_git_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return process.stdout.strip()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def output_strings(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "text",
                "output",
                "content",
                "stdout",
                "screen",
                "terminalOutput",
            } and isinstance(child, str) and child.strip():
                values.append(child)
            values.extend(output_strings(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(output_strings(child))
    return values


def terminal_handles(value: Any) -> list[str]:
    handles: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "handle",
                "terminalHandle",
                "terminal_handle",
                "terminalId",
                "terminal_id",
            } and isinstance(child, (str, int)):
                rendered = str(child).strip()
                if rendered:
                    handles.append(rendered)
            handles.extend(terminal_handles(child))
    elif isinstance(value, list):
        for child in value:
            handles.extend(terminal_handles(child))
    return list(dict.fromkeys(handles))


def first_string(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, (str, int)):
                rendered = str(child).strip()
                if rendered:
                    return rendered
            found = first_string(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_string(child, keys)
            if found:
                return found
    return None


def list_terminals(
    *,
    selector: str,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    return run(
        ["orca", "terminal", "list", "--worktree", selector, "--json"],
        cwd=cwd,
        env=env,
        timeout=60,
    )


def resolve_parent_terminal(
    *,
    selector: str,
    explicit: str | None,
    command: str,
    title: str,
    cwd: Path,
    env: dict[str, str],
) -> str:
    before = list_terminals(selector=selector, cwd=cwd, env=env)
    existing = set(terminal_handles(before))
    if explicit:
        if explicit not in existing:
            raise RuntimeError(
                "explicit parent terminal is not attached to smoke repository"
            )
        return explicit

    created = run(
        [
            "orca",
            "terminal",
            "create",
            "--worktree",
            selector,
            "--command",
            command,
            "--title",
            title,
            "--json",
        ],
        cwd=cwd,
        env=env,
        timeout=120,
    )
    terminal = first_string(
        created,
        {
            "handle",
            "terminalHandle",
            "terminal_handle",
            "terminalId",
            "terminal_id",
        },
    )
    if terminal in existing:
        terminal = None
    if terminal:
        return terminal

    for _ in range(20):
        current = list_terminals(selector=selector, cwd=cwd, env=env)
        candidates = [
            handle for handle in terminal_handles(current)
            if handle not in existing
        ]
        if candidates:
            return candidates[-1]
        time.sleep(0.5)
    raise RuntimeError("could not create parent terminal in smoke repository")


def read_terminal(
    *,
    terminal: str,
    cursor: str | None,
    cwd: Path,
    env: dict[str, str],
) -> tuple[str, str | None]:
    argv = [
        "orca",
        "terminal",
        "read",
        "--terminal",
        terminal,
        "--limit",
        "8000",
        "--json",
    ]
    if cursor:
        argv.extend(["--cursor", cursor])
    payload = run(argv, cwd=cwd, env=env, timeout=60)
    text = max(output_strings(payload), key=len, default="")
    next_cursor = first_string(payload, {"nextCursor", "next_cursor", "cursor"})
    return text, next_cursor


def wait_for_callback(
    *,
    task_id: str,
    actor: str,
    callback_type: str,
    parent_terminal: str,
    cursor: str | None,
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    transcript: list[dict[str, Any]],
) -> str | None:
    marker = (
        f"SIN_CALLBACK task={task_id} actor={actor} type={callback_type}"
    )
    current_cursor = cursor
    while time.monotonic() < deadline:
        text, next_cursor = read_terminal(
            terminal=parent_terminal,
            cursor=current_cursor,
            cwd=cwd,
            env=env,
        )
        found = marker in text
        transcript.append({
            "stage": f"callback:{actor}:{callback_type}",
            "terminal": parent_terminal,
            "observed_chars": len(text),
            "found": found,
        })
        if found:
            return next_cursor or current_cursor
        if next_cursor:
            current_cursor = next_cursor
        time.sleep(1)
    raise TimeoutError(
        f"timed out waiting for direct callback: {actor}:{callback_type}"
    )


def wait_for_artifact(
    *,
    sin_orca: Path,
    task_id: str,
    actor: str,
    filename: str,
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        payload = run(
            [str(sin_orca), "mailbox", task_id, "--actor", actor],
            cwd=cwd,
            env=env,
            timeout=90,
            require_ok=False,
        )
        transcript.append({
            "stage": f"mailbox:{actor}",
            "exit_code": payload.get("_exit_code"),
            "update_count": len(payload.get("updates", [])),
            "warning_count": len(payload.get("warnings", [])),
        })
        for update in payload.get("updates", []):
            if not isinstance(update, dict):
                continue
            if update.get("filename") == filename and update.get("ok") is True:
                return update
            if update.get("filename") == filename and update.get("ok") is False:
                raise RuntimeError(
                    f"{actor} produced invalid {filename}: {update.get('error')}"
                )
        warnings = payload.get("warnings", [])
        if warnings:
            raise RuntimeError(f"actor warning while waiting for {filename}: {warnings}")

        status = run(
            [str(sin_orca), "status", task_id],
            cwd=cwd,
            env=env,
            timeout=60,
        )
        archived = (
            filename == "checkpoint.json" and bool(status.get("checkpoints"))
        ) or (
            filename == "report.json" and status.get("report_received") is True
        ) or (
            filename == "review.json" and status.get("review_received") is True
        )
        if archived:
            transcript.append({
                "stage": f"artifact:{actor}:{filename}",
                "archived": True,
            })
            return {
                "ok": True,
                "filename": filename,
                "archived": True,
            }
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for {actor}:{filename}")


def callback_pairs(status: dict[str, Any]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for callback in status.get("callbacks", []):
        if not isinstance(callback, dict):
            continue
        actor = callback.get("actor")
        callback_type = callback.get("type")
        if isinstance(actor, str) and isinstance(callback_type, str):
            result.add((actor, callback_type))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sst",
        type=Path,
        default=Path.home() / "dev" / "SIN-Save-Token",
    )
    parser.add_argument("--agent", default="mimo-code")
    parser.add_argument("--parent-terminal")
    parser.add_argument("--parent-command", default="zsh")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--simone-task-id")
    args = parser.parse_args()

    sst = args.sst.expanduser().resolve()
    sin_orca = sst / "bin" / "sin-orca"
    if not sin_orca.is_file():
        print(json.dumps({"ok": False, "error": f"missing {sin_orca}"}, indent=2))
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = (
        Path.home()
        / ".local"
        / "state"
        / "sin-orca-smoke"
        / f"run-{stamp}-{os.getpid()}"
    )
    repository = run_root / "repository"
    state_root = run_root / "controller-state"
    repository.mkdir(parents=True, mode=0o700)

    run_git(repository, "init")
    run_git(repository, "config", "user.email", "smoke@opensin.local")
    run_git(repository, "config", "user.name", "OpenSIN Smoke")
    (repository / "README.md").write_text(
        "# Orca live smoke\n",
        encoding="utf-8",
    )
    run_git(repository, "add", "README.md")
    run_git(repository, "commit", "-m", "base")
    initial_head = run_git(repository, "rev-parse", "HEAD")

    env = {
        **os.environ,
        "SIN_ORCA_STATE_ROOT": str(state_root),
        "PYTHONPATH": str(sst / "lib"),
        "SIN_SAVE_TOKEN_HOME": str(sst),
    }
    transcript: list[dict[str, Any]] = []
    report_path = run_root / "live-smoke-report.json"
    report: dict[str, Any] = {
        "schema_version": 2,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "run_root": str(run_root),
        "repository": str(repository),
        "agent": args.agent,
        "stages": transcript,
    }

    deadline = time.monotonic() + max(120, args.timeout_seconds)
    try:
        selector = f"path:{repository}"
        parent_terminal = resolve_parent_terminal(
            selector=selector,
            explicit=args.parent_terminal,
            command=args.parent_command,
            title=f"sin-smoke-parent-{os.getpid()}",
            cwd=repository,
            env=env,
        )
        report["parent_terminal"] = parent_terminal
        _, parent_cursor = read_terminal(
            terminal=parent_terminal,
            cursor=None,
            cwd=repository,
            env=env,
        )

        verification_command = (
            "python3 -c \"from pathlib import Path; "
            "lines=Path('README.md').read_text(encoding='utf-8').splitlines(); "
            "raise SystemExit(0 if lines.count('ORCA_LIVE_SMOKE_OK') == 1 else 1)\""
        )
        dispatch_argv = [
            str(sin_orca),
            "dispatch",
            "--repo",
            str(repository),
            "--role",
            "implementer",
            "--agent",
            args.agent,
            "--approval-mode",
            "stepwise",
            "--parent-terminal",
            parent_terminal,
            "--objective",
            "Append the exact line 'ORCA_LIVE_SMOKE_OK' to README.md.",
            "--step",
            "Append the exact line ORCA_LIVE_SMOKE_OK to README.md and make no other changes.",
            "--allowed-path",
            "README.md",
            "--acceptance",
            "README.md contains exactly one line ORCA_LIVE_SMOKE_OK.",
            "--verify-command",
            verification_command,
            "--checkpoint",
            "plan-ready",
        ]
        if args.simone_task_id:
            dispatch_argv.extend(["--simone-task-id", args.simone_task_id])

        dispatched = run(
            dispatch_argv,
            cwd=repository,
            env=env,
            timeout=240,
        )
        transcript.append({
            "stage": "dispatch",
            "task_id": dispatched.get("task_id"),
            "terminal": dispatched.get("terminal"),
            "same_worktree": dispatched.get("same_worktree"),
        })
        task_id = str(dispatched["task_id"])
        report["task_id"] = task_id
        if dispatched.get("same_worktree") is not True:
            raise RuntimeError("worker dispatch did not use same-worktree mode")
        if dispatched.get("worktree_path") != str(repository):
            raise RuntimeError("worker dispatch selected a different repository")
        if dispatched.get("parent_terminal") != parent_terminal:
            raise RuntimeError("worker dispatch lost parent terminal binding")
        worker_terminal = str(dispatched["terminal"])
        if worker_terminal == parent_terminal:
            raise RuntimeError("worker reused the parent terminal")

        parent_cursor = wait_for_callback(
            task_id=task_id,
            actor="worker",
            callback_type="ack",
            parent_terminal=parent_terminal,
            cursor=parent_cursor,
            cwd=repository,
            env=env,
            deadline=deadline,
            transcript=transcript,
        )
        parent_cursor = wait_for_callback(
            task_id=task_id,
            actor="worker",
            callback_type="checkpoint",
            parent_terminal=parent_terminal,
            cursor=parent_cursor,
            cwd=repository,
            env=env,
            deadline=deadline,
            transcript=transcript,
        )

        approved = run(
            [
                str(sin_orca),
                "approve",
                task_id,
                "--step",
                "S01",
                "--instruction",
                "Proceed with only S01, then write the final report with concrete evidence.",
            ],
            cwd=repository,
            env=env,
            timeout=90,
        )
        transcript.append({"stage": "approve", "payload": approved})

        parent_cursor = wait_for_callback(
            task_id=task_id,
            actor="worker",
            callback_type="done",
            parent_terminal=parent_terminal,
            cursor=parent_cursor,
            cwd=repository,
            env=env,
            deadline=deadline,
            transcript=transcript,
        )

        verified = run(
            [str(sin_orca), "verify", task_id],
            cwd=repository,
            env=env,
            timeout=300,
        )
        if verified.get("ok") is not True:
            raise RuntimeError(f"controller verification failed: {verified}")
        transcript.append({
            "stage": "verify",
            "ok": verified.get("ok"),
            "diff_sha256": verified.get("diff_sha256"),
        })

        review_started = run(
            [str(sin_orca), "review", task_id],
            cwd=repository,
            env=env,
            timeout=240,
        )
        if review_started.get("same_worktree") is not True:
            raise RuntimeError("reviewer was not started in implementer worktree")
        if review_started.get("worktree_path") != str(repository):
            raise RuntimeError("reviewer selected another repository")
        reviewer_terminal = str(review_started["terminal"])
        if reviewer_terminal in {worker_terminal, parent_terminal}:
            raise RuntimeError("reviewer terminal is not independent")
        if review_started.get("reviewer_agent") == args.agent:
            raise RuntimeError("reviewer reused the implementer agent")
        transcript.append({
            "stage": "review-start",
            "terminal": reviewer_terminal,
            "agent": review_started.get("reviewer_agent"),
            "diff_sha256": review_started.get("diff_sha256"),
        })

        parent_cursor = wait_for_callback(
            task_id=task_id,
            actor="reviewer",
            callback_type="done",
            parent_terminal=parent_terminal,
            cursor=parent_cursor,
            cwd=repository,
            env=env,
            deadline=deadline,
            transcript=transcript,
        )

        completed = run(
            [str(sin_orca), "complete", task_id],
            cwd=repository,
            env=env,
            timeout=300,
        )
        if completed.get("status") != "completed":
            raise RuntimeError(f"completion gate rejected: {completed}")
        transcript.append({"stage": "complete", "payload": completed})

        status = run(
            [str(sin_orca), "status", task_id],
            cwd=repository,
            env=env,
            timeout=90,
        )
        required_callbacks = {
            ("worker", "ack"),
            ("worker", "checkpoint"),
            ("worker", "done"),
            ("reviewer", "done"),
        }
        missing_callbacks = required_callbacks - callback_pairs(status)
        if missing_callbacks:
            raise RuntimeError(
                f"controller ledger is missing direct callbacks: {sorted(missing_callbacks)}"
            )

        manifest_path = Path(str(completed["manifest"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        body = manifest.get("body", {})
        if body.get("changed_files") != ["README.md"]:
            raise RuntimeError("completion manifest has unexpected changed files")
        if not manifest.get("integrity", {}).get("hash"):
            raise RuntimeError("completion manifest integrity hash missing")
        if not body.get("diff_sha256"):
            raise RuntimeError("completion manifest diff hash missing")

        lines = (repository / "README.md").read_text(
            encoding="utf-8"
        ).splitlines()
        if lines.count("ORCA_LIVE_SMOKE_OK") != 1:
            raise RuntimeError("README smoke marker is not present exactly once")
        if run_git(repository, "rev-parse", "HEAD") != initial_head:
            raise RuntimeError("worker changed repository HEAD")

        worktree_paths = [
            line.removeprefix("worktree ")
            for line in run_git(repository, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]
        if worktree_paths != [str(repository)]:
            raise RuntimeError(
                f"unexpected Git worktrees created: {worktree_paths}"
            )

        if args.simone_task_id:
            synced = run(
                [
                    str(sin_orca),
                    "sync-simone",
                    task_id,
                    "--simone-task-id",
                    args.simone_task_id,
                ],
                cwd=repository,
                env=env,
                timeout=180,
            )
            transcript.append({"stage": "simone-sync", "payload": synced})

        report.update({
            "ok": True,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "manifest": str(manifest_path),
            "diff_sha256": body.get("diff_sha256"),
            "changed_files": body.get("changed_files"),
            "worker_terminal": worker_terminal,
            "reviewer_terminal": reviewer_terminal,
            "callbacks": sorted([list(item) for item in callback_pairs(status)]),
            "git_worktrees": worktree_paths,
            "head_unchanged": True,
        })
        atomic_json(report_path, report)
        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "report": str(report_path),
            "manifest": str(manifest_path),
            "run_root": str(run_root),
            "parent_terminal": parent_terminal,
            "worker_terminal": worker_terminal,
            "reviewer_terminal": reviewer_terminal,
            "git_worktrees": worktree_paths,
        }, indent=2))
        return 0
    except Exception as error:
        report.update({
            "ok": False,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": str(error),
        })
        atomic_json(report_path, report)
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "report": str(report_path),
            "run_root": str(run_root),
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
