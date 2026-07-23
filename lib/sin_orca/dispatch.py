"""Actual Orca worktree and worker dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .state import (
    append_event,
    atomic_write_json,
    repository_root,
    save_task,
    sha256_json,
    task_dir,
    utc_now,
)
from .writer_reservation import acquire_writer, release_writer


def safe_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    return environment


def run_git(
    root: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env=env or safe_subprocess_environment(),
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or process.stdout.strip()
        )

    return process.stdout.strip()


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


def resolve_parent_terminal(explicit: str | None) -> str:
    candidates = [
        explicit,
        os.getenv("SIN_ORCA_PARENT_TERMINAL"),
        os.getenv("ORCA_TERMINAL_HANDLE"),
        os.getenv("ORCA_CURRENT_TERMINAL"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ValueError(
        "same-worktree dispatch requires --parent-terminal or "
        "SIN_ORCA_PARENT_TERMINAL"
    )


def create_baseline_commit(
    root: Path,
    task_id: str,
) -> tuple[str, str, str]:
    """Snapshot the exact live worktree without touching its real index or HEAD."""
    head_sha = run_git(root, "rev-parse", "HEAD")
    index_path = task_dir(task_id, root) / "baseline.index"
    index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        index_path.parent.chmod(0o700)
    except OSError:
        pass
    index_path.unlink(missing_ok=True)
    environment = safe_subprocess_environment()
    environment.update({
        "GIT_INDEX_FILE": str(index_path),
        "GIT_AUTHOR_NAME": "SIN Orca Controller",
        "GIT_AUTHOR_EMAIL": "sin-orca@localhost",
        "GIT_COMMITTER_NAME": "SIN Orca Controller",
        "GIT_COMMITTER_EMAIL": "sin-orca@localhost",
    })
    try:
        run_git(root, "read-tree", "HEAD", env=environment)
        run_git(root, "add", "-A", "--", ".", env=environment)
        run_git(
            root,
            "rm",
            "-r",
            "-q",
            "--cached",
            "--ignore-unmatch",
            "--",
            ".sin-worker",
            env=environment,
        )
        tree_sha = run_git(root, "write-tree", env=environment)
        baseline_sha = run_git(
            root,
            "commit-tree",
            tree_sha,
            "-p",
            head_sha,
            "-m",
            f"sin-orca baseline {task_id}",
            env=environment,
        )
        baseline_ref = f"refs/sin-orca/baselines/{task_id}"
        run_git(
            root,
            "update-ref",
            baseline_ref,
            baseline_sha,
            env=environment,
        )
    finally:
        index_path.unlink(missing_ok=True)
        index_path.with_suffix(index_path.suffix + ".lock").unlink(missing_ok=True)
    return head_sha, baseline_sha, baseline_ref


def baseline_ref_is_valid(
    root: Path,
    baseline_ref: Any,
    expected_sha: str,
) -> bool:
    if (
        not isinstance(baseline_ref, str)
        or not baseline_ref.startswith("refs/sin-orca/baselines/")
    ):
        return False
    try:
        return run_git(root, "rev-parse", baseline_ref) == expected_sha
    except RuntimeError:
        return False


def rollback_dispatch_resources(
    root: Path,
    *,
    task_id: str,
    baseline_ref: str | None,
    writer_reserved: bool,
) -> None:
    if baseline_ref:
        try:
            run_git(root, "update-ref", "-d", baseline_ref)
        except RuntimeError:
            pass
    if writer_reserved:
        try:
            release_writer(root, task_id=task_id, allow_missing=True)
        except RuntimeError:
            pass


def task_outbox(root: Path, task_id: str) -> Path:
    outbox = root / ".sin-worker" / "tasks" / task_id / "outbox"
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        outbox.chmod(0o700)
    except OSError:
        pass
    return outbox


def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse Orca JSON even when a launcher prints a short preamble."""
    stripped = text.strip()
    if not stripped:
        raise RuntimeError("orca returned empty output")

    candidates = [stripped]
    candidates.extend(
        stripped[index:]
        for index, character in enumerate(stripped)
        if character == "{"
    )
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value, _ = decoder.raw_decode(candidate.lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise RuntimeError("orca did not return a valid JSON object")


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

    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)

    process = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=environment,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or process.stdout.strip()
            or f"orca exited with {process.returncode}"
        )

    result = _parse_json_object(process.stdout)

    if result.get("ok") is False:
        error = result.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
        else:
            message = error
        raise RuntimeError(str(message or "orca reported failure"))

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
    *,
    worker_terminal: str,
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

    checkpoints = "\n".join(
        f"{index}. {checkpoint}"
        for index, checkpoint in enumerate(
            task.get("required_checkpoints", []),
            start=1,
        )
    ) or "(none)"

    approval_mode = "stepwise"
    approval_rules = """Do not execute any ordered step before receiving `CODEX APPROVED. Step <step-id>` for that exact step.
A checkpoint is evidence, never approval. Every checkpoint requires a stop and a fresh explicit approval."""
    protocol_steps = """1. Before inspecting or changing repository files, send an `ack` callback directly to the parent terminal.
2. Atomically write the checkpoint for the exact next step, emit its ready marker, send a `checkpoint` callback with that step ID, and stop.
3. Wait for Codex approval naming the exact next step ID.
4. Execute only that approved step.
5. Prepare the next checkpoint and stop again before any later step.
6. Stop immediately on discovery outside scope, material ambiguity, ownership conflict, unsafe action, repeated failure, or parent interrupt.
7. Write the final report only after every listed step and required verification are complete.
8. After the report exists, send a `done` callback directly to the parent terminal."""

    return f"""# SIN WORKER CONTRACT

Task ID: {task["task_id"]}
Parent task ID: {task.get("parent_task_id") or "none"}
Task hash: {task["task_hash"]}
Repository HEAD at dispatch: {task["repository_head_sha"]}
Baseline snapshot SHA: {task["base_sha"]}
Repository root: {task["repository_root"]}
Worktree selector: {task["worktree_selector"]}
Parent terminal: {task["parent_terminal_handle"]}
Your terminal: {worker_terminal}
Role: {task["role"]}
Edits allowed: {"yes" if task.get("allow_edits") else "no"}
Child delegation allowed: {"yes" if task.get("allow_child_delegation") else "no"}
Approval mode: {approval_mode}

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
Do not create a worktree or branch.
Do not commit, merge, push or rebase.
Do not use sleep or polling to coordinate with the parent.
{approval_rules}
A worker report is not parent verification.

## Required checkpoint order

{checkpoints}

Protocol:

{protocol_steps}

Direct callback helper:

```bash
sin-orca notify {task["task_id"]} \
  --type <ack|checkpoint|discovery|question|blocked|child-dispatched|done> \
  --step <step-id-only-for-checkpoint> \
  --summary "<short factual summary>" \
  --changed "<comma-separated files or none>" \
  --verify "<status>" \
  --action "<requested parent action or none>"
```

Omit `--step` for non-checkpoint callbacks. The callback must reach parent terminal `{task["parent_terminal_handle"]}`. Large evidence belongs in the task artifact; callbacks contain only concise facts and paths.

Each checkpoint must include `checkpoint`, `sequence`, `step_id`, `status`,
`changed_files`, `commands`, `unresolved`, and `child_process_running`.
Each command entry should include `command`, `exit_code`, and either `error`
or `stderr` when it failed. Do not include full terminal transcripts.

The final report must include `status="complete"`, `changed_files`, a non-empty
`evidence` list, `commands`, `unresolved`, and:

```json
"scope_compliance": {{
  "outside_allowlist_touched": false,
  "unrequested_dependencies_added": false,
  "architecture_decisions_made": false
}}
```

Write structured artifacts only to:

{task["artifact_outbox"]}/checkpoint.json
{task["artifact_outbox"]}/report.json

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
    parent_terminal: str | None = None,
    parent_task_id: str | None = None,
    allow_child_delegation: bool = False,
    simone_task_id: str | None = None,
) -> dict[str, Any]:
    if role not in {"explorer", "librarian", "implementer", "reviewer"}:
        raise ValueError(f"unsupported worker role: {role}")
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent must be a non-empty string")
    agent = agent.strip()
    if role != "implementer" and allow_edits:
        raise ValueError(
            "only implementer tasks may edit; use --read-only for other roles"
        )

    candidate_root = (
        Path(repository).expanduser().resolve()
        if repository
        else repository_root()
    )
    if not candidate_root.is_dir():
        raise ValueError(f"repository does not exist: {candidate_root}")
    root = Path(
        run_git(candidate_root, "rev-parse", "--show-toplevel")
    ).resolve()
    parent_handle = resolve_parent_terminal(parent_terminal)
    selector = f"path:{root}"
    if role == "implementer":
        missing: list[str] = []
        if not steps:
            missing.append("steps")
        if not allowed_paths:
            missing.append("allowed_paths")
        if not acceptance_criteria:
            missing.append("acceptance_criteria")
        if not verification_commands:
            missing.append("verification_commands")
        if not required_checkpoints:
            missing.append("required_checkpoints")
        if missing:
            raise ValueError(
                "implementer task is missing required fields: "
                + ", ".join(missing)
            )
        if len(required_checkpoints) != len(steps):
            raise ValueError(
                "implementer tasks require exactly one checkpoint per step"
            )
        if len(set(required_checkpoints)) != len(required_checkpoints):
            raise ValueError("required checkpoints must be unique")

    task_id = make_task_id(role, objective)
    writer_reservation: dict[str, Any] | None = None
    baseline_ref: str | None = None
    try:
        if allow_edits:
            writer_reservation = acquire_writer(
                root,
                task_id=task_id,
                parent_task_id=(
                    parent_task_id.strip() if parent_task_id else None
                ),
            )
        repository_head_sha, base_sha, baseline_ref = create_baseline_commit(
            root,
            task_id,
        )
        outbox = task_outbox(root, task_id)
    except Exception:
        rollback_dispatch_resources(
            root,
            task_id=task_id,
            baseline_ref=baseline_ref,
            writer_reserved=writer_reservation is not None,
        )
        raise

    assert baseline_ref is not None

    task: dict[str, Any] = {
        "schema_version": 2,
        "task_id": task_id,
        "parent_task_id": parent_task_id.strip() if parent_task_id else None,
        "created_at": utc_now(),
        "repository_root": str(root),
        "repository_head_sha": repository_head_sha,
        "base_sha": base_sha,
        "baseline_ref": baseline_ref,
        "workspace_mode": "same-worktree",
        "worktree_selector": selector,
        "parent_terminal_handle": parent_handle,
        "artifact_outbox": str(outbox.relative_to(root)),
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
        "allow_child_delegation": bool(allow_child_delegation),
        "approval_mode": "stepwise",
        "writer_reservation": writer_reservation,
    }
    if simone_task_id:
        task["simone_task_id"] = simone_task_id.strip()

    hash_material = dict(task)
    task["task_hash"] = (
        "sha256:"
        + sha256_json(hash_material)
    )

    try:
        save_task(task, root=root)
        append_event(
            task_id,
            "task.created",
            {
                "task_hash": task["task_hash"],
                "base_sha": base_sha,
                "baseline_ref": baseline_ref,
                "repository_head_sha": repository_head_sha,
                "workspace_mode": "same-worktree",
                "worktree_selector": selector,
                "parent_terminal_handle": parent_handle,
                "approval_mode": "stepwise",
                "role": role,
            },
            actor="codex",
            root=root,
        )
    except Exception:
        rollback_dispatch_resources(
            root,
            task_id=task_id,
            baseline_ref=baseline_ref,
            writer_reserved=writer_reservation is not None,
        )
        raise

    try:
        before_result = run_orca(
            ["terminal", "list", "--worktree", selector],
            timeout=30,
        )
        existing_handles = set(terminal_handles(before_result))
        if parent_handle not in existing_handles:
            raise RuntimeError(
                "parent terminal is not attached to the selected repository worktree"
            )

        created = run_orca([
            "terminal",
            "create",
            "--worktree",
            selector,
            "--command",
            agent,
            "--title",
            task_id,
        ])
        terminal = first_string(
            created.get("result", created),
            {
                "handle",
                "terminalHandle",
                "terminal_handle",
                "terminalId",
                "terminal_id",
            },
        )
        if terminal in existing_handles:
            terminal = None

        if not terminal:
            for _ in range(20):
                after_result = run_orca(
                    ["terminal", "list", "--worktree", selector],
                    timeout=30,
                )
                candidates = [
                    handle
                    for handle in terminal_handles(after_result)
                    if handle not in existing_handles
                    and handle != parent_handle
                ]
                if candidates:
                    terminal = candidates[-1]
                    break
                time.sleep(0.5)

        if not terminal:
            raise RuntimeError(
                "worker terminal could not be created in current worktree"
            )
    except Exception as error:
        append_event(
            task_id,
            "task.failed",
            {
                "stage": "same-worktree-terminal-dispatch",
                "selector": selector,
                "error": str(error),
            },
            actor="controller",
            root=root,
        )
        rollback_dispatch_resources(
            root,
            task_id=task_id,
            baseline_ref=baseline_ref,
            writer_reserved=writer_reservation is not None,
        )
        raise

    prompt = render_worker_prompt(task, worker_terminal=terminal)
    prompt_file = task_dir(task_id, root) / "worker-prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o600)

    try:
        run_orca([
            "terminal",
            "send",
            "--terminal",
            terminal,
            "--text",
            prompt,
            "--enter",
        ])
    except Exception as error:
        append_event(
            task_id,
            "task.failed",
            {
                "stage": "worker-prompt-send",
                "terminal_handle": terminal,
                "error": str(error),
            },
            actor="controller",
            root=root,
        )
        rollback_dispatch_resources(
            root,
            task_id=task_id,
            baseline_ref=baseline_ref,
            writer_reserved=writer_reservation is not None,
        )
        raise

    append_event(
        task_id,
        "worker.spawned",
        {
            "agent": agent,
            "worktree_path": str(root),
            "worktree_selector": selector,
            "terminal_handle": terminal,
            "parent_terminal_handle": parent_handle,
            "same_worktree": True,
            "approval_mode": "stepwise",
            "outbox_path": str(outbox),
        },
        actor="worker",
        root=root,
    )
    atomic_write_json(
        task_dir(task_id, root) / "activity.json",
        {
            "worker_last_active": utc_now(),
            "worker_child_process_running": False,
        },
    )

    return {
        "task_id": task_id,
        "task_hash": task["task_hash"],
        "base_sha": base_sha,
        "baseline_ref": baseline_ref,
        "repository_head_sha": repository_head_sha,
        "agent": agent,
        "selector": selector,
        "terminal": terminal,
        "parent_terminal": parent_handle,
        "worktree_path": str(root),
        "same_worktree": True,
        "approval_mode": "stepwise",
        "artifact_outbox": str(outbox),
        "status": "awaiting-ack",
        "simone_task_id": task.get("simone_task_id"),
    }
