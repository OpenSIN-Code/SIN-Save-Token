"""
sin_orca.state – Externer Controller-State mit Event-Log.

State liegt AUSSERHALB des Repositorys:
  ~/.local/state/sin-orca/<repository-id>/<task-id>/

Kein Worker schreibt direkt hinein.
"""

import fcntl
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ZERO_HASH = "0" * 64


def safe_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    return environment


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def run_git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=root, text=True, capture_output=True, check=False, timeout=15,
        env=safe_subprocess_environment(),
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return process.stdout.strip()


def repository_root() -> Path:
    process = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False, timeout=5,
        env=safe_subprocess_environment(),
    )
    if process.returncode == 0 and process.stdout.strip():
        return Path(process.stdout.strip()).resolve()
    return Path.cwd().resolve()


def repository_id(root: Path | None = None) -> str:
    root = (root or repository_root()).resolve()
    try:
        common_dir_raw = run_git(root, "rev-parse", "--git-common-dir")
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = (root / common_dir).resolve()
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=root, text=True, capture_output=True, check=False, timeout=5,
            env=safe_subprocess_environment(),
        ).stdout.strip()
        material = {"git_common_dir": str(common_dir), "remote": remote}
    except RuntimeError:
        material = {"path": str(root)}
    return sha256_json(material)[:24]


def state_base() -> Path:
    override = os.environ.get("SIN_ORCA_STATE_ROOT")
    base = (
        Path(override).expanduser()
        if override
        else Path.home() / ".local" / "state" / "sin-orca"
    )
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def state_root(root: Path | None = None) -> Path:
    result = state_base() / repository_id(root)
    result.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result.chmod(0o700)
    except OSError:
        pass
    return result


