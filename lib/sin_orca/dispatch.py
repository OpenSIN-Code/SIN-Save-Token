"""Actual Orca worktree and worker dispatch."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .state import (
    append_event,
    repository_root,
    save_task,
    sha256_json,
    task_dir,
    utc_now,
)


def run_git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or process.stdout.strip()
        )

    return process.stdout.strip()


def run_orca(
    arguments: list[str],
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    binary = shutil.which("orca")

    if binary is None:
        raise RuntimeError("orca is not available on PATH")

    argv = [binary, *arguments]

    if "--json" not in argv:
        argv.append("--json")

    process = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or process.stdout.strip()
            or f"orca exited with {process.returncode}"
        )

    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "orca did not return valid JSON"
        ) from error

    if not isinstance(result, dict):
        raise RuntimeError("orca result is not an object")

    return result


def deep_values(
    value: Any,
    keys: set[str],
) -> list[Any]:
    results: list[Any] = []

    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                results.append(child)

            results.extend(
                deep_values(child, keys)
            )

    elif isinstance(value, list):
        for child in value:
            results.extend(
                deep_values(child, keys)
            )

    return results


def first_string(
    value: Any,
    keys: set[str],
) -> str | None:
    for candidate in deep_values(value, keys):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

        if isinstance(candidate, int):
            return str(candidate)

    return None


def make_task_id(
    role: str,
    objective: str,
) -> str:
    safe_role = re.sub(
        r"[^a-z0-9-]+",
        "-",
        role.lower(),
    ).strip("-")

    material = (
        f"{time.time_ns()}:"
        f"{uuid.uuid4().hex}:"
        f"{objective}"
    )

    digest = hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:10]

    return f"{safe_role}-{digest}"


def render_worker_prompt(
    task: dict[str, Any],
) -> str:
    steps = "\n".join(
        f"{step['id']}. {step['instruction']}"
        for step in task["steps"]
    )

    allowed = "\n".join(
        f"- {path}"
        for path in task["allowed_paths"]
    )

    acceptance = "\n".join(
        f"- {item['id']}: {item['text']}"
        for item in task["acceptance_criteria"]
    )

    return f"""# SIN WORKER CONTRACT

Task ID: {task["task_id"]}
Task hash: {task["task_hash"]}
Base SHA: {task["base_sha"]}
Role: {task["role"]}

## Objective

{task["objective"]}

## Allowed paths

{allowed}

## Ordered steps

{steps}

## Acceptance criteria

{acceptance}

Do not make architecture decisions.
Do not edit outside the allowlist.
Do not commit, merge, push or rebase.
Execute only explicitly approved steps.

Write structured artifacts only to:

.sin-worker/outbox/checkpoint.json
.sin-worker/outbox/report.json

Every artifact must contain:

- task_id
- task_hash
- base_sha

Emit `SIN_ARTIFACT_READY <filename>` after atomically writing an artifact.
"""


def dispatch_task(
    *,
    role: str,
    objective: str,
    steps: list[str],
    allowed_paths: list[str],
    forbidden_paths: list[str],
    acceptance_criteria: list[str],
    agent: str,
    verification_commands: list[list[str]],
    required_checkpoints: list[str],
    allow_edits: bool,
    repository: str | None = None,
    setup: str = "none",
    simone_task_id: str | None = None,
) -> dict[str, Any]:
    root = (
        Path(repository).expanduser().resolve()
        if repository
        else repository_root()
    )
    if not root.is_dir():
        raise ValueError(f"repository does not exist: {root}")

    base_sha = run_git(root, "rev-parse", "HEAD")
    task_id = make_task_id(role, objective)

    task: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "created_at": utc_now(),
        "repository_root": str(root),
        "base_sha": base_sha,
        "role": role,
        "agent": agent,
        "objective": objective,
        "steps": [
            {
                "id": f"S{index:02d}",
                "instruction": value,
            }
            for index, value in enumerate(
                steps,
                start=1,
            )
        ],
        "allowed_paths": allowed_paths,
        "forbidden_paths": forbidden_paths,
        "acceptance_criteria": [
            {
                "id": f"AC{index:02d}",
                "text": value,
            }
            for index, value in enumerate(
                acceptance_criteria,
                start=1,
            )
        ],
        "verification_commands": verification_commands,
        "required_checkpoints": required_checkpoints,
        "allow_edits": allow_edits,
    }
    if simone_task_id:
        task["simone_task_id"] = simone_task_id.strip()

    hash_material = dict(task)
    task["task_hash"] = (
        "sha256:"
        + sha256_json(hash_material)
    )

    save_task(task, root=root)

    append_event(
        task_id,
        "task.created",
        {
            "task_hash": task["task_hash"],
            "base_sha": base_sha,
            "role": role,
        },
        actor="codex",
        root=root,
    )

    prompt = render_worker_prompt(task)

    prompt_file = task_dir(task_id, root) / "worker-prompt.md"
    prompt_file.write_text(
        prompt,
        encoding="utf-8",
    )

    arguments = [
        "worktree",
        "create",
        "--name",
        task_id,
        "--agent",
        agent,
        "--prompt",
        prompt,
        "--setup",
        setup,
    ]

    arguments.extend(
        ["--repo", str(root)]
    )

    try:
        result = run_orca(arguments)
    except Exception as error:
        append_event(
            task_id,
            "task.failed",
            {
                "stage": "worker-dispatch",
                "error": str(error),
            },
            actor="controller",
            root=root,
        )
        raise

    worktree_id = first_string(
        result,
        {"worktreeId", "worktree_id", "id"},
    )
    worktree_path = first_string(
        result,
        {"worktreePath", "worktree_path", "path"},
    )
    branch = first_string(
        result,
        {"branch", "branchName", "branch_name"},
    )

    if worktree_id:
        selector = f"id:{worktree_id}"
    elif worktree_path:
        selector = f"path:{worktree_path}"
    elif branch:
        selector = f"branch:{branch}"
    else:
        raise RuntimeError(
            "Orca did not return a usable worktree selector"
        )

    terminal: str | None = None

    for _ in range(20):
        terminal_result = run_orca(
            [
                "terminal",
                "list",
                "--worktree",
                selector,
            ],
            timeout=30,
        )

        terminal = first_string(
            terminal_result,
            {
                "handle",
                "terminalHandle",
                "terminal_handle",
                "terminalId",
                "terminal_id",
            },
        )

        if terminal:
            break

        time.sleep(0.5)

    if not terminal:
        append_event(
            task_id,
            "task.failed",
            {
                "stage": "terminal-discovery",
                "selector": selector,
            },
            actor="controller",
            root=root,
        )
        raise RuntimeError(
            "worker terminal could not be discovered"
        )

    append_event(
        task_id,
        "worker.spawned",
        {
            "agent": agent,
            "worktree_id": worktree_id,
            "worktree_path": worktree_path,
            "branch": branch,
            "worktree_selector": selector,
            "terminal_handle": terminal,
        },
        actor="worker",
        root=root,
    )

    return {
        "task_id": task_id,
        "task_hash": task["task_hash"],
        "base_sha": base_sha,
        "agent": agent,
        "selector": selector,
        "terminal": terminal,
        "worktree_path": worktree_path,
        "status": "awaiting-ack",
        "simone_task_id": task.get("simone_task_id"),
    }
