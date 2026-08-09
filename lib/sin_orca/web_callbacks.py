"""Event-driven ChatGPT Web callbacks into an originating OpenCode terminal.

The terminal handle is the transport identity.  The OpenCode session ID is kept
as correlation metadata because a repository may have multiple sessions and a
TUI normally listens on a random local server port.  Callback capabilities are
short-lived, one-shot, repository-local files under ``.sin-gpt-web``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .dispatch import run_git, run_orca
from .state import atomic_write_json
from .verification import redact_text

CALLBACK_SCHEMA_VERSION = 1
TOKEN_PATTERN = re.compile(r"^gptwcb_[0-9a-f]{32}$")
RELAY_ID_PATTERN = re.compile(r"^gptwcr_[0-9a-f]{32}$")
DELIVERY_ID_PATTERN = re.compile(r"^gptwcd_[0-9a-f]{32}$")
SESSION_PATTERN = re.compile(r"^ses_[A-Za-z0-9]+$")
TASK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FINAL_STATUSES = {"done", "blocked", "failed"}
DEFAULT_TTL_MINUTES = 24 * 60
DEFAULT_MAX_ROUNDS = 50
DEFAULT_RELAY_INTERVAL_SECONDS = 60
DEFAULT_RELAY_MAX_ATTEMPTS = 3
EXPIRABLE_CALLBACK_STATUSES = {
    "open",
    "pending-delivery",
    "sent",
    "delivery-indeterminate",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def resolve_repository(value: str | Path) -> Path:
    candidate = Path(value).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"repository does not exist: {candidate}")
    root = Path(run_git(candidate, "rev-parse", "--show-toplevel")).resolve()
    return root


def callback_directory(repository: Path) -> Path:
    directory = repository / ".sin-gpt-web" / "callbacks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def callback_path(repository: Path, token: str) -> Path:
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("invalid callback token")
    return callback_directory(repository) / f"{token}.json"


@contextmanager
def callback_lock(repository: Path, token: str) -> Iterator[None]:
    lock_path = callback_directory(repository) / f".{token}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            os.fchmod(handle.fileno(), 0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def resolve_callback_token(
    repository: str | Path,
    *,
    task_id: str,
    round_number: int,
) -> str:
    """Resolve one repository-local callback without exposing its opaque token.

    The task/round selector is intentionally exact and refuses ambiguity. It is
    suitable for ChatGPT Web tool calls where an opaque capability value may be
    treated as sensitive by the connector safety layer.
    """
    root = resolve_repository(repository)
    rendered_task = task_id.strip()
    if not TASK_PATTERN.fullmatch(rendered_task):
        raise ValueError("invalid callback task ID")
    if round_number < 1 or round_number > 500:
        raise ValueError("callback round must be between 1 and 500")

    matches: list[str] = []
    for path in sorted(callback_directory(root).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        token = record.get("token")
        if (
            record.get("schema_version") == CALLBACK_SCHEMA_VERSION
            and record.get("task_id") == rendered_task
            and record.get("round") == round_number
            and Path(str(record.get("repository_root", ""))).resolve() == root
            and isinstance(token, str)
            and TOKEN_PATTERN.fullmatch(token)
        ):
            matches.append(token)

    if not matches:
        raise RuntimeError(
            f"no callback capability found for task {rendered_task} round {round_number}"
        )

    open_matches: list[str] = []
    for token in matches:
        record = load_callback(root, token)
        if record.get("status") != "open":
            continue
        if utc_now() >= _parse_expiry(record):
            continue
        open_matches.append(token)
    if len(open_matches) == 1:
        # A retry may coexist with an earlier cancelled capability for the same
        # task/round. Only the unique live capability is eligible for dispatch.
        return open_matches[0]
    if len(open_matches) > 1:
        raise RuntimeError(
            f"ambiguous open callback capabilities for task {rendered_task} "
            f"round {round_number}: " + ", ".join(open_matches)
        )
    if len(matches) == 1:
        # Preserve one-shot replay diagnostics: resolving a sole sent capability
        # lets send_callback return the precise already-sent error.
        return matches[0]
    raise RuntimeError(
        f"ambiguous callback capabilities for task {rendered_task} round {round_number}: "
        + ", ".join(matches)
    )


def resolve_callback_token_for_status(
    repository: str | Path,
    *,
    task_id: str,
    round_number: int,
    statuses: set[str],
) -> str:
    """Resolve one exact callback state without guessing among duplicates."""
    root = resolve_repository(repository)
    task = _validate_task_id(task_id)
    if round_number < 1 or round_number > 500:
        raise ValueError("callback round must be between 1 and 500")

    matches: list[str] = []
    for path in sorted(callback_directory(root).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token = record.get("token") if isinstance(record, dict) else None
        if (
            isinstance(record, dict)
            and record.get("schema_version") == CALLBACK_SCHEMA_VERSION
            and record.get("task_id") == task
            and record.get("round") == round_number
            and record.get("status") in statuses
            and Path(str(record.get("repository_root", ""))).resolve() == root
            and isinstance(token, str)
            and TOKEN_PATTERN.fullmatch(token)
        ):
            matches.append(token)
    if len(matches) == 1:
        return matches[0]
    selector = ", ".join(sorted(statuses))
    if not matches:
        raise RuntimeError(
            f"no {selector} callback capability found for task {task} round {round_number}"
        )
    raise RuntimeError(
        f"ambiguous {selector} callback capabilities for task {task} round {round_number}: "
        + ", ".join(matches)
    )


def resolve_callback_token_for_relay(
    repository: str | Path,
    *,
    relay_id: str,
) -> str:
    """Resolve one relay by its non-secret, per-capability selector."""
    root = resolve_repository(repository)
    if not RELAY_ID_PATTERN.fullmatch(relay_id):
        raise ValueError("invalid callback relay ID")
    matches: list[str] = []
    for path in sorted(callback_directory(root).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token = record.get("token") if isinstance(record, dict) else None
        if (
            isinstance(record, dict)
            and record.get("schema_version") == CALLBACK_SCHEMA_VERSION
            and record.get("relay_id") == relay_id
            and Path(str(record.get("repository_root", ""))).resolve() == root
            and isinstance(token, str)
            and TOKEN_PATTERN.fullmatch(token)
        ):
            matches.append(token)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("no callback capability found for relay ID")
    raise RuntimeError("ambiguous callback relay ID")


def resolve_callback_token_for_delivery_id(
    repository: str | Path,
    *,
    delivery_id: str,
) -> str:
    """Resolve one callback receipt by its non-secret delivery correlation ID."""
    root = resolve_repository(repository)
    if not DELIVERY_ID_PATTERN.fullmatch(delivery_id):
        raise ValueError("invalid callback delivery ID")
    matches: list[str] = []
    for path in sorted(callback_directory(root).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token = record.get("token") if isinstance(record, dict) else None
        if (
            isinstance(record, dict)
            and record.get("schema_version") == CALLBACK_SCHEMA_VERSION
            and record.get("delivery_id") == delivery_id
            and Path(str(record.get("repository_root", ""))).resolve() == root
            and isinstance(token, str)
            and TOKEN_PATTERN.fullmatch(token)
        ):
            matches.append(token)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("no callback capability found for delivery ID")
    raise RuntimeError("ambiguous callback delivery ID")


def load_callback(repository: Path, token: str) -> dict[str, Any]:
    path = callback_path(repository, token)
    if not path.is_file():
        raise RuntimeError(f"callback capability not found: {token}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid callback capability: {token}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid callback capability: {token}")
    if value.get("schema_version") != CALLBACK_SCHEMA_VERSION:
        raise RuntimeError("unsupported callback schema version")
    if value.get("token") != token:
        raise RuntimeError("callback token/file mismatch")
    if Path(str(value.get("repository_root", ""))).resolve() != repository:
        raise RuntimeError("callback repository mismatch")
    return value


def _result_object(value: dict[str, Any]) -> dict[str, Any]:
    result = value.get("result", value)
    return result if isinstance(result, dict) else {}


def terminal_records(value: dict[str, Any], repository: Path) -> list[dict[str, Any]]:
    terminals = _result_object(value).get("terminals", [])
    if not isinstance(terminals, list):
        return []
    records: list[dict[str, Any]] = []
    for item in terminals:
        if not isinstance(item, dict):
            continue
        handle = item.get("handle")
        worktree = item.get("worktreePath") or item.get("worktree_path")
        if not isinstance(handle, str) or not handle:
            continue
        if not isinstance(worktree, str) or not worktree:
            continue
        try:
            exact_worktree = Path(worktree).expanduser().resolve() == repository
        except OSError:
            exact_worktree = False
        if not exact_worktree:
            continue
        if item.get("connected") is False or item.get("writable") is False:
            continue
        records.append(dict(item))
    return records


def _looks_like_opencode(record: dict[str, Any]) -> bool:
    material = "\n".join(
        str(record.get(key) or "") for key in ("title", "preview")
    ).casefold()
    return any(
        marker in material
        for marker in (
            "ctrl+p commands",
            "build ·",
            "opencode",
            "oc |",
        )
    )


def list_repository_terminals(repository: Path) -> list[dict[str, Any]]:
    payload = run_orca(
        ["terminal", "list", "--worktree", f"path:{repository}"],
        timeout=30,
    )
    return terminal_records(payload, repository)


def resolve_origin_terminal(
    repository: Path,
    explicit: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    requested = (
        explicit
        or os.getenv("SIN_GPT_WEB_ORIGIN_TERMINAL")
        or os.getenv("SIN_ORCA_PARENT_TERMINAL")
        or os.getenv("ORCA_TERMINAL_HANDLE")
    )
    records = list_repository_terminals(repository)
    by_handle = {str(item["handle"]): item for item in records}
    if requested:
        requested = requested.strip()
        if requested not in by_handle:
            raise RuntimeError(
                "origin terminal is not a connected writable terminal in the exact repository"
            )
        source = "explicit" if explicit else "environment"
        return requested, source, by_handle[requested]

    candidates = [item for item in records if _looks_like_opencode(item)]
    if not candidates:
        candidates = records
    if not candidates:
        raise RuntimeError(
            "no connected writable Orca terminal exists for the repository; "
            "pass --origin-terminal from the invoking OpenCode TUI"
        )
    if len(candidates) == 1:
        return (
            str(candidates[0]["handle"]),
            "unique-repository-terminal",
            candidates[0],
        )

    handles = ", ".join(str(item["handle"]) for item in candidates)
    raise RuntimeError(
        "origin terminal is ambiguous; pass --origin-terminal explicitly. "
        f"Candidates: {handles}"
    )


def _decode_json_array_with_preamble(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    raise RuntimeError("OpenCode session list did not contain a JSON array")


def _validate_session_id(value: str) -> str:
    rendered = value.strip()
    if not SESSION_PATTERN.fullmatch(rendered):
        raise ValueError(f"invalid OpenCode session ID: {value!r}")
    return rendered


def orca_state_candidates() -> list[Path]:
    explicit = os.getenv("SIN_ORCA_STATE_FILE") or os.getenv("ORCA_STATE_FILE")
    if explicit:
        return [Path(explicit).expanduser()]

    home = Path.home()
    roots = [
        home / "Library" / "Application Support" / "orca" / "profiles",
        home / ".config" / "orca" / "profiles",
        home / ".local" / "share" / "orca" / "profiles",
    ]
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.glob("*/orca-data.json"))
    return sorted(
        candidates,
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )


def _load_orca_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def resolve_session_from_orca_state(
    terminal: dict[str, Any],
) -> dict[str, Any] | None:
    tab_id = terminal.get("tabId") or terminal.get("tab_id")
    leaf_id = terminal.get("leafId") or terminal.get("leaf_id")
    pty_id = terminal.get("ptyId") or terminal.get("pty_id")
    worktree_id = terminal.get("worktreeId") or terminal.get("worktree_id")
    if not all(isinstance(value, str) and value for value in (tab_id, leaf_id, pty_id)):
        return None
    pane_key = f"{tab_id}:{leaf_id}"

    for state_path in orca_state_candidates():
        state = _load_orca_state(state_path)
        if state is None:
            continue
        workspace = state.get("workspaceSession")
        if not isinstance(workspace, dict):
            continue

        layouts = workspace.get("terminalLayoutsByTabId")
        if not isinstance(layouts, dict):
            continue
        layout = layouts.get(tab_id)
        if not isinstance(layout, dict):
            continue
        pty_by_leaf = layout.get("ptyIdsByLeafId")
        if not isinstance(pty_by_leaf, dict) or pty_by_leaf.get(leaf_id) != pty_id:
            continue

        sessions = workspace.get("sleepingAgentSessionsByPaneKey")
        if not isinstance(sessions, dict):
            continue
        agent_state = sessions.get(pane_key)
        if not isinstance(agent_state, dict):
            continue
        if agent_state.get("tabId") != tab_id:
            continue
        if worktree_id and agent_state.get("worktreeId") != worktree_id:
            continue
        if str(agent_state.get("agent") or "").casefold() != "opencode":
            continue
        provider = agent_state.get("providerSession")
        if not isinstance(provider, dict) or provider.get("key") != "session_id":
            continue
        session_id = provider.get("id")
        if not isinstance(session_id, str) or not SESSION_PATTERN.fullmatch(session_id):
            continue
        return {
            "id": session_id,
            "source": "orca-agent-session-state",
            "confidence": "exact",
            "tab_id": tab_id,
            "leaf_id": leaf_id,
            "pane_key": pane_key,
            "pty_id": pty_id,
            "state_profile": state_path.parent.name,
        }
    return None


def resolve_origin_session(
    repository: Path,
    terminal: dict[str, Any],
    explicit: str | None = None,
) -> dict[str, Any]:
    if explicit:
        return {
            "id": _validate_session_id(explicit),
            "source": "explicit",
            "confidence": "exact",
        }
    environment = os.getenv("OPENCODE_SESSION_ID")
    if environment:
        return {
            "id": _validate_session_id(environment),
            "source": "environment",
            "confidence": "exact",
        }

    state_session = resolve_session_from_orca_state(terminal)
    if state_session is not None:
        return state_session

    binary = shutil.which("opencode")
    if binary is None:
        return {"id": None, "source": "unavailable", "confidence": "none"}
    process = subprocess.run(
        [binary, "session", "list", "--format", "json"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if process.returncode != 0:
        return {
            "id": None,
            "source": "opencode-session-list-failed",
            "confidence": "none",
        }
    try:
        sessions = _decode_json_array_with_preamble(process.stdout)
    except RuntimeError:
        return {
            "id": None,
            "source": "opencode-session-list-invalid",
            "confidence": "none",
        }
    candidates = [
        item
        for item in sessions
        if isinstance(item, dict)
        and item.get("directory") == str(repository)
        and isinstance(item.get("id"), str)
        and SESSION_PATTERN.fullmatch(str(item["id"]))
    ]
    if not candidates:
        return {
            "id": None,
            "source": "no-repository-session",
            "confidence": "none",
        }
    if len(candidates) != 1:
        return {
            "id": None,
            "source": "ambiguous-repository-sessions",
            "confidence": "none",
            "candidate_count": len(candidates),
        }
    selected = candidates[0]
    return {
        "id": str(selected["id"]),
        "title": str(selected.get("title") or ""),
        "updated": selected.get("updated"),
        "source": "opencode-session-list:unique-repository-session",
        "confidence": "unambiguous",
        "candidate_count": 1,
    }


def _validate_task_id(task_id: str) -> str:
    rendered = task_id.strip()
    if not TASK_PATTERN.fullmatch(rendered):
        raise ValueError(
            "task ID must be 1-128 characters using letters, numbers, dot, colon, underscore or hyphen"
        )
    return rendered


def _command_template(
    repository: Path,
    *,
    task_id: str,
    round_number: int,
) -> str:
    return shlex.join(
        [
            "sin-orca",
            "web-callback-send",
            "--repo",
            str(repository),
            "--task-id",
            task_id,
            "--round",
            str(round_number),
            "--status",
            "done",
            "--summary",
            "<short factual completion summary>",
            "--changed",
            "<comma-separated changed files or none>",
            "--verify",
            "<tests and verification status>",
        ]
    )


def open_callback(
    *,
    repository: str | Path,
    task_id: str,
    origin_terminal: str | None = None,
    origin_session_id: str | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    round_number: int = 1,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> dict[str, Any]:
    root = resolve_repository(repository)
    task = _validate_task_id(task_id)
    if not 5 <= ttl_minutes <= 7 * 24 * 60:
        raise ValueError("callback TTL must be between 5 and 10080 minutes")
    if not 1 <= max_rounds <= 500:
        raise ValueError("max rounds must be between 1 and 500")
    if not 1 <= round_number <= max_rounds:
        raise ValueError("round must be between 1 and max rounds")

    terminal, terminal_source, terminal_record = resolve_origin_terminal(
        root,
        origin_terminal,
    )
    session = resolve_origin_session(root, terminal_record, origin_session_id)
    now = utc_now()
    token = f"gptwcb_{uuid.uuid4().hex}"
    record: dict[str, Any] = {
        "schema_version": CALLBACK_SCHEMA_VERSION,
        "token": token,
        "relay_id": f"gptwcr_{uuid.uuid4().hex}",
        "status": "open",
        "task_id": task,
        "repository_root": str(root),
        "created_at": isoformat(now),
        "expires_at": isoformat(now + timedelta(minutes=ttl_minutes)),
        "ttl_minutes": ttl_minutes,
        "origin_terminal": terminal,
        "origin_terminal_source": terminal_source,
        "origin_session": session,
        "round": round_number,
        "max_rounds": max_rounds,
        "conversation": None,
    }
    path = callback_path(root, token)
    atomic_write_json(path, record)
    return {
        "ok": True,
        "status": "callback-open",
        "callback": token,
        "task_id": task,
        "repository": str(root),
        "origin_terminal": terminal,
        "origin_terminal_source": terminal_source,
        "origin_session": session,
        "expires_at": record["expires_at"],
        "round": round_number,
        "max_rounds": max_rounds,
        "record": str(path),
        "callback_command_template": _command_template(
            root,
            task_id=task,
            round_number=round_number,
        ),
    }


def _valid_chatgpt_url(value: str) -> str:
    rendered = value.strip()
    parsed = urlparse(rendered)
    parts = [part for part in parsed.path.split("/") if part]
    conversation_id = parts[1] if len(parts) >= 2 and parts[0] == "c" else ""
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}
        or not conversation_id
        or conversation_id.casefold().startswith("web:")
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", conversation_id)
    ):
        raise ValueError(
            "conversation URL must be a canonical https://chatgpt.com/c/<id> URL; "
            "synthetic WEB aliases are not accepted"
        )
    return rendered


def bind_callback(
    *,
    repository: str | Path,
    token: str,
    page_id: str,
    conversation_url: str,
    title: str | None = None,
    profile: str = "OpenSIN",
    chatgpt_project: str | None = None,
) -> dict[str, Any]:
    root = resolve_repository(repository)
    rendered_page = page_id.strip()
    if (
        not rendered_page
        or len(rendered_page) > 256
        or any(c.isspace() for c in rendered_page)
    ):
        raise ValueError("invalid browser page ID")
    rendered_url = _valid_chatgpt_url(conversation_url)
    with callback_lock(root, token):
        record = load_callback(root, token)
        if record.get("status") != "open":
            raise RuntimeError("only an open callback can be bound to a conversation")
        record["conversation"] = {
            "page_id": rendered_page,
            "url": rendered_url,
            "title": redact_text(title or "").strip()[:300],
            "profile": redact_text(profile).strip()[:100] or "OpenSIN",
            "chatgpt_project": redact_text(chatgpt_project or "").strip()[:300] or None,
            "bound_at": isoformat(utc_now()),
        }
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-bound",
        "callback": token,
        "conversation": record["conversation"],
    }


def _parse_expiry(record: dict[str, Any]) -> datetime:
    try:
        value = datetime.fromisoformat(str(record["expires_at"]))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("callback has invalid expiry") from error
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def callback_status(*, repository: str | Path, token: str) -> dict[str, Any]:
    root = resolve_repository(repository)
    record = load_callback(root, token)
    expired = utc_now() >= _parse_expiry(record)
    return {
        "ok": True,
        "status": record.get("status"),
        "expired": expired,
        "callback": token,
        "task_id": record.get("task_id"),
        "origin_terminal": record.get("origin_terminal"),
        "origin_session": record.get("origin_session"),
        "round": record.get("round"),
        "max_rounds": record.get("max_rounds"),
        "conversation": record.get("conversation"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "dispatch_started_at": record.get("dispatch_started_at"),
        "sent_at": record.get("sent_at"),
        "delivery_failed_at": record.get("delivery_failed_at"),
        "delivery_error": record.get("delivery_error"),
        "callback_status": record.get("callback_status"),
        "receipt_at": record.get("receipt_at"),
        "delivery_id": record.get("delivery_id"),
        "relay_fallback": record.get("relay_fallback"),
    }


def _compact(value: str, limit: int) -> str:
    cleaned = " ".join(redact_text(value).split())
    return cleaned[:limit]


def _changed_files(values: list[str] | None) -> list[str]:
    changed: list[str] = []
    for raw in values or []:
        for item in raw.split(","):
            rendered = _compact(item, 500)
            if not rendered or rendered.casefold() in {"none", "null", "-"}:
                continue
            changed.append(rendered)
    return list(dict.fromkeys(changed))[:100]


def _default_next_action(record: dict[str, Any]) -> str:
    round_number = int(record.get("round") or 1)
    max_rounds = int(record.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    conversation = record.get("conversation")
    binding = ""
    if isinstance(conversation, dict):
        page_id = conversation.get("page_id")
        url = conversation.get("url")
        if page_id or url:
            binding = (
                " Continue the same ChatGPT Web conversation using its saved "
                f"page/URL binding ({page_id or 'no-page-id'}, {url or 'no-url'})."
            )
    if round_number >= max_rounds:
        return (
            "Refresh .sin-gpt-web/taskplan.sqlite3 and TASKPLAN.md through "
            "sin-gpt-web-state, independently verify the evidence, and either "
            "complete the goal or record a genuine blocker. The configured loop "
            "round budget is exhausted; do not auto-delegate another round."
        )
    return (
        "Refresh .sin-gpt-web/taskplan.sqlite3 and TASKPLAN.md through "
        "sin-gpt-web-state, independently verify the evidence, then continue the "
        "CEO loop with the next highest-priority bounded task. Re-delegate to "
        "ChatGPT Web without waiting for human input unless a genuine external "
        "authority blocker exists. Stop only when the definition of done and all "
        "acceptance gates pass." + binding
    )


def _terminal_is_still_bound(repository: Path, handle: str) -> bool:
    return any(
        item.get("handle") == handle for item in list_repository_terminals(repository)
    )


def relay_launch_agent_directory() -> Path:
    configured = os.getenv("SIN_ORCA_LAUNCH_AGENTS_DIR")
    directory = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Library" / "LaunchAgents"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def relay_launch_agent_label(repository: Path, record: dict[str, Any]) -> str:
    relay_id = str(record.get("relay_id") or "")
    if not RELAY_ID_PATTERN.fullmatch(relay_id):
        raise RuntimeError("callback has invalid relay ID")
    material = "\0".join((str(repository), relay_id))
    return (
        "com.sin-orca.web-callback."
        + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    )


def _relay_binary() -> str:
    binary = shutil.which("sin-orca")
    if binary is None:
        raise RuntimeError("sin-orca is unavailable for callback relay scheduling")
    return binary


def _launchctl_process(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def _run_launchctl(arguments: list[str], *, tolerate_failure: bool = False) -> None:
    binary = shutil.which("launchctl")
    if binary is None:
        if tolerate_failure:
            return
        raise RuntimeError("launchctl is unavailable for callback relay scheduling")
    process = _launchctl_process([binary, *arguments])
    if process.returncode and not tolerate_failure:
        detail = (process.stderr or process.stdout).strip()
        raise RuntimeError(detail or "launchctl callback relay operation failed")


def _relay_fallback_active(record: dict[str, Any]) -> bool:
    fallback = record.get("relay_fallback")
    return isinstance(fallback, dict) and fallback.get("status") == "installed"


def _remove_relay_fallback(
    repository: Path,
    record: dict[str, Any],
    *,
    reason: str,
) -> bool:
    fallback = record.get("relay_fallback")
    if not isinstance(fallback, dict) or fallback.get("status") == "removed":
        return False
    if fallback.get("status") == "installed":
        _deactivate_relay_fallback(repository, record, reason=reason)
        fallback = record["relay_fallback"]
    record["relay_fallback"] = {
        **fallback,
        "status": "removed",
        "removed_at": isoformat(utc_now()),
        "remove_reason": reason,
    }
    return True


def _expire_callback_if_needed(
    repository: Path,
    token: str,
    record: dict[str, Any],
) -> bool:
    """Atomically make an unacknowledged callback terminal after its TTL.

    Callers hold ``callback_lock`` so no delivery or receipt can race this
    transition. The relay remains scheduled through sent/indeterminate states
    specifically so its next invocation can enforce this expiry.
    """
    if record.get("status") not in EXPIRABLE_CALLBACK_STATUSES:
        return False
    now = utc_now()
    if now < _parse_expiry(record):
        return False
    record.update({"status": "expired", "expired_at": isoformat(now)})
    _remove_relay_fallback(repository, record, reason="callback-expired")
    atomic_write_json(callback_path(repository, token), record)
    return True


def _deactivate_relay_fallback(
    repository: Path,
    record: dict[str, Any],
    *,
    reason: str,
) -> bool:
    """Stop scheduled delivery while retaining recovery and receipt correlation."""
    fallback = record.get("relay_fallback")
    if not isinstance(fallback, dict) or fallback.get("status") != "installed":
        return False
    label = str(fallback.get("label") or "")
    plist_path = relay_launch_agent_directory() / f"{label}.plist"
    _run_launchctl(["bootout", f"gui/{os.getuid()}/{label}"], tolerate_failure=True)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    record["relay_fallback"] = {
        **fallback,
        "status": "inert",
        "deactivated_at": isoformat(utc_now()),
        "deactivate_reason": reason,
    }
    return True


def install_callback_relay(
    *,
    repository: str | Path,
    token: str,
    interval_seconds: int = DEFAULT_RELAY_INTERVAL_SECONDS,
    max_attempts: int = DEFAULT_RELAY_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Install an optional bounded relay without exposing callback data to launchd."""
    root = resolve_repository(repository)
    if not 30 <= interval_seconds <= 3600:
        raise ValueError("relay interval must be between 30 and 3600 seconds")
    if not 1 <= max_attempts <= 20:
        raise ValueError("relay max attempts must be between 1 and 20")
    with callback_lock(root, token):
        record = load_callback(root, token)
        if record.get("status") != "pending-delivery":
            raise RuntimeError("only a pending-delivery callback can install a relay")
        if _relay_fallback_active(record):
            return {
                "ok": True,
                "status": "callback-relay-installed",
                "reused": True,
                "task_id": record.get("task_id"),
                "round": record.get("round"),
            }

        label = relay_launch_agent_label(root, record)
        plist_path = relay_launch_agent_directory() / f"{label}.plist"
        plist = {
            "Label": label,
            "ProgramArguments": [
                _relay_binary(),
                "web-callback-relay",
                "--repo",
                str(root),
                "--relay-id",
                str(record["relay_id"]),
                "--scheduled",
            ],
            "WorkingDirectory": str(root),
            "StartInterval": interval_seconds,
            "ProcessType": "Background",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }
        temporary = plist_path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            plistlib.dump(plist, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(plist_path)
        record["relay_fallback"] = {
            "status": "installed",
            "label": label,
            "relay_id": record["relay_id"],
            "interval_seconds": interval_seconds,
            "max_attempts": max_attempts,
            "attempts": 0,
            "installed_at": isoformat(utc_now()),
        }
        atomic_write_json(callback_path(root, token), record)
        try:
            _run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])
        except Exception:
            record["relay_fallback"] = {
                **record["relay_fallback"],
                "status": "install-failed",
                "failed_at": isoformat(utc_now()),
            }
            atomic_write_json(callback_path(root, token), record)
            raise
    return {
        "ok": True,
        "status": "callback-relay-installed",
        "reused": False,
        "task_id": record.get("task_id"),
        "round": record.get("round"),
    }


