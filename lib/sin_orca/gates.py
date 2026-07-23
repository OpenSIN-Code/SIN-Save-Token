"""Authoritative task completion gates."""

from __future__ import annotations

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


def step_ids(task: dict[str, Any]) -> list[str]:
    return [
        str(item["id"])
        for item in task.get("steps", [])
        if isinstance(item, dict) and item.get("id")
    ]


def _matching_event_sequences(
    events: list[dict[str, Any]],
    *,
    event_type: str,
    actor: str | None = None,
    callback_type: str | None = None,
    step_id: str | None = None,
) -> list[int]:
    matches: list[int] = []
    for event in events:
        if event.get("type") != event_type:
            continue
        if actor is not None and event.get("actor") != actor:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if callback_type is not None and payload.get("callback_type") != callback_type:
            continue
        if step_id is not None and payload.get("step_id") != step_id:
            continue
        sequence = event.get("sequence")
        if isinstance(sequence, int):
            matches.append(sequence)
    return matches


def _single_sequence(
    values: list[int],
    *,
    missing_error: str,
    duplicate_error: str,
    errors: list[str],
) -> int | None:
    if not values:
        errors.append(missing_error)
        return None
    if len(values) != 1:
        errors.append(duplicate_error)
        return None
    return values[0]


def execution_protocol_errors(task_id: str) -> list[str]:
    events = read_events(task_id, verify=True)
    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)
    errors: list[str] = []

    if any(event.get("type") == "task.failed" for event in events):
        errors.append("task contains an unrecovered failure event")
    if any(event.get("type") == "task.cancelled" for event in events):
        errors.append("task was cancelled")

    required_checkpoints = list(task.get("required_checkpoints", []))
    received_checkpoints = [
        item.get("checkpoint")
        for item in ledger.get("checkpoints", [])
        if item.get("actor") == "worker"
    ]
    if received_checkpoints != required_checkpoints:
        errors.append(
            "required checkpoints are incomplete or out of order"
        )

    expected_steps = step_ids(task)
    approved_steps = [
        str(message.get("step_id"))
        for message in ledger.get("controller_messages", [])
        if message.get("type") == "codex.approved"
        and message.get("step_id")
    ]
    approval_mode = task.get("approval_mode", "stepwise")
    if approval_mode not in {"continuous-preauthorized", "stepwise"}:
        errors.append(f"unsupported task approval mode: {approval_mode!r}")
    elif approval_mode == "stepwise":
        if approved_steps != expected_steps:
            errors.append(
                "stepwise task steps were not explicitly approved in order"
            )
    elif approved_steps:
        errors.append(
            "continuous-preauthorized task contains unexpected explicit approvals"
        )

    ack_sequence = _single_sequence(
        _matching_event_sequences(
            events,
            event_type="worker.callback",
            actor="worker",
            callback_type="ack",
        ),
        missing_error="worker direct ack callback missing",
        duplicate_error="worker direct ack callback is duplicated",
        errors=errors,
    )

    previous_boundary = ack_sequence or 0
    for index, step_id in enumerate(expected_steps):
        checkpoint_name = (
            required_checkpoints[index]
            if index < len(required_checkpoints)
            else None
        )
        checkpoint_events = [
            event
            for event in events
            if event.get("type") == "checkpoint.received"
            and event.get("actor") == "worker"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("step_id") == step_id
            and event["payload"].get("checkpoint") == checkpoint_name
        ]
        checkpoint_sequences = [
            event["sequence"]
            for event in checkpoint_events
            if isinstance(event.get("sequence"), int)
        ]
        checkpoint_sequence = _single_sequence(
            checkpoint_sequences,
            missing_error=f"{step_id} checkpoint artifact missing",
            duplicate_error=f"{step_id} checkpoint artifact is duplicated",
            errors=errors,
        )
        callback_sequence = _single_sequence(
            _matching_event_sequences(
                events,
                event_type="worker.callback",
                actor="worker",
                callback_type="checkpoint",
                step_id=step_id,
            ),
            missing_error=f"{step_id} direct checkpoint callback missing",
            duplicate_error=f"{step_id} direct checkpoint callback is duplicated",
            errors=errors,
        )

        if ack_sequence is not None:
            for label, sequence in (
                ("checkpoint artifact", checkpoint_sequence),
                ("checkpoint callback", callback_sequence),
            ):
                if sequence is not None and ack_sequence >= sequence:
                    errors.append(f"worker ack must precede {step_id} {label}")

        if (
            checkpoint_sequence is not None
            and callback_sequence is not None
            and checkpoint_sequence >= callback_sequence
        ):
            errors.append(
                f"{step_id} checkpoint callback must follow its artifact"
            )
        for label, sequence in (
            ("checkpoint artifact", checkpoint_sequence),
            ("checkpoint callback", callback_sequence),
        ):
            if sequence is not None and sequence <= previous_boundary:
                errors.append(
                    f"{step_id} {label} is out of step order"
                )

        if approval_mode == "stepwise":
            approval_sequence = _single_sequence(
                _matching_event_sequences(
                    events,
                    event_type="codex.approved",
                    actor="codex",
                    step_id=step_id,
                ),
                missing_error=f"{step_id} explicit approval missing",
                duplicate_error=f"{step_id} explicit approval is duplicated",
                errors=errors,
            )
            if (
                callback_sequence is not None
                and approval_sequence is not None
                and callback_sequence >= approval_sequence
            ):
                errors.append(
                    f"{step_id} approval must follow its checkpoint callback"
                )
            if (
                ack_sequence is not None
                and approval_sequence is not None
                and ack_sequence >= approval_sequence
            ):
                errors.append(
                    f"worker ack must precede {step_id} approval"
                )
            if approval_sequence is not None:
                previous_boundary = approval_sequence
        elif callback_sequence is not None:
            previous_boundary = callback_sequence

    report_sequences = _matching_event_sequences(
        events,
        event_type="worker.report.received",
        actor="worker",
    )
    done_sequences = _matching_event_sequences(
        events,
        event_type="worker.callback",
        actor="worker",
        callback_type="done",
    )
    report_sequence = None
    done_sequence = None
    if isinstance(ledger.get("report"), dict):
        report_sequence = _single_sequence(
            report_sequences,
            missing_error="worker report event missing",
            duplicate_error="worker report event is duplicated",
            errors=errors,
        )
        done_sequence = _single_sequence(
            done_sequences,
            missing_error="worker direct done callback missing",
            duplicate_error="worker direct done callback is duplicated",
            errors=errors,
        )
        for label, sequence in (
            ("worker report", report_sequence),
            ("worker done callback", done_sequence),
        ):
            if sequence is not None and sequence <= previous_boundary:
                errors.append(
                    f"{label} arrived before the final protocol boundary"
                )
        if (
            report_sequence is not None
            and done_sequence is not None
            and report_sequence >= done_sequence
        ):
            errors.append(
                "worker done callback must follow the accepted worker report"
            )

        verification_sequences = _matching_event_sequences(
            events,
            event_type="verification.completed",
            actor="controller",
        )
        if verification_sequences:
            first_verification = min(verification_sequences)
            for label, sequence in (
                ("worker report", report_sequence),
                ("worker done callback", done_sequence),
            ):
                if sequence is not None and sequence >= first_verification:
                    errors.append(f"{label} arrived after controller verification")
    elif done_sequences:
        errors.append("worker done callback exists without accepted report")

    return list(dict.fromkeys(errors))


