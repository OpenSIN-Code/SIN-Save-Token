"""Blind review — new terminal in the implementer's worktree, not a fresh checkout."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sin_context.evidence_firewall import render_for_model, wrap_evidence
from sin_review_context import ReviewContextBuilder

from .dispatch import (
    baseline_ref_is_valid,
    first_string,
    run_git,
    run_orca,
    terminal_handles,
)
from .gates import execution_protocol_errors
from .state import (
    append_event,
    atomic_write_json,
    load_task,
    rebuild_ledger,
    task_dir,
)
from .verification import actual_changed_files, bounded_diff
from .writer_reservation import reservation_status


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

    protocol_errors = execution_protocol_errors(task_id)
    if not isinstance(ledger.get("report"), dict):
        protocol_errors.append("worker report missing")
    if protocol_errors:
        raise RuntimeError(
            "worker protocol is incomplete before review: "
            + "; ".join(protocol_errors)
        )

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
    if worker.get("same_worktree") is not True:
        raise RuntimeError("worker was not dispatched in same-worktree mode")
    expected_selector = f"path:{Path(task['repository_root']).resolve()}"
    if worktree_selector != expected_selector:
        raise RuntimeError("worker selector does not match task repository")

    reviewer_agent = select_reviewer_agent(
        preferred_agents=preferred_agents,
        implementer_agent=implementer_agent,
    )

    worktree = Path(worktree_path).resolve()
    if worktree != Path(task["repository_root"]).resolve():
        raise RuntimeError("worker worktree path does not match task repository")
    if task.get("allow_edits") is True:
        writer = reservation_status(worktree)
        if not isinstance(writer, dict) or writer.get("task_id") != task_id:
            raise RuntimeError("task does not own repository writer before review")
    current_head = run_git(worktree, "rev-parse", "HEAD")
    if current_head != task.get("repository_head_sha"):
        raise RuntimeError("worker changed repository HEAD before review")
    if not baseline_ref_is_valid(
        worktree,
        task.get("baseline_ref"),
        task["base_sha"],
    ):
        raise RuntimeError("task baseline reference is missing or changed")

    current_changed = actual_changed_files(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    verified_changed = verification.get("changed_files")
    if not isinstance(verified_changed, list) or sorted(
        str(path) for path in verified_changed
    ) != current_changed:
        raise RuntimeError(
            "worktree changed after controller verification; verify again"
        )

    diff = bounded_diff(
        worktree=worktree,
        base_sha=task["base_sha"],
    )
    if verification.get("diff_sha256") != diff["full_sha256"]:
        raise RuntimeError(
            "controller verification does not cover the current diff"
        )

    existing_reviewer = ledger.get("actors", {}).get("reviewer")
    if (
        isinstance(existing_reviewer, dict)
        and existing_reviewer.get("same_worktree") is True
        and existing_reviewer.get("diff_sha256") == diff["full_sha256"]
        and existing_reviewer.get("terminal_handle")
    ):
        return {
            "task_id": task_id,
            "reviewer_agent": existing_reviewer.get("agent"),
            "selector": worktree_selector,
            "terminal": existing_reviewer.get("terminal_handle"),
            "worktree_path": worktree_path,
            "same_worktree": True,
            "diff_sha256": diff["full_sha256"],
            "status": (
                "review-complete"
                if isinstance(ledger.get("review"), dict)
                else "review-running"
            ),
            "reused": True,
        }

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

    try:
        advisory_context = ReviewContextBuilder(worktree).build_review_context(
            task["base_sha"]
        )
    except (OSError, RuntimeError, ValueError) as error:
        advisory_context = {
            "schema_version": 1,
            "base_sha": task["base_sha"],
            "worktree": str(worktree),
            "changed_files": [
                {"path": path, "change_type": "unknown"}
                for path in current_changed
            ],
            "changed_symbols": [],
            "affected_flows": [],
            "test_gaps": [],
            "risk_signals": [],
            "crg_advisory": {
                "ok": False,
                "provider": "code-review-graph",
                "status": "context-build-error",
                "error_type": type(error).__name__,
                "authoritative": False,
            },
            "crg_authoritative": False,
            "graphify_paths": [],
            "uncertainties": [
                "review-context construction failed; inspect Git diff directly"
            ],
            "recommended_review_order": [],
            "total_risk_score": 0.0,
            "diff_hash": diff["full_sha256"],
            "diff_length": len(diff["text"]),
        }

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
        "review_context": advisory_context,
        "crg_authoritative": False,
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

{task["artifact_outbox"]}/review.json

Include:
- task_id
- task_hash
- base_sha
- verdict (accept or reject)
- criteria (array with id, status, evidence)
- diff_sha256 (must exactly equal `{diff["full_sha256"]}`)
- scope_violation (boolean)
- regressions (array)
- unverified (array)

Reject if the live `git diff` hash does not match the supplied diff_sha256.
After atomically writing the review, send a direct callback with:

sin-orca notify {task_id} --actor reviewer --type done --summary "blind review complete" --verify "<exact verdict: accept or reject>" --action "parent should run completion gate"

Review packet:

{json.dumps(packet, ensure_ascii=False, indent=2)}
"""

    # Snapshot existing terminals first so fallback discovery cannot select the
    # implementer's terminal or an older reviewer terminal.
    existing_handles = {
        str(worker.get("terminal_handle") or ""),
        str(task.get("parent_terminal_handle") or ""),
    }
    try:
        before = run_orca(
            [
                "terminal",
                "list",
                "--worktree",
                worktree_selector,
            ],
            timeout=30,
        )
        existing_handles.update(terminal_handles(before))
    except RuntimeError:
        pass
    existing_handles.discard("")

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

    if terminal in existing_handles:
        terminal = None

    if not terminal:
        # Terminal create might not return the handle directly. Poll until a
        # genuinely new handle appears; never reuse the implementer's terminal.
        for _ in range(20):
            term_result = run_orca(
                [
                    "terminal",
                    "list",
                    "--worktree",
                    worktree_selector,
                ],
                timeout=30,
            )
            candidates = [
                handle for handle in terminal_handles(term_result)
                if handle not in existing_handles
            ]
            if candidates:
                terminal = candidates[-1]
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
            "parent_terminal_handle": task.get("parent_terminal_handle"),
            "outbox_path": str(
                Path(task["repository_root"])
                / task["artifact_outbox"]
            ),
            "review_packet": str(packet_path),
            "same_worktree": True,
            "diff_sha256": diff["full_sha256"],
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
        "diff_sha256": diff["full_sha256"],
        "status": "review-running",
        "reused": False,
    }