def cancel_callback_relay(
    *,
    repository: str | Path,
    token: str,
    reason: str = "manual-cancel",
) -> dict[str, Any]:
    root = resolve_repository(repository)
    with callback_lock(root, token):
        record = load_callback(root, token)
        removed = _remove_relay_fallback(root, record, reason=_compact(reason, 200))
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-relay-cancelled",
        "reused": not removed,
        "task_id": record.get("task_id"),
        "round": record.get("round"),
    }


def resolve_delivery_terminal(
    repository: Path,
    record: dict[str, Any],
) -> tuple[str | None, str]:
    """Resolve a live delivery target without guessing between OpenCode sessions."""
    terminals = list_repository_terminals(repository)
    origin = str(record.get("origin_terminal") or "")
    if any(item.get("handle") == origin for item in terminals):
        return origin, "origin-terminal"

    session = record.get("origin_session")
    expected_session = session.get("id") if isinstance(session, dict) else None
    if not isinstance(expected_session, str) or not SESSION_PATTERN.fullmatch(
        expected_session
    ):
        return None, "origin-terminal-gone-and-session-unresolved"

    matches = [
        item
        for item in terminals
        if _looks_like_opencode(item)
        and (resolve_session_from_orca_state(item) or {}).get("id") == expected_session
    ]
    if len(matches) == 1:
        return str(matches[0]["handle"]), "rebound-origin-session"
    if len(matches) > 1:
        return None, "origin-terminal-gone-and-session-ambiguous"
    return None, "origin-terminal-gone-and-session-offline"