def completion_errors(
    task_id: str,
    *,
    actual_changed_files: list[str],
    actual_diff_sha256: str | None = None,
) -> list[str]:
    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)
    events = read_events(task_id, verify=True)
    errors = execution_protocol_errors(task_id)

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

        reported_changed = sorted(
            str(path) for path in report.get("changed_files", [])
        ) if isinstance(report.get("changed_files"), list) else []
        if reported_changed != sorted(actual_changed_files):
            errors.append(
                "worker report changed_files does not match actual Git state"
            )

        if task.get("role") == "implementer" and not report.get("evidence"):
            errors.append("implementer report has no evidence")

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

        if scope.get("architecture_decisions_made") is not False:
            errors.append(
                "worker did not prove architecture-decision compliance"
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
    else:
        if verification.get("ok") is not True:
            errors.append("controller verification failed")
        verified_changed = verification.get("changed_files")
        if not isinstance(verified_changed, list) or sorted(
            str(path) for path in verified_changed
        ) != sorted(actual_changed_files):
            errors.append(
                "controller verification changed_files is stale"
            )
        if task.get("role") == "implementer" and not verification.get("results"):
            errors.append("controller verification has no test results")
        if actual_diff_sha256 is not None and (
            verification.get("diff_sha256") != actual_diff_sha256
        ):
            errors.append("controller verification diff hash is stale")

    if task["role"] == "implementer":
        review = ledger.get("review")
        worker_actor = ledger.get("actors", {}).get("worker", {})
        reviewer_actor = ledger.get("actors", {}).get("reviewer", {})

        if not isinstance(reviewer_actor, dict):
            errors.append("independent reviewer actor missing")
        else:
            if reviewer_actor.get("agent") == worker_actor.get("agent"):
                errors.append("implementer and reviewer use the same agent")
            if reviewer_actor.get("same_worktree") is not True:
                errors.append("reviewer did not inspect the implementer worktree")
            if actual_diff_sha256 is not None and (
                reviewer_actor.get("diff_sha256") != actual_diff_sha256
            ):
                errors.append("reviewer was spawned for a stale diff")

        reviewer_callbacks = [
            item
            for item in ledger.get("callbacks", [])
            if isinstance(item, dict)
            and item.get("actor") == "reviewer"
            and item.get("callback_type") == "done"
        ]
        if len(reviewer_callbacks) != 1:
            errors.append(
                "reviewer direct done callback missing or duplicated"
            )

        reviewer_spawn_sequence = _single_sequence(
            _matching_event_sequences(
                events,
                event_type="reviewer.spawned",
                actor="reviewer",
            ),
            missing_error="reviewer spawn event missing",
            duplicate_error="reviewer spawn event is duplicated",
            errors=errors,
        )
        review_sequence = _single_sequence(
            _matching_event_sequences(
                events,
                event_type="review.completed",
                actor="reviewer",
            ),
            missing_error="blind review event missing",
            duplicate_error="blind review event is duplicated",
            errors=errors,
        )
        reviewer_done_sequence = _single_sequence(
            _matching_event_sequences(
                events,
                event_type="reviewer.callback",
                actor="reviewer",
                callback_type="done",
            ),
            missing_error="reviewer direct done callback missing",
            duplicate_error="reviewer direct done callback is duplicated",
            errors=errors,
        )
        verification_sequences = _matching_event_sequences(
            events,
            event_type="verification.completed",
            actor="controller",
        )
        if reviewer_spawn_sequence is not None and verification_sequences:
            if reviewer_spawn_sequence <= max(verification_sequences):
                errors.append("reviewer was spawned before final verification")
        for label, sequence in (
            ("blind review", review_sequence),
            ("reviewer done callback", reviewer_done_sequence),
        ):
            if (
                sequence is not None
                and reviewer_spawn_sequence is not None
                and sequence <= reviewer_spawn_sequence
            ):
                errors.append(f"{label} arrived before reviewer spawn")
        if (
            review_sequence is not None
            and reviewer_done_sequence is not None
            and review_sequence >= reviewer_done_sequence
        ):
            errors.append(
                "reviewer done callback must follow the accepted review"
            )
        completion_sequences = _matching_event_sequences(
            events,
            event_type="task.completed",
            actor="controller",
        )
        if completion_sequences:
            first_completion = min(completion_sequences)
            for label, sequence in (
                ("blind review", review_sequence),
                ("reviewer done callback", reviewer_done_sequence),
            ):
                if sequence is not None and sequence >= first_completion:
                    errors.append(f"{label} arrived after task completion")

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
            if len(reviewer_callbacks) == 1 and reviewer_callbacks[0].get(
                "verification_status"
            ) != review.get("verdict"):
                errors.append("reviewer callback verdict does not match review")

            if actual_diff_sha256 is not None and (
                review.get("diff_sha256") != actual_diff_sha256
            ):
                errors.append("blind review diff hash is stale or missing")

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

            criteria = [
                item for item in review.get("criteria", [])
                if isinstance(item, dict)
            ]
            criterion_keys = [item.get("id") for item in criteria]
            if len(criterion_keys) != len(set(criterion_keys)):
                errors.append("review contains duplicate criterion IDs")
            statuses = {
                item.get("id"): item.get("status")
                for item in criteria
            }

            for criterion_id in criterion_ids(task):
                if statuses.get(criterion_id) != "proven":
                    errors.append(
                        f"{criterion_id} was not proven"
                    )

    return errors
