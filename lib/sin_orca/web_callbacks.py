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
SESSION_PATTERN = re.compile(r"^ses_[A-Za-z0-9]+$")
TASK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FINAL_STATUSES = {"done", "blocked", "failed"}
DEFAULT_TTL_MINUTES = 24 * 60
DEFAULT_MAX_ROUNDS = 50


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
        str(record.get(key) or "")
        for key in ("title", "preview")
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


def _command_template(repository: Path, token: str) -> str:
    return shlex.join([
        "sin-orca",
        "web-callback-send",
        "--repo",
        str(repository),
        "--callback",
        token,
        "--status",
        "done",
        "--summary",
        "<short factual completion summary>",
        "--changed",
        "<comma-separated changed files or none>",
        "--verify",
        "<tests and verification status>",
    ])


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
        "callback_command_template": _command_template(root, token),
    }


def _valid_chatgpt_url(value: str) -> str:
    rendered = value.strip()
    parsed = urlparse(rendered)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}
        or "/c/" not in parsed.path
    ):
        raise ValueError(
            "conversation URL must be an https://chatgpt.com conversation URL containing /c/"
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
    if not rendered_page or len(rendered_page) > 256 or any(c.isspace() for c in rendered_page):
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
        "acceptance gates pass."
        + binding
    )


def _terminal_is_still_bound(repository: Path, handle: str) -> bool:
    return any(
        item.get("handle") == handle
        for item in list_repository_terminals(repository)
    )


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
    page_id = (
        conversation.get("page_id") if isinstance(conversation, dict) else None
    )
    header = (
        "SIN_GPT_WEB_CALLBACK "
        f"task={record['task_id']} status={final_status} token={record['token']} "
        f"session={session_id or 'unresolved'} "
        f"round={record.get('round', 1)}/{record.get('max_rounds', DEFAULT_MAX_ROUNDS)}"
    )
    return "\n".join([
        header,
        f"SUMMARY: {summary}",
        f"CHANGED: {json.dumps(changed or ['none'], ensure_ascii=False)}",
        f"VERIFY: {verification or 'unknown'}",
        f"CHATGPT_PAGE_ID: {page_id or 'unresolved'}",
        f"CHATGPT_CONVERSATION_URL: {conversation_url or 'unresolved'}",
        f"REQUIRED_ACTION: {next_action}",
        "Treat this callback as a wake-up event, not as proof of completion. "
        "Inspect repository state, taskplan evidence, diff, and tests before accepting it.",
    ])


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
        if record.get("status") != "open":
            raise RuntimeError(
                f"callback is already {record.get('status')}; capabilities are one-shot"
            )
        if utc_now() >= _parse_expiry(record):
            record["status"] = "expired"
            record["expired_at"] = isoformat(utc_now())
            atomic_write_json(callback_path(root, token), record)
            raise RuntimeError("callback capability has expired")
        terminal = str(record.get("origin_terminal") or "")
        if not terminal or not _terminal_is_still_bound(root, terminal):
            raise RuntimeError(
                "origin terminal is no longer connected and writable in the exact repository"
            )
        action = _compact(next_action or _default_next_action(record), 1800)
        message = render_callback_message(
            record,
            final_status=final_status,
            summary=rendered_summary,
            changed=rendered_changed,
            verification=rendered_verification,
            next_action=action,
        )
        if dry_run:
            return {
                "ok": True,
                "status": "callback-dry-run",
                "callback": token,
                "origin_terminal": terminal,
                "message": message,
            }

        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        record.update({
            "status": "dispatching",
            "callback_status": final_status,
            "dispatch_started_at": isoformat(utc_now()),
            "summary": rendered_summary,
            "changed_files": rendered_changed,
            "verification": rendered_verification,
            "next_action": action,
            "message_sha256": message_sha256,
        })
        atomic_write_json(callback_path(root, token), record)
        try:
            run_orca([
                "terminal",
                "send",
                "--terminal",
                terminal,
                "--text",
                message,
                "--enter",
            ], timeout=30)
        except Exception as error:
            record.update({
                "status": "delivery-failed",
                "delivery_failed_at": isoformat(utc_now()),
                "delivery_error": _compact(str(error), 700) or type(error).__name__,
            })
            atomic_write_json(callback_path(root, token), record)
            raise
        record.update({
            "status": "sent",
            "sent_at": isoformat(utc_now()),
        })
        atomic_write_json(callback_path(root, token), record)

    return {
        "ok": True,
        "status": "callback-sent",
        "callback": token,
        "task_id": record.get("task_id"),
        "callback_status": final_status,
        "origin_terminal": record.get("origin_terminal"),
        "origin_session": record.get("origin_session"),
        "round": record.get("round"),
        "max_rounds": record.get("max_rounds"),
        "conversation": record.get("conversation"),
        "sent_at": record.get("sent_at"),
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
        record.update({
            "status": "cancelled",
            "cancelled_at": isoformat(utc_now()),
            "cancel_reason": rendered_reason,
        })
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-cancelled",
        "callback": token,
        "reused": False,
    }