def task_dir(task_id: str, root: Path | None = None) -> Path:
    safe = "".join(c for c in task_id if c.isalnum() or c in "-_")
    if not safe or safe != task_id:
        raise ValueError(f"invalid task id: {task_id!r}")

    if root is not None:
        directory = state_root(root) / task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        return directory

    current = state_root() / task_id
    if current.exists():
        return current

    repository_bucket = current.parent.name
    looks_like_repository_id = (
        len(repository_bucket) == 24
        and all(
            character in "0123456789abcdef"
            for character in repository_bucket
        )
    )
    search_base = (
        current.parent.parent
        if looks_like_repository_id
        else current.parent
    )
    matches = sorted(
        path.parent
        for path in search_base.glob(f"*/{task_id}/task.json")
        if path.parent != current
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        locations = ", ".join(str(path) for path in matches)
        raise RuntimeError(
            f"task id is ambiguous across repositories: {locations}"
        )

    current.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        current.chmod(0o700)
    except OSError:
        pass
    return current


def task_path(task_id: str, root: Path | None = None) -> Path:
    return task_dir(task_id, root) / "task.json"


def events_path(task_id: str, root: Path | None = None) -> Path:
    return task_dir(task_id, root) / "events.jsonl"


def ledger_path(task_id: str, root: Path | None = None) -> Path:
    return task_dir(task_id, root) / "ledger.json"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_task(task: dict[str, Any], root: Path | None = None) -> None:
    atomic_write_json(task_path(task["task_id"], root), task)


def load_task(task_id: str, root: Path | None = None) -> dict[str, Any]:
    value = json.loads(task_path(task_id, root).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("task file does not contain an object")
    return value


def read_events(task_id: str, root: Path | None = None, *, verify: bool = True) -> list[dict[str, Any]]:
    path = events_path(task_id, root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise RuntimeError(f"invalid event at line {line_number}")
        events.append(event)
    if verify:
        previous_hash = ZERO_HASH
        for expected_sequence, event in enumerate(events, start=1):
            if event.get("sequence") != expected_sequence:
                raise RuntimeError(f"invalid event sequence at {expected_sequence}")
            if event.get("previous_hash") != previous_hash:
                raise RuntimeError(f"event chain broken at sequence {expected_sequence}")
            material = {k: event[k] for k in ("sequence", "type", "timestamp", "actor", "payload", "previous_hash")}
            expected_hash = sha256_json(material)
            if event.get("event_hash") != expected_hash:
                raise RuntimeError(f"event hash mismatch at sequence {expected_sequence}")
            previous_hash = expected_hash
    return events


def reduce_events(task_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    ledger: dict[str, Any] = {
        "task_id": task_id, "status": "unknown", "actors": {},
        "checkpoints": [], "controller_messages": [], "callbacks": [],
        "verification": None, "review": None, "report": None,
        "suspended": False, "updated_at": None,
    }
    for event in events:
        etype = event["type"]
        payload = event["payload"]
        actor = event["actor"]
        ledger["updated_at"] = event["timestamp"]
        if etype == "task.created":
            ledger["status"] = "created"
            ledger["task_hash"] = payload["task_hash"]
            ledger["base_sha"] = payload["base_sha"]
        elif etype == "worker.spawned":
            ledger["actors"][actor] = dict(payload)
            ledger["status"] = "awaiting-ack"
        elif etype == "reviewer.spawned":
            ledger["actors"][actor] = dict(payload)
            ledger["status"] = "review-running"
        elif etype == "checkpoint.received":
            ledger["checkpoints"].append({"actor": actor, **payload})
            ledger["status"] = f"checkpoint:{payload['checkpoint']}"
        elif etype in {"codex.sent", "codex.interrupted", "codex.approved", "codex.followup"}:
            ledger["controller_messages"].append({"type": etype, "actor": actor, **payload})
            ledger["status"] = "worker-running"
        elif etype in {"worker.callback", "reviewer.callback"}:
            ledger["callbacks"].append({"type": etype, "actor": actor, **payload})
            callback_type = payload.get("callback_type")
            if callback_type == "ack":
                ledger["status"] = "worker-acknowledged"
            elif callback_type == "discovery":
                ledger["status"] = "worker-discovery-received"
            elif callback_type == "question":
                ledger["status"] = "worker-question-received"
            elif callback_type == "blocked":
                ledger["status"] = "worker-blocked"
            elif callback_type == "done":
                ledger["status"] = (
                    "review-callback-received"
                    if actor == "reviewer"
                    else "worker-callback-received"
                )
        elif etype == "task.suspended":
            ledger["suspended"] = True
            ledger["status"] = "suspended"
        elif etype == "task.resumed":
            ledger["suspended"] = False
            ledger["status"] = "worker-running"
        elif etype == "worker.report.received":
            ledger["report"] = payload
            ledger["status"] = "report-received"
        elif etype == "verification.completed":
            ledger["verification"] = payload
            ledger["status"] = "verification-complete"
        elif etype == "review.completed":
            ledger["review"] = payload
            ledger["status"] = "review-complete"
        elif etype == "worker.blocked":
            ledger["worker_blocked"] = payload
            ledger["status"] = "worker-blocked"
        elif etype == "worker.stalled":
            ledger["worker_stalled"] = payload
            ledger["status"] = "worker-stalled"
        elif etype == "worker.recovered":
            ledger["worker_stalled"] = None
            ledger["worker_blocked"] = None
            ledger["status"] = "worker-running"
        elif etype == "task.completed":
            ledger["status"] = "completed"
        elif etype == "task.cancelled":
            ledger["status"] = "cancelled"
            ledger["cancelled"] = payload
        elif etype == "task.failed":
            ledger["status"] = "failed"
            ledger["failure"] = payload
    return ledger


def rebuild_ledger(task_id: str, root: Path | None = None) -> dict[str, Any]:
    ledger = reduce_events(task_id, read_events(task_id, root))
    atomic_write_json(ledger_path(task_id, root), ledger)
    return ledger


def append_event(
    task_id: str, event_type: str, payload: dict[str, Any],
    *, actor: str, root: Path | None = None,
) -> dict[str, Any]:
    directory = task_dir(task_id, root)
    lock_path = directory / ".events.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            os.fchmod(lock.fileno(), 0o600)
        except OSError:
            pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        events = read_events(task_id, root)
        sequence = len(events) + 1
        previous_hash = events[-1]["event_hash"] if events else ZERO_HASH
        material = {
            "sequence": sequence, "type": event_type,
            "timestamp": utc_now(), "actor": actor,
            "payload": payload, "previous_hash": previous_hash,
        }
        event = {**material, "event_hash": sha256_json(material)}
        with events_path(task_id, root).open("a", encoding="utf-8") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        ledger = reduce_events(task_id, [*events, event])
        atomic_write_json(ledger_path(task_id, root), ledger)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return ledger
