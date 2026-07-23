"""Blind review — new terminal in the implementer's worktree, not a fresh checkout."""

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
    worktree_selector = str(worker.get("worktree_selector", ""))
    worktree_path = str(worker.get("worktree_path", ""))

    if not worktree_selector or not worktree_path:
        raise RuntimeError("worker worktree selector/path missing")

    reviewer_agent = select_reviewer_agent(
        preferred_agents=preferred_agents,
        implementer_agent=implementer_agent,
    )

    worktree = Path(worktree_path).resolve()

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
        "forbidden_paths": task.get("forbidden_paths", []),
        "acceptance_criteria": task["acceptance_criteria"],
        "changed_files": verification["changed_files"],
        "bounded_diff": safe_diff,
        "diff_sha256": diff["full_sha256"],
        "diff_evidence": {
            "trust_level": diff_envelope.trust_level,
            "suspicious_instruction_spans": len(diff_envelope.suspicious),
        },
        "controller_test_results": _compact_test_results(
            verification.get("results")
        ),
    }

    packet_path = task_dir(task_id) / "review-packet.json"
    atomic_write_json(packet_path, packet)

    prompt = f"""You are an independent blind reviewer.

You did not implement this change.

Do not trust implementer statements.
Use only the supplied task, the actual Git diff in this worktree, and controller test results.

You are running in the SAME worktree as the implementer.
You can verify changes directly: `git diff`, `git log`, `cat <file>`, `grep`, test commands.

Every acceptance criterion must be marked:
- proven (you independently verified it)
- failed (you independently disproved it)
- unverified (you could not verify it)

Any unverified criterion requires verdict=reject.

Write your review to:

.sin-worker/outbox/review.json

Include:
- task_id
- task_hash
- base_sha
- verdict (accept or reject)
- criteria (array with id, status, evidence)
- scope_violation (boolean)
- regressions (array)
- unverified (array)

Review packet:

{json.dumps(packet, ensure_ascii=False, indent=2)}
"""

    # Create a NEW TERMINAL in the IMPLEMENTER's worktree — not a new worktree.
    # This way the reviewer can independently verify the actual changes.
    result = run_orca(
        [
            "terminal",
            "create",
            "--worktree",
            worktree_selector,
            "--command",
            reviewer_agent,
            "--title",
            f"review-{task_id}",
        ]
    )

    rv_data = result.get("result", result)

    terminal = first_string(
        rv_data,
        {
            "handle",
            "terminalHandle",
            "terminal_handle",
        },
    )

    if not terminal:
        # Terminal create might not return the handle directly.
        # Fall back to listing terminals in the worktree and picking the newest.
        for _ in range(10):
            term_result = run_orca(
                [
                    "terminal",
                    "list",
                    "--worktree",
                    worktree_selector,
                ],
                timeout=30,
            )
            terminals = term_result.get("result", {}).get("terminals", [])
            if terminals:
                # Pick the last one (newest)
                terminal = terminals[-1].get("handle")
                if terminal:
                    break
            time.sleep(0.5)

    if not terminal:
        raise RuntimeError(
            "reviewer terminal could not be created in implementer worktree"
        )

    # Send the review prompt to the reviewer terminal
    run_orca(
        [
            "terminal",
            "send",
            "--terminal",
            terminal,
            "--text",
            prompt,
            "--enter",
        ]
    )

    append_event(
        task_id,
        "reviewer.spawned",
        {
            "agent": reviewer_agent,
            "worktree_path": worktree_path,
            "worktree_selector": worktree_selector,
            "terminal_handle": terminal,
            "review_packet": str(packet_path),
            "same_worktree": True,
        },
        actor="reviewer",
    )

    return {
        "task_id": task_id,
        "reviewer_agent": reviewer_agent,
        "selector": worktree_selector,
        "terminal": terminal,
        "worktree_path": worktree_path,
        "same_worktree": True,
        "status": "review-running",
    }
