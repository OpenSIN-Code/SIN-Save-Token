"""
sin_orca.mailbox – Artefakt-Mailbox statt Terminaltranscript.

Worker schreiben Artefakte in .sin-worker/outbox/.
Controller liest sie, validiert Identität und Reihenfolge,
speichert als Events.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .state import append_event, load_task, rebuild_ledger, task_dir

MAX_ARTIFACT_BYTES = 65536

ARTIFACT_EVENTS = {
    "checkpoint.json": "checkpoint.received",
    "report.json": "worker.report.received",
    "review.json": "review.completed",
}


def actor_worktree(task_id: str, actor: str) -> Path:
    ledger = rebuild_ledger(task_id)
    info = ledger.get("actors", {}).get(actor)
    if not isinstance(info, dict):
        raise RuntimeError(f"unknown actor: {actor}")
    path = info.get("worktree_path")
    if not isinstance(path, str) or not path:
        raise RuntimeError(f"actor {actor} has no worktree path")
    return Path(path).resolve()


def validate_identity(task: dict[str, Any], payload: dict[str, Any]) -> None:
    expected = {"task_id": task["task_id"], "task_hash": task["task_hash"], "base_sha": task["base_sha"]}
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeError(f"artifact {key} mismatch: expected {value!r}, got {payload.get(key)!r}")


def validate_checkpoint_order(task_id: str, payload: dict[str, Any]) -> None:
    task = load_task(task_id)
    ledger = rebuild_ledger(task_id)
    required = list(task.get("required_checkpoints", []))
    received = [item.get("checkpoint") for item in ledger.get("checkpoints", [])]
    checkpoint = payload.get("checkpoint")
    if checkpoint not in required:
        raise RuntimeError(f"unexpected checkpoint: {checkpoint!r}")
    expected_index = len(received)
    if expected_index >= len(required):
        raise RuntimeError("all required checkpoints were already received")
    expected = required[expected_index]
    if checkpoint != expected:
        raise RuntimeError(f"checkpoint out of order: expected {expected!r}, got {checkpoint!r}")
    sequence = payload.get("sequence")
    if sequence != expected_index + 1:
        raise RuntimeError(f"invalid checkpoint sequence: {sequence!r}")


def consume_file(*, task_id: str, actor: str, source: Path) -> dict[str, Any]:
    stat = source.stat()
    if stat.st_size > MAX_ARTIFACT_BYTES:
        raise RuntimeError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    raw = source.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("artifact is not a JSON object")
    task = load_task(task_id)
    validate_identity(task, payload)
    if source.name == "checkpoint.json":
        validate_checkpoint_order(task_id, payload)
    digest = hashlib.sha256(raw).hexdigest()
    destination = task_dir(task_id) / "artifacts" / actor / f"{source.stem}-{digest[:16]}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, destination)
    append_event(task_id, ARTIFACT_EVENTS[source.name], {**payload, "_artifact": {"sha256": digest, "path": str(destination), "size_bytes": len(raw)}}, actor=actor)
    processed = source.with_suffix(source.suffix + ".processed")
    os.replace(source, processed)
    return {"artifact": source.name, "sha256": digest, "payload": payload}


def consume_mailbox(task_id: str, actor: str) -> list[dict[str, Any]]:
    outbox = actor_worktree(task_id, actor) / ".sin-worker" / "outbox"
    if not outbox.exists():
        return []
    results: list[dict[str, Any]] = []
    for filename in ("checkpoint.json", "report.json", "review.json"):
        source = outbox / filename
        if source.is_file():
            results.append(consume_file(task_id=task_id, actor=actor, source=source))
    return results
