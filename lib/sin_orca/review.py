"""Blind review that spawns a separate reviewer worker."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sin_context.evidence_firewall import render_for_model, wrap_evidence

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


def _compact_test_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
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
            if key in item
        }
        for item in value[:100]
        if isinstance(item, dict)
    ]


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

    diff_envelope = wrap_evidence(
        source=f"git-diff:{task_id}",
        source_type="repository-diff",
        content=diff["text"],
        trust_level="repository-untrusted",
        metadata={
            "base_sha": task["base_sha"],
            "full_sha256": diff["full_sha256"],
        },
    )
    safe_diff = render_for_model(
        diff_envelope,
        maximum_chars=60000,
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
        "bounded_diff": safe_diff,
        "diff_sha256": diff["full_sha256"],
        "diff_evidence": {
            "trust_level": diff_envelope.trust_level,
            "suspicious_instruction_spans": len(
                diff_envelope.suspicious
            ),
        },
        "controller_test_results": _compact_test_results(
            verification.get("results")
        ),
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
            "skip",
            "--repo",
            str(task.get("repository_root") or worktree),
        ]
    )

    rv_data = result.get("result", result)
    reviewer_path = first_string(
        rv_data,
        {"worktreePath", "worktree_path", "path"},
    )
    reviewer_id = first_string(
        rv_data,
        {"worktreeId", "worktree_id", "id"},
    )

    if reviewer_id:
        selector = f"id:{reviewer_id}"
    elif reviewer_path:
        selector = f"path:{reviewer_path}"
    else:
        raise RuntimeError(
            "Orca did not return a reviewer worktree selector"
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
            terminal_result.get("result", terminal_result),
            {
                "handle",
                "terminalHandle",
                "terminal_handle",
            },
        )
        if terminal:
            break
        time.sleep(0.5)

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