def render_callback_message(
    record: dict[str, Any],
    *,
    final_status: str,
    summary: str,
    changed: list[str],
    verification: str,
    next_action: str,
) -> str:
    session = record.get("origin_session")
    session_id = session.get("id") if isinstance(session, dict) else None
    conversation = record.get("conversation")
    conversation_url = (
        conversation.get("url") if isinstance(conversation, dict) else None
    )
    page_id = conversation.get("page_id") if isinstance(conversation, dict) else None
    header = (
        "SIN_GPT_WEB_CALLBACK "
        f"task={record['task_id']} status={final_status} "
        f"delivery={record['delivery_id']} "
        f"session={session_id or 'unresolved'} "
        f"round={record.get('round', 1)}/{record.get('max_rounds', DEFAULT_MAX_ROUNDS)}"
    )
    receipt_command = shlex.join(
        [
            "sin-orca",
            "web-callback-ack",
            "--repo",
            str(record["repository_root"]),
            "--delivery-id",
            str(record["delivery_id"]),
        ]
    )
    return "\n".join(
        [
            header,
            f"SUMMARY: {summary}",
            f"CHANGED: {json.dumps(changed or ['none'], ensure_ascii=False)}",
            f"VERIFY: {verification or 'unknown'}",
            f"CHATGPT_PAGE_ID: {page_id or 'unresolved'}",
            f"CHATGPT_CONVERSATION_URL: {conversation_url or 'unresolved'}",
            f"REQUIRED_ACTION: {next_action}",
            f"RECEIPT_ACTION: {receipt_command}",
            "Process this delivery ID at most once and send its receipt only after processing.",
            "Treat this callback as a wake-up event, not as proof of completion. "
            "Inspect repository state, taskplan evidence, diff, and tests before accepting it.",
        ]
    )


