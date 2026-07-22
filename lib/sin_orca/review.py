"""Blind review that spawns a separate reviewer worker."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .dispatch import first_string, run_orca
from .state import (
    append_event,
    atomic_write_json,
    load_task,
    rebuild_ledger,
    task_dir,
)
from .verification import bounded_diff


def select_reviewer_agent(
    *,
    preferred_agents: list[str],
    implementer_agent: str,
) -> str:
    for candidate in preferred_agents:
        if candidate != implementer_agent:
            return candidate

    raise RuntimeError(
        "no independent reviewer agent is available"
    )


def start_blind_review(
    *,
    task_id: str,
    preferred_agents: list[str],
) -> dict[str, Any]:
    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)

    verification = ledger.get("verification")

    if (
        not isinstance(verification, dict)
        or verification.get("ok") is not True
    ):
        raise RuntimeError(
            "controller verification must be green "
            "before review"
        )

    worker = ledger.get("actors", {}).get("worker")

    if not isinstance(worker, dict):
        raise RuntimeError("worker actor is missing")

    implementer_agent = str(worker["agent"])

    reviewer_agent = select_reviewer_agent(
        preferred_agents=preferred_agents,
        implementer_agent=implementer_agent,
    )

    worktree = Path(
        worker["worktree_path"]
    ).resolve()

    diff = bounded_diff(
        worktree=worktree,
        base_sha=task["base_sha"],
    )

    packet = {
        "schema_version": 1,
        "task_id": task_id,
        "task_hash": task["task_hash"],
        "base_sha": task["base_sha"],
        "objective": task["objective"],
        "allowed_paths": task["allowed_paths"],
        "forbidden_paths": task.get(
            "forbidden_paths",
            [],
        ),
        "acceptance_criteria": task[
            "acceptance_criteria"
        ],
        "changed_files": verification[
            "changed_files"
        ],
        "bounded_diff": diff["text"],
        "diff_sha256": diff["full_sha256"],
        "controller_test_results": verification[
            "results"
        ],
    }

    packet_path = (
        task_dir(task_id)
        / "review-packet.json"
    )

    atomic_write_json(
        packet_path,
        packet,
    )

    prompt = f"""You are an independent blind reviewer.

You did not implement this change.

Do not trust implementer statements.
Use only the supplied task, actual Git diff and controller test results.

Every acceptance criterion must be marked:
- proven
- failed
- unverified

Any unverified criterion requires verdict=reject.

Write exactly:

.sin-worker/outbox/review.json

Include:
- task_id
- task_hash
- base_sha
- verdict
- criteria
- scope_violation
- regressions
- unverified

Review packet:

{json.dumps(packet, ensure_ascii=False, indent=2)}
"""

    result = run_orca(
        [
            "worktree",
            "create",
            "--name",
            f"{task_id}-review",
            "--agent",
            reviewer_agent,
            "--prompt",
            prompt,
            "--setup",
            "none",
        ]
    )

    reviewer_path = first_string(
        result,
        {"worktreePath", "worktree_path", "path"},
    )
    reviewer_id = first_string(
        result,
        {"worktreeId", "worktree_id", "id"},
    )

    selector = (
        f"id:{reviewer_id}"
        if reviewer_id
        else f"path:{reviewer_path}"
    )

    terminal_result = run_orca(
        [
            "terminal",
            "list",
            "--worktree",
            selector,
        ]
    )

    terminal = first_string(
        terminal_result,
        {
            "handle",
            "terminalHandle",
            "terminal_handle",
        },
    )

    if not terminal:
        raise RuntimeError(
            "reviewer terminal was not created"
        )

    append_event(
        task_id,
        "reviewer.spawned",
        {
            "agent": reviewer_agent,
            "worktree_path": reviewer_path,
            "worktree_selector": selector,
            "terminal_handle": terminal,
            "review_packet": str(packet_path),
        },
        actor="reviewer",
    )

    return {
        "task_id": task_id,
        "reviewer_agent": reviewer_agent,
        "selector": selector,
        "terminal": terminal,
        "status": "review-running",
    }
