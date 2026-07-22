"""Authoritative task completion gates."""

from __future__ import annotations

import fnmatch
from typing import Any

from .state import load_task, read_events, rebuild_ledger
from .verification import path_allowed


def criterion_ids(
    task: dict[str, Any],
) -> list[str]:
    return [
        str(item["id"])
        for item in task["acceptance_criteria"]
    ]


def completion_errors(
    task_id: str,
    *,
    actual_changed_files: list[str],
) -> list[str]:
    read_events(task_id, verify=True)

    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)
    errors: list[str] = []

    required_checkpoints = task.get(
        "required_checkpoints",
        [],
    )

    received_checkpoints = [
        item.get("checkpoint")
        for item in ledger.get("checkpoints", [])
        if item.get("actor") == "worker"
    ]

    if received_checkpoints != required_checkpoints:
        errors.append(
            "required checkpoints are incomplete or out of order"
        )

    report = ledger.get("report")

    if not isinstance(report, dict):
        errors.append("worker report missing")
    else:
        if report.get("task_id") != task_id:
            errors.append("worker report task_id mismatch")

        if report.get("task_hash") != task["task_hash"]:
            errors.append("worker report task_hash mismatch")

        if report.get("base_sha") != task["base_sha"]:
            errors.append("worker report base_sha mismatch")

        if report.get("status") != "complete":
            errors.append("worker report is not complete")

        if report.get("unresolved"):
            errors.append("worker report contains unresolved work")

        scope = report.get("scope_compliance", {})

        if scope.get("outside_allowlist_touched") is not False:
            errors.append(
                "worker did not prove allowlist compliance"
            )

        if (
            scope.get("unrequested_dependencies_added")
            is not False
        ):
            errors.append(
                "worker did not prove dependency compliance"
            )

    outside = [
        path
        for path in actual_changed_files
        if not path_allowed(
            path,
            task["allowed_paths"],
        )
    ]

    if outside:
        errors.append(
            "changed files outside allowlist: "
            + ", ".join(outside)
        )

    forbidden = [
        path
        for path in actual_changed_files
        if path_allowed(
            path,
            task.get("forbidden_paths", []),
        )
    ]

    if forbidden:
        errors.append(
            "forbidden files changed: "
            + ", ".join(forbidden)
        )

    verification = ledger.get("verification")

    if not isinstance(verification, dict):
        errors.append("controller verification missing")
    elif verification.get("ok") is not True:
        errors.append("controller verification failed")

    if task["role"] == "implementer":
        review = ledger.get("review")

        if not isinstance(review, dict):
            errors.append("blind review missing")
        else:
            if review.get("task_id") != task_id:
                errors.append("review task_id mismatch")

            if review.get("task_hash") != task["task_hash"]:
                errors.append("review task_hash mismatch")

            if review.get("base_sha") != task["base_sha"]:
                errors.append("review base_sha mismatch")

            if review.get("verdict") != "accept":
                errors.append("blind reviewer rejected")

            if review.get("scope_violation") is not False:
                errors.append(
                    "reviewer found a scope violation"
                )

            if review.get("regressions"):
                errors.append("reviewer found regressions")

            if review.get("unverified"):
                errors.append(
                    "review contains unverified findings"
                )

            statuses = {
                item.get("id"): item.get("status")
                for item in review.get("criteria", [])
                if isinstance(item, dict)
            }

            for criterion_id in criterion_ids(task):
                if statuses.get(criterion_id) != "proven":
                    errors.append(
                        f"{criterion_id} was not proven"
                    )

    return errors