def _deliver_pending_callback(
    repository: Path,
    token: str,
    record: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not DELIVERY_ID_PATTERN.fullmatch(str(record.get("delivery_id") or "")):
        # Existing queued callbacks predate receipt correlation. Persist their
        # delivery identity before any transport attempt so recovery remains safe.
        record["delivery_id"] = f"gptwcd_{uuid.uuid4().hex}"
        record["delivery_state"] = "pending"
        message = render_callback_message(
            record,
            final_status=str(record["callback_status"]),
            summary=str(record["summary"]),
            changed=list(record.get("changed_files") or []),
            verification=str(record.get("verification") or "unknown"),
            next_action=str(record.get("next_action") or ""),
        )
        record["message_sha256"] = hashlib.sha256(message.encode("utf-8")).hexdigest()
        atomic_write_json(callback_path(repository, token), record)
    if record.get("delivery_state") == "indeterminate":
        return {
            "ok": True,
            "status": "callback-delivery-indeterminate",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_id": record.get("delivery_id"),
        }
    terminal, target_source = resolve_delivery_terminal(repository, record)
    record["delivery_attempts"] = int(record.get("delivery_attempts") or 0) + 1
    record["last_delivery_attempt_at"] = isoformat(utc_now())
    if not terminal:
        record.update(
            {
                "status": "pending-delivery",
                "delivery_reason": target_source,
            }
        )
        atomic_write_json(callback_path(repository, token), record)
        return {
            "ok": True,
            "status": "callback-pending",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_reason": target_source,
        }

    message = render_callback_message(
        record,
        final_status=str(record["callback_status"]),
        summary=str(record["summary"]),
        changed=list(record.get("changed_files") or []),
        verification=str(record.get("verification") or "unknown"),
        next_action=str(record.get("next_action") or ""),
    )
    if dry_run:
        return {
            "ok": True,
            "status": "callback-dry-run",
            "callback": token,
            "origin_terminal": terminal,
            "target_source": target_source,
            "message": message,
        }

    try:
        run_orca(
            [
                "terminal",
                "send",
                "--terminal",
                terminal,
                "--text",
                message,
                "--enter",
            ],
            timeout=30,
        )
    except Exception as error:
        record.update(
            {
                # Orca may have accepted the input before the transport error.
                # Never retry that delivery ID blindly; the TUI receipt resolves it.
                "status": "delivery-indeterminate",
                "delivery_state": "indeterminate",
                "delivery_reason": "terminal-send-indeterminate",
                "delivery_error": _compact(str(error), 700) or type(error).__name__,
            }
        )
        atomic_write_json(callback_path(repository, token), record)
        return {
            "ok": True,
            "status": "callback-delivery-indeterminate",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_id": record.get("delivery_id"),
            "delivery_reason": "terminal-send-indeterminate",
        }

    record.update(
        {
            "status": "sent",
            "delivery_state": "sent",
            "delivery_terminal": terminal,
            "delivery_target_source": target_source,
            "sent_at": isoformat(utc_now()),
        }
    )
    atomic_write_json(callback_path(repository, token), record)
    return {
        "ok": True,
        "status": "callback-sent",
        "callback": token,
        "task_id": record.get("task_id"),
        "callback_status": record.get("callback_status"),
        "origin_terminal": terminal,
        "origin_session": record.get("origin_session"),
        "round": record.get("round"),
        "max_rounds": record.get("max_rounds"),
        "conversation": record.get("conversation"),
        "sent_at": record.get("sent_at"),
    }


def send_callback(
    *,
    repository: str | Path,
    token: str,
    final_status: str,
    summary: str,
    changed: list[str] | None = None,
    verification: str = "unknown",
    next_action: str | None = None,
    dry_run: bool = False,
    relay_fallback: bool = False,
    relay_interval_seconds: int = DEFAULT_RELAY_INTERVAL_SECONDS,
    relay_max_attempts: int = DEFAULT_RELAY_MAX_ATTEMPTS,
) -> dict[str, Any]:
    root = resolve_repository(repository)
    if final_status not in FINAL_STATUSES:
        raise ValueError(
            "callback status must be one of: " + ", ".join(sorted(FINAL_STATUSES))
        )
    rendered_summary = _compact(summary, 700)
    if not rendered_summary:
        raise ValueError("callback summary must not be empty")
    rendered_changed = _changed_files(changed)
    rendered_verification = _compact(verification, 500) or "unknown"

    with callback_lock(root, token):
        record = load_callback(root, token)
        if _expire_callback_if_needed(root, token, record):
            raise RuntimeError("callback capability has expired")
        if record.get("status") == "open":
            action = _compact(next_action or _default_next_action(record), 1800)
            delivery_id = f"gptwcd_{uuid.uuid4().hex}"
            message = render_callback_message(
                {**record, "delivery_id": delivery_id},
                final_status=final_status,
                summary=rendered_summary,
                changed=rendered_changed,
                verification=rendered_verification,
                next_action=action,
            )
            if dry_run:
                terminal, target_source = resolve_delivery_terminal(root, record)
                return {
                    "ok": True,
                    "status": "callback-dry-run",
                    "callback": token,
                    "origin_terminal": terminal,
                    "target_source": target_source,
                    "message": message,
                }
            record.update(
                {
                    # Persist the outcome before terminal lookup so a terminal restart
                    # cannot discard the completion event.
                    "status": "pending-delivery",
                    "callback_status": final_status,
                    "dispatch_started_at": isoformat(utc_now()),
                    "summary": rendered_summary,
                    "changed_files": rendered_changed,
                    "verification": rendered_verification,
                    "next_action": action,
                    "delivery_id": delivery_id,
                    "delivery_state": "pending",
                    "message_sha256": hashlib.sha256(
                        message.encode("utf-8")
                    ).hexdigest(),
                }
            )
            atomic_write_json(callback_path(root, token), record)
        elif record.get("status") == "delivery-indeterminate":
            return _deliver_pending_callback(root, token, record, dry_run=dry_run)
        elif record.get("status") != "pending-delivery":
            raise RuntimeError(
                f"callback is already {record.get('status')}; capabilities are one-shot"
            )
        result = _deliver_pending_callback(root, token, record, dry_run=dry_run)
    if relay_fallback and not dry_run and result.get("status") == "callback-pending":
        result["relay_fallback"] = install_callback_relay(
            repository=root,
            token=token,
            interval_seconds=relay_interval_seconds,
            max_attempts=relay_max_attempts,
        )
    return result


def relay_callback(
    *,
    repository: str | Path,
    token: str,
    dry_run: bool = False,
    scheduled: bool = False,
) -> dict[str, Any]:
    """Retry a persisted callback-inbox item after a terminal/session restart."""
    root = resolve_repository(repository)
    with callback_lock(root, token):
        record = load_callback(root, token)
        if _expire_callback_if_needed(root, token, record):
            return {"ok": True, "status": "callback-expired", "callback": token}
        if record.get("status") in {"sent", "delivery-indeterminate"}:
            # Keep the scheduler installed but inert so it can make the TTL
            # transition if no receipt ever arrives.
            return {
                "ok": True,
                "status": "callback-awaiting-receipt",
                "callback": token,
                "delivery_id": record.get("delivery_id"),
            }
        if record.get("status") != "pending-delivery":
            raise RuntimeError("only a pending-delivery callback can be relayed")
        if scheduled and not _relay_fallback_active(record):
            return {"ok": True, "status": "callback-relay-inactive", "callback": token}
        result = _deliver_pending_callback(root, token, record, dry_run=dry_run)
        if scheduled and not dry_run:
            fallback = record["relay_fallback"]
            fallback["attempts"] = int(fallback.get("attempts") or 0) + 1
            if record.get("status") == "pending-delivery" and fallback[
                "attempts"
            ] >= int(fallback["max_attempts"]):
                _deactivate_relay_fallback(
                    root,
                    record,
                    reason="retry-budget-exhausted",
                )
            atomic_write_json(callback_path(root, token), record)
        return result


def acknowledge_callback(
    *,
    repository: str | Path,
    token: str,
    delivery_id: str,
) -> dict[str, Any]:
    """Record one TUI receipt and remove any optional relay without polling."""
    root = resolve_repository(repository)
    with callback_lock(root, token):
        record = load_callback(root, token)
        if record.get(
            "delivery_id"
        ) != delivery_id or not DELIVERY_ID_PATTERN.fullmatch(delivery_id):
            raise RuntimeError("callback receipt delivery ID does not match")
        if record.get("status") == "acknowledged":
            return {
                "ok": True,
                "status": "callback-acknowledged",
                "reused": True,
                "task_id": record.get("task_id"),
                "round": record.get("round"),
            }
        if _expire_callback_if_needed(root, token, record):
            raise RuntimeError("callback capability has expired")
        if record.get("status") not in {"sent", "delivery-indeterminate"}:
            raise RuntimeError("only a delivered callback can be acknowledged")
        record.update(
            {
                "status": "acknowledged",
                "receipt_at": isoformat(utc_now()),
            }
        )
        _remove_relay_fallback(root, record, reason="receipt-acknowledged")
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-acknowledged",
        "reused": False,
        "task_id": record.get("task_id"),
        "round": record.get("round"),
    }


def cancel_callback(
    *,
    repository: str | Path,
    token: str,
    reason: str,
) -> dict[str, Any]:
    root = resolve_repository(repository)
    rendered_reason = _compact(reason, 700)
    if not rendered_reason:
        raise ValueError("cancellation reason must not be empty")
    with callback_lock(root, token):
        record = load_callback(root, token)
        status = record.get("status")
        if status == "cancelled":
            return {
                "ok": True,
                "status": "callback-cancelled",
                "callback": token,
                "reused": True,
            }
        if status != "open":
            raise RuntimeError(
                f"a {status} callback cannot be cancelled; capabilities are one-shot"
            )
        record.update(
            {
                "status": "cancelled",
                "cancelled_at": isoformat(utc_now()),
                "cancel_reason": rendered_reason,
            }
        )
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-cancelled",
        "callback": token,
        "reused": False,
    }
