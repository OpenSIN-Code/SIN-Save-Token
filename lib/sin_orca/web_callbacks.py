"""Event-driven ChatGPT Web callbacks into an originating OpenCode terminal.

The terminal handle is the transport identity.  The OpenCode session ID is kept
as correlation metadata because a repository may have multiple sessions and a
TUI normally listens on a random local server port.  Callback capabilities are
short-lived, one-shot, repository-local files under ``.sin-gpt-web``.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib import error as urllib_error
from urllib import request as urllib_request

from .dispatch import run_git, run_orca
from .state import atomic_write_json
from .verification import redact_text

CALLBACK_SCHEMA_VERSION = 1
TOKEN_PATTERN = re.compile(r"^gptwcb_[0-9a-f]{32}$")
RELAY_ID_PATTERN = re.compile(r"^gptwcr_[0-9a-f]{32}$")
DELIVERY_ID_PATTERN = re.compile(r"^gptwcd_[0-9a-f]{32}$")
SESSION_PATTERN = re.compile(r"^ses_[A-Za-z0-9]+$")
DSH_SESSION_PATTERN = re.compile(r"^session-[0-9a-fA-F-]{36}$")
TASK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FINAL_STATUSES = {"done", "blocked", "failed"}
DEFAULT_TTL_MINUTES = 24 * 60
DEFAULT_MAX_ROUNDS = 50
DEFAULT_RELAY_INTERVAL_SECONDS = 60
DEFAULT_RELAY_MAX_ATTEMPTS = 3
DEFAULT_TUI_IDLE_TIMEOUT_SECONDS = 30
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


def terminal_records(
    value: dict[str, Any],
    repository: Path,
    *,
    allow_busy: bool = False,
    allow_disconnected: bool = False,
) -> list[dict[str, Any]]:
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
        if item.get("connected") is False and not allow_disconnected:
            continue
        if not allow_busy and item.get("writable") is False:
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


def list_repository_terminals(
    repository: Path,
    *,
    allow_busy: bool = False,
    allow_disconnected: bool = False,
) -> list[dict[str, Any]]:
    payload = run_orca(
        ["terminal", "list", "--worktree", f"path:{repository}"],
        timeout=30,
    )
    return terminal_records(
        payload,
        repository,
        allow_busy=allow_busy,
        allow_disconnected=allow_disconnected,
    )


def resolve_origin_terminal(
    repository: Path,
    explicit: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    requested = (
        explicit
        or os.getenv("SIN_GPT_WEB_ORIGIN_TERMINAL")
        or os.getenv("SIN_NEVER_END_ORIGIN_TERMINAL")
        or os.getenv("SIN_ORCA_PARENT_TERMINAL")
        or os.getenv("ORCA_TERMINAL_HANDLE")
    )
    records = list_repository_terminals(
        repository,
        allow_busy=bool(requested),
        allow_disconnected=bool(requested),
    )
    by_handle = {str(item["handle"]): item for item in records}
    if requested:
        requested = requested.strip()
        if requested not in by_handle:
            if (
                explicit
                or os.getenv("SIN_GPT_WEB_ORIGIN_TERMINAL")
                or os.getenv("SIN_NEVER_END_ORIGIN_TERMINAL")
            ):
                return (
                    requested,
                    "explicit-unobserved",
                    {
                        "handle": requested,
                        "worktreePath": str(repository),
                        "connected": False,
                        "writable": False,
                    },
                )
            raise RuntimeError("origin terminal is not known for the exact repository")
        source = "explicit" if explicit else "environment"
        if by_handle[requested].get("connected") is False:
            source = f"{source}-disconnected"
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
    environment = os.getenv("OPENCODE_SESSION_ID") or os.getenv(
        "SIN_NEVER_END_ORIGIN_SESSION"
    )
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


def resolve_prime_agent_session(active_session_id: str) -> dict[str, Any]:
    """Resolve one live Prime Agent daemon session through its public CLI."""
    rendered = active_session_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", rendered):
        raise ValueError("invalid Prime Agent active session ID")
    binary = shutil.which("prime-agent")
    if binary is None:
        raise RuntimeError("prime-agent CLI is unavailable")
    try:
        process = subprocess.run(
            [binary, "list", "--json"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Prime Agent session inventory is unavailable") from error
    if process.returncode:
        raise RuntimeError("Prime Agent session inventory failed")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Prime Agent session inventory was not JSON") from error
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    matches = [
        item
        for item in sessions or []
        if isinstance(item, dict)
        and item.get("activeSessionId") == rendered
        and item.get("lifecycle") == "live"
    ]
    if len(matches) != 1:
        raise RuntimeError("Prime Agent active session is not uniquely live")
    return {
        "transport": "prime-agent",
        "active_session_id": rendered,
        "cli": "prime-agent",
    }


def resolve_dsh_session(
    session_id: str,
    repository: str | Path,
) -> dict[str, Any]:
    """Resolve one exact persisted top-level DSH session for a repository."""
    rendered = session_id.strip()
    if not DSH_SESSION_PATTERN.fullmatch(rendered):
        raise ValueError("invalid DeepSeek Harness session ID")
    root = resolve_repository(repository)
    dsh_home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))).expanduser()
    session_root = Path(
        os.environ.get("SIN_DSH_SESSION_ROOT", str(dsh_home / "sessions-sin"))
    ).expanduser()
    matches: list[dict[str, Any]] = []
    if session_root.is_dir():
        for path in session_root.rglob("session.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    first = handle.readline()
                header = json.loads(first)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(header, dict) or header.get("type") != "session":
                continue
            if header.get("id") != rendered:
                continue
            if int(header.get("delegationDepth") or 0) != 0:
                raise RuntimeError("DeepSeek Harness callback target is not a top-level session")
            cwd = header.get("cwd")
            if not isinstance(cwd, str) or Path(cwd).expanduser().resolve() != root:
                raise RuntimeError("DeepSeek Harness callback repository identity mismatch")
            matches.append({"path": str(path), "cwd": cwd})
    if len(matches) != 1:
        raise RuntimeError("DeepSeek Harness session is not uniquely persisted for this repository")
    api_url = os.environ.get("SIN_DSH_CALLBACK_URL", "http://127.0.0.1:61368").rstrip("/")
    parsed = urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("DeepSeek Harness callback host must be loopback HTTP")
    return {
        "transport": "deepseek-harness",
        "session_id": rendered,
        "api_url": api_url,
        "session_log": matches[0]["path"],
        "cli": "dsh",
    }


def _post_dsh_session_prompt(
    *,
    api_url: str,
    session_id: str,
    message: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    rpc_id = str(uuid.uuid4())
    body = {
        "type": "client-request",
        "rpcId": rpc_id,
        "method": "session.prompt",
        "payload": {
            "sessionId": session_id,
            "mode": "queue",
            "content": [{"type": "text", "text": message}],
        },
    }
    request = urllib_request.Request(
        f"{api_url.rstrip('/')}/api/session.prompt",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
    with opener.open(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("type") != "server-response":
        raise RuntimeError("DeepSeek Harness callback response envelope is invalid")
    if payload.get("rpcId") != rpc_id:
        raise RuntimeError("DeepSeek Harness callback RPC identity mismatch")
    result = payload.get("result")
    value = result.get("value") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(value, dict) or value.get("accepted") is not True:
        raise RuntimeError("DeepSeek Harness callback was not accepted")
    return payload


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
    prime_agent_session_id: str | None = None,
    dsh_session_id: str | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    round_number: int = 1,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> dict[str, Any]:
    root = resolve_repository(repository)
    task = _validate_task_id(task_id)
    if not 5 <= ttl_minutes <= 7 * 24 * 60:
        raise ValueError("callback TTL must be between 5 and 10080 minutes")
    if max_rounds < 0 or max_rounds > 500:
        raise ValueError("max rounds must be 0 (unbounded) or between 1 and 500")
    if round_number < 1 or (max_rounds and round_number > max_rounds):
        raise ValueError("round must be positive and within max rounds")

    selected_origins = sum(
        bool(value)
        for value in (prime_agent_session_id, dsh_session_id, origin_terminal or origin_session_id)
    )
    if selected_origins > 1:
        raise ValueError(
            "callback origin must be exactly one of DeepSeek Harness, Prime Agent, or OpenCode fields"
        )
    prime_transport = (
        resolve_prime_agent_session(prime_agent_session_id)
        if prime_agent_session_id
        else None
    )
    dsh_transport = (
        resolve_dsh_session(dsh_session_id, root)
        if dsh_session_id
        else None
    )
    if prime_transport is None and dsh_transport is None:
        terminal, terminal_source, terminal_record = resolve_origin_terminal(
            root,
            origin_terminal,
        )
        session = resolve_origin_session(root, terminal_record, origin_session_id)
        origin_agent = "opencode"
        origin_transport = None
    else:
        terminal = None
        terminal_source = "deepseek-harness" if dsh_transport else "prime-agent"
        session = None
        origin_agent = "deepseek-harness" if dsh_transport else "prime-agent"
        origin_transport = dsh_transport or prime_transport
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
        "origin_agent": origin_agent,
        "origin_transport": origin_transport,
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
        "origin_agent": record["origin_agent"],
        "origin_transport": origin_transport,
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
        "origin_agent": record.get("origin_agent", "opencode"),
        "origin_transport": record.get("origin_transport"),
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
        "completion_handoff_state": (
            record.get("completion_handoff", {}).get("state")
            if isinstance(record.get("completion_handoff"), dict)
            else None
        ),
        "completion_handoff_staged_at": (
            record.get("completion_handoff", {}).get("staged_at")
            if isinstance(record.get("completion_handoff"), dict)
            else None
        ),
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


def _handoff_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Return the callback identity that a staged completion is bound to."""
    return {
        "schema_version": record.get("schema_version"),
        "task_id": record.get("task_id"),
        "round": record.get("round"),
        "repository_root": record.get("repository_root"),
        "origin_agent": record.get("origin_agent"),
        "origin_terminal": record.get("origin_terminal"),
        "origin_session": record.get("origin_session"),
        "origin_transport": record.get("origin_transport"),
        "conversation": record.get("conversation"),
        "relay_id": record.get("relay_id"),
        "expires_at": record.get("expires_at"),
    }


def _handoff_mac(token: str, payload: dict[str, Any]) -> str:
    material = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(token.encode("utf-8"), material, hashlib.sha256).hexdigest()


def stage_callback_handoff(
    *,
    repository: str | Path,
    token: str,
    final_status: str,
    summary: str,
    changed: list[str] | None = None,
    verification: str = "unknown",
    next_action: str | None = None,
) -> dict[str, Any]:
    """Persist a cryptographically callback-bound completion before delivery.

    This is intentionally separate from transport delivery. If the ChatGPT tool
    execution window ends after staging but before ``web-callback-send``, the
    local watchdog can validate this handoff against the exact callback identity
    and canonical taskplan evidence, then perform the original one-shot send.
    """
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
        if record.get("status") != "open":
            raise RuntimeError("only an open callback can stage completion")
        conversation = record.get("conversation")
        conversation_bound = isinstance(conversation, dict) and bool(conversation.get("url"))
        if final_status == "done" and rendered_verification.casefold() in {
            "unknown",
            "none",
            "not-run",
            "not run",
        }:
            raise ValueError("done handoff requires concrete verification")
        taskplan_validation = None
        delivery_mode = "callback-bound"
        if not conversation_bound:
            # A missing browser binding must never be papered over by inventing a
            # conversation.  The only safe fallback is a distinct origin wake-up
            # backed by the canonical taskplan/report.  It does not become a
            # normal completion callback and therefore carries no archive authority.
            taskplan_validation = _validate_taskplan_evidence(
                root,
                task_id=str(record.get("task_id") or ""),
                final_status=final_status,
            )
            delivery_mode = "origin-reconcile-unbound"
        action = _compact(next_action or _default_next_action(record), 1800)
        outcome = {
            "status": final_status,
            "summary": rendered_summary,
            "changed_files": rendered_changed,
            "verification": rendered_verification,
            "next_action": action,
        }
        if delivery_mode != "callback-bound":
            outcome["delivery_mode"] = delivery_mode
        payload = {
            "version": 1,
            "identity": _handoff_identity(record),
            "outcome": outcome,
            "staged_at": isoformat(utc_now()),
        }
        record["completion_handoff"] = {
            **payload,
            "mac_sha256": _handoff_mac(token, payload),
            "state": "staged",
        }
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-handoff-staged",
        "task_id": record.get("task_id"),
        "round": record.get("round"),
        "conversation": record.get("conversation"),
        "delivery_mode": delivery_mode,
        "taskplan_validation": taskplan_validation,
    }


def validate_callback_handoff(
    repository: Path,
    token: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    handoff = record.get("completion_handoff")
    if not isinstance(handoff, dict) or handoff.get("state") != "staged":
        raise RuntimeError("callback has no staged completion handoff")
    payload = {
        "version": handoff.get("version"),
        "identity": handoff.get("identity"),
        "outcome": handoff.get("outcome"),
        "staged_at": handoff.get("staged_at"),
    }
    expected = _handoff_mac(token, payload)
    actual = str(handoff.get("mac_sha256") or "")
    if not hmac.compare_digest(expected, actual):
        raise RuntimeError("completion handoff MAC validation failed")
    if payload.get("identity") != _handoff_identity(record):
        raise RuntimeError("completion handoff callback identity mismatch")
    outcome = payload.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("status") not in FINAL_STATUSES:
        raise RuntimeError("completion handoff outcome is invalid")
    if Path(str(record.get("repository_root", ""))).resolve() != repository:
        raise RuntimeError("completion handoff repository mismatch")
    return outcome


def _validate_taskplan_evidence(
    repository: Path,
    *,
    task_id: str,
    final_status: str,
) -> dict[str, Any]:
    db_path = repository / ".sin-gpt-web" / "taskplan.sqlite3"
    if not db_path.is_file():
        raise RuntimeError("canonical taskplan database is missing")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("canonical taskplan integrity check failed")
        row = connection.execute(
            "SELECT status,evidence,completion_report,blocked_reason FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("callback task is missing from canonical taskplan")
    status = str(row["status"] or "")
    evidence = str(row["evidence"] or "").strip()
    report = str(row["completion_report"] or "").strip()
    blocker = str(row["blocked_reason"] or "").strip()
    if final_status == "done" and (status != "done" or not evidence or not report):
        raise RuntimeError("done handoff lacks independent canonical task completion evidence")
    if final_status == "blocked" and (status != "blocked" or not blocker):
        raise RuntimeError("blocked handoff lacks canonical task blocker evidence")
    return {
        "task_status": status,
        "evidence_present": bool(evidence),
        "completion_report_present": bool(report),
        "blocked_reason_present": bool(blocker),
    }


def reconcile_callback_handoff(
    *,
    repository: str | Path,
    token: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate a staged completion and perform the original callback send."""
    root = resolve_repository(repository)
    with callback_lock(root, token):
        record = load_callback(root, token)
        if _expire_callback_if_needed(root, token, record):
            raise RuntimeError("callback capability has expired")
        if record.get("status") != "open":
            return {
                "ok": True,
                "status": "callback-handoff-noop",
                "callback_status": record.get("status"),
                "task_id": record.get("task_id"),
                "round": record.get("round"),
            }
        outcome = validate_callback_handoff(root, token, record)
        taskplan = _validate_taskplan_evidence(
            root,
            task_id=str(record.get("task_id") or ""),
            final_status=str(outcome["status"]),
        )
    if outcome.get("delivery_mode") == "origin-reconcile-unbound":
        result = _deliver_unbound_origin_reconciliation(
            repository=root,
            token=token,
            outcome=outcome,
            taskplan_validation=taskplan,
            dry_run=dry_run,
            relay_fallback=not dry_run,
        )
    else:
        result = send_callback(
            repository=root,
            token=token,
            final_status=str(outcome["status"]),
            summary=str(outcome["summary"]),
            changed=list(outcome.get("changed_files") or []),
            verification=str(outcome.get("verification") or "unknown"),
            next_action=str(outcome.get("next_action") or ""),
            dry_run=dry_run,
            relay_fallback=not dry_run,
        )
    result["handoff_reconciled"] = True
    result["taskplan_validation"] = taskplan
    return result


def _default_next_action(record: dict[str, Any]) -> str:
    round_number = int(record.get("round") or 1)
    raw_max_rounds = record.get("max_rounds")
    max_rounds = DEFAULT_MAX_ROUNDS if raw_max_rounds is None else int(raw_max_rounds)
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
    if max_rounds > 0 and round_number >= max_rounds:
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
    terminals = list_repository_terminals(repository, allow_busy=True)
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


def wait_for_delivery_terminal_idle(
    terminal: str,
    *,
    timeout_seconds: int = DEFAULT_TUI_IDLE_TIMEOUT_SECONDS,
) -> None:
    """Wait until the target TUI can accept a new agent turn.

    A connected terminal can still be consuming the previous OpenCode turn.
    Sending while it is busy writes input into the PTY without reliably waking
    the current session, so delivery must wait for Orca's explicit idle state.
    """
    run_orca(
        [
            "terminal",
            "wait",
            "--terminal",
            terminal,
            "--for",
            "tui-idle",
            "--timeout-ms",
            str(timeout_seconds * 1000),
        ],
        timeout=timeout_seconds + 5,
    )


def _render_unbound_origin_reconciliation_message(
    record: dict[str, Any],
    *,
    summary: str,
    changed: list[str],
    verification: str,
    next_action: str,
) -> str:
    """Render a wake-up that cannot be mistaken for a canonical callback."""
    transport = record.get("origin_transport")
    if isinstance(transport, dict) and transport.get("transport") == "deepseek-harness":
        transport_name = "deepseek-harness"
        target = str(transport.get("session_id") or "unresolved")
    elif isinstance(transport, dict) and transport.get("transport") == "prime-agent":
        transport_name = "prime-agent"
        target = str(transport.get("active_session_id") or "unresolved")
    else:
        transport_name = "opencode-terminal"
        session = record.get("origin_session")
        target = (
            str(session.get("id") or "unresolved")
            if isinstance(session, dict)
            else str(record.get("origin_terminal") or "unresolved")
        )
    reconciliation = record.get("origin_reconciliation")
    outcome_status = (
        str(reconciliation.get("outcome_status") or "unknown")
        if isinstance(reconciliation, dict)
        else "unknown"
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
            "SIN_GPT_WEB_ORIGIN_RECONCILE "
            f"task={record['task_id']} outcome={outcome_status} "
            f"delivery={record['delivery_id']} transport={transport_name} "
            f"target={target} round={record.get('round', 1)}",
            "REASON: The callback was never bound to a canonical ChatGPT conversation. "
            "No canonical completion callback or archive/close authority is being asserted.",
            f"WORKER_SUMMARY: {summary}",
            f"CHANGED: {json.dumps(changed or ['none'], ensure_ascii=False)}",
            f"VERIFY: {verification or 'unknown'}",
            "CHATGPT_PAGE_ID: unresolved",
            "CHATGPT_CONVERSATION_URL: unresolved",
            f"REQUIRED_ACTION: {next_action}",
            "ORIGIN_RECONCILE_ACTION: Refresh the repository's canonical taskplan/report, "
            "independently verify repository state, diff and tests, then continue the CEO loop. "
            "Do not infer or perform archive/close from this notice; exact conversation identity is missing.",
            f"RECEIPT_ACTION: {receipt_command}",
            "Process this reconciliation delivery ID at most once and acknowledge it only after processing.",
            "This is an origin wake-up/reconciliation event, not proof of completion.",
        ]
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
    if record.get("completion_mode") == "origin-reconcile-unbound":
        return _render_unbound_origin_reconciliation_message(
            record,
            summary=summary,
            changed=changed,
            verification=verification,
            next_action=next_action,
        )
    session = record.get("origin_session")
    session_id = session.get("id") if isinstance(session, dict) else None
    conversation = record.get("conversation")
    conversation_url = (
        conversation.get("url") if isinstance(conversation, dict) else None
    )
    page_id = conversation.get("page_id") if isinstance(conversation, dict) else None
    transport = record.get("origin_transport")
    prime_target = (
        transport.get("active_session_id")
        if isinstance(transport, dict) and transport.get("transport") == "prime-agent"
        else None
    )
    dsh_target = (
        transport.get("session_id")
        if isinstance(transport, dict) and transport.get("transport") == "deepseek-harness"
        else None
    )
    if dsh_target:
        session_display = f"dsh_session={dsh_target} "
        transport_name = "deepseek-harness"
        target = dsh_target
    elif prime_target:
        session_display = f"prime_session={prime_target} "
        transport_name = "prime-agent"
        target = prime_target
    else:
        session_display = f"opencode_session={session_id or 'unresolved'} "
        transport_name = "opencode-terminal"
        target = session_id or "unresolved"
    header = (
        "SIN_GPT_WEB_CALLBACK "
        f"task={record['task_id']} status={final_status} "
        f"delivery={record['delivery_id']} "
        f"{session_display}"
        f"transport={transport_name} "
        f"target={target} "
        f"round={record.get('round', 1)}/"
        f"{'∞' if int(record.get('max_rounds', DEFAULT_MAX_ROUNDS) or 0) == 0 else record.get('max_rounds', DEFAULT_MAX_ROUNDS)}"
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
    post_completion_action = (
        "POST_COMPLETION_ACTION: For status=done, after independent verification, "
        "archive the exact CHATGPT_CONVERSATION_URL with "
        "sin-gpt-web-archive --repo <repo> --conversation-url <url> --yes, "
        "then verify that every Orca tab with that URL is closed. Only after the "
        "archive and tab-close verification, commit the intended changes and push "
        "them to origin/main. Never push before the completed ChatGPT Web chat is "
        "archived and all matching tabs are closed."
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
            post_completion_action,
            "Process this delivery ID at most once and send its receipt only after processing.",
            "Treat this callback as a wake-up event, not as proof of completion. "
            "Inspect repository state, taskplan evidence, diff, and tests before accepting it.",
        ]
    )


def _deliver_dsh_callback(
    repository: Path,
    token: str,
    record: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    transport = record.get("origin_transport")
    session_id = (
        transport.get("session_id")
        if isinstance(transport, dict) and transport.get("transport") == "deepseek-harness"
        else None
    )
    api_url = transport.get("api_url") if isinstance(transport, dict) else None
    if not isinstance(session_id, str) or not isinstance(api_url, str):
        raise RuntimeError("invalid DeepSeek Harness callback transport")
    try:
        resolve_dsh_session(session_id, repository)
    except (RuntimeError, ValueError):
        record.update(
            {
                "status": "pending-delivery",
                "delivery_reason": "dsh-session-offline",
            }
        )
        atomic_write_json(callback_path(repository, token), record)
        return {
            "ok": True,
            "status": "callback-pending",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_reason": "dsh-session-offline",
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
            "origin_dsh_session": session_id,
            "target_source": "dsh-session",
            "message": message,
        }
    try:
        _post_dsh_session_prompt(
            api_url=api_url,
            session_id=session_id,
            message=message,
        )
    except urllib_error.HTTPError as error:
        record.update(
            {
                "status": "pending-delivery",
                "delivery_state": "pending",
                "delivery_reason": f"dsh-host-http-{error.code}",
            }
        )
        atomic_write_json(callback_path(repository, token), record)
        return {
            "ok": True,
            "status": "callback-pending",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_reason": record["delivery_reason"],
        }
    except (OSError, TimeoutError, urllib_error.URLError, RuntimeError, json.JSONDecodeError) as error:
        record.update(
            {
                "status": "delivery-indeterminate",
                "delivery_state": "indeterminate",
                "delivery_reason": "dsh-session-prompt-indeterminate",
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
            "delivery_reason": "dsh-session-prompt-indeterminate",
        }
    record.update(
        {
            "status": "sent",
            "delivery_state": "sent",
            "delivery_dsh_session": session_id,
            "delivery_target_source": "dsh-session",
            "delivery_receipt_status": "accepted",
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
        "origin_dsh_session": session_id,
        "round": record.get("round"),
        "max_rounds": record.get("max_rounds"),
        "conversation": record.get("conversation"),
        "sent_at": record.get("sent_at"),
    }


def _deliver_prime_agent_callback(
    repository: Path,
    token: str,
    record: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    transport = record.get("origin_transport")
    session_id = (
        transport.get("active_session_id")
        if isinstance(transport, dict) and transport.get("transport") == "prime-agent"
        else None
    )
    if not isinstance(session_id, str):
        raise RuntimeError("invalid Prime Agent callback transport")
    try:
        resolve_prime_agent_session(session_id)
    except RuntimeError:
        record.update(
            {
                "status": "pending-delivery",
                "delivery_reason": "prime-agent-session-offline",
            }
        )
        atomic_write_json(callback_path(repository, token), record)
        return {
            "ok": True,
            "status": "callback-pending",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_reason": "prime-agent-session-offline",
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
            "origin_prime_agent_session": session_id,
            "target_source": "prime-agent-session",
            "message": message,
        }
    binary = shutil.which("prime-agent")
    if binary is None:
        raise RuntimeError("prime-agent CLI is unavailable")
    try:
        process = subprocess.run(
            [binary, "send", session_id, message, "--json"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        payload = json.loads(process.stdout) if process.returncode == 0 else None
        target = payload.get("target") if isinstance(payload, dict) else None
        if (
            not isinstance(target, dict)
            or target.get("activeSessionId") != session_id
            or payload.get("deliveryStatus") not in {"delivered", "queued"}
        ):
            raise RuntimeError("Prime Agent delivery receipt was invalid")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError) as error:
        record.update(
            {
                "status": "delivery-indeterminate",
                "delivery_state": "indeterminate",
                "delivery_reason": "prime-agent-send-indeterminate",
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
            "delivery_reason": "prime-agent-send-indeterminate",
        }
    record.update(
        {
            "status": "sent",
            "delivery_state": "sent",
            "delivery_prime_agent_session": session_id,
            "delivery_target_source": "prime-agent-session",
            "delivery_receipt_status": payload["deliveryStatus"],
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
        "origin_prime_agent_session": session_id,
        "round": record.get("round"),
        "max_rounds": record.get("max_rounds"),
        "conversation": record.get("conversation"),
        "sent_at": record.get("sent_at"),
    }


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
    transport = record.get("origin_transport")
    if isinstance(transport, dict) and transport.get("transport") == "deepseek-harness":
        return _deliver_dsh_callback(repository, token, record, dry_run=dry_run)
    if isinstance(transport, dict) and transport.get("transport") == "prime-agent":
        return _deliver_prime_agent_callback(repository, token, record, dry_run=dry_run)
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
        wait_for_delivery_terminal_idle(terminal)
    except Exception as error:
        record.update(
            {
                "status": "pending-delivery",
                "delivery_reason": "terminal-not-idle",
                "delivery_error": _compact(str(error), 700) or type(error).__name__,
            }
        )
        atomic_write_json(callback_path(repository, token), record)
        return {
            "ok": True,
            "status": "callback-pending",
            "callback": token,
            "task_id": record.get("task_id"),
            "delivery_id": record.get("delivery_id"),
            "delivery_reason": "terminal-not-idle",
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


def _deliver_unbound_origin_reconciliation(
    *,
    repository: Path,
    token: str,
    outcome: dict[str, Any],
    taskplan_validation: dict[str, Any],
    dry_run: bool,
    relay_fallback: bool,
) -> dict[str, Any]:
    """Deliver a distinct exact-origin wake-up for an unbound completion handoff.

    The callback itself never becomes a canonical ``done`` callback.  We persist
    ``callback_status=reconcile`` and a dedicated mode so every downstream reader
    can distinguish this from a conversation-bound completion.  The existing
    exact-origin transports and bounded relay are reused after the durable record
    is written.
    """
    root = resolve_repository(repository)
    with callback_lock(root, token):
        record = load_callback(root, token)
        if _expire_callback_if_needed(root, token, record):
            raise RuntimeError("callback capability has expired")
        existing_mode = str(record.get("completion_mode") or "")
        if record.get("status") in {"sent", "acknowledged"} and existing_mode == "origin-reconcile-unbound":
            return {
                "ok": True,
                "status": "callback-origin-reconciled",
                "reused": True,
                "task_id": record.get("task_id"),
                "round": record.get("round"),
                "delivery_id": record.get("delivery_id"),
            }
        if record.get("status") == "open":
            conversation = record.get("conversation")
            if isinstance(conversation, dict) and conversation.get("url"):
                raise RuntimeError("bound callback must use canonical callback delivery")
            delivery_id = f"gptwcd_{uuid.uuid4().hex}"
            summary = _compact(str(outcome.get("summary") or ""), 700)
            changed = _changed_files(list(outcome.get("changed_files") or []))
            verification = _compact(str(outcome.get("verification") or ""), 500) or "unknown"
            next_action = _compact(str(outcome.get("next_action") or ""), 1800)
            record.update(
                {
                    "status": "pending-delivery",
                    "callback_status": "reconcile",
                    "completion_mode": "origin-reconcile-unbound",
                    "dispatch_started_at": isoformat(utc_now()),
                    "summary": summary,
                    "changed_files": changed,
                    "verification": verification,
                    "next_action": next_action,
                    "delivery_id": delivery_id,
                    "delivery_state": "pending",
                    "origin_reconciliation": {
                        "mode": "origin-reconcile-unbound",
                        "state": "pending",
                        "outcome_status": str(outcome.get("status") or "unknown"),
                        "taskplan_validation": taskplan_validation,
                        "staged_at": isoformat(utc_now()),
                    },
                }
            )
            message = render_callback_message(
                record,
                final_status="reconcile",
                summary=summary,
                changed=changed,
                verification=verification,
                next_action=next_action,
            )
            if dry_run:
                return {
                    "ok": True,
                    "status": "callback-origin-reconcile-dry-run",
                    "task_id": record.get("task_id"),
                    "round": record.get("round"),
                    "delivery_id": delivery_id,
                    "message": message,
                }
            record["message_sha256"] = hashlib.sha256(message.encode("utf-8")).hexdigest()
            handoff = record.get("completion_handoff")
            if isinstance(handoff, dict):
                record["completion_handoff"] = {
                    **handoff,
                    "state": "consumed",
                    "consumed_at": isoformat(utc_now()),
                    "delivery_id": delivery_id,
                }
            atomic_write_json(callback_path(root, token), record)
        elif existing_mode != "origin-reconcile-unbound" or record.get("status") not in {
            "pending-delivery",
            "delivery-indeterminate",
        }:
            raise RuntimeError("callback cannot enter unbound origin reconciliation from its current state")

        result = _deliver_pending_callback(root, token, record, dry_run=False)
        reconciliation = record.get("origin_reconciliation")
        if isinstance(reconciliation, dict):
            if result.get("status") == "callback-sent":
                reconciliation["state"] = "sent"
                reconciliation["sent_at"] = isoformat(utc_now())
            elif result.get("status") == "callback-pending":
                reconciliation["state"] = "pending"
            else:
                reconciliation["state"] = "indeterminate"
            atomic_write_json(callback_path(root, token), record)
    if relay_fallback and result.get("status") == "callback-pending":
        result["relay_fallback"] = install_callback_relay(
            repository=root,
            token=token,
        )
    if result.get("status") == "callback-sent":
        result["status"] = "callback-origin-reconciled"
    result["origin_reconciliation"] = True
    return result


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
            if isinstance(record.get("completion_handoff"), dict):
                staged = validate_callback_handoff(root, token, record)
                staged_values = (
                    str(staged.get("status") or ""),
                    str(staged.get("summary") or ""),
                    list(staged.get("changed_files") or []),
                    str(staged.get("verification") or ""),
                    str(staged.get("next_action") or ""),
                )
                provided_values = (
                    final_status,
                    rendered_summary,
                    rendered_changed,
                    rendered_verification,
                    action,
                )
                if staged_values != provided_values:
                    raise RuntimeError(
                        "callback send does not match staged completion handoff"
                    )
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
            if isinstance(record.get("completion_handoff"), dict):
                record["completion_handoff"] = {
                    **record["completion_handoff"],
                    "state": "consumed",
                    "consumed_at": isoformat(utc_now()),
                    "delivery_id": delivery_id,
                }
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
        if record.get("completion_mode") == "origin-reconcile-unbound" and not dry_run:
            reconciliation = record.get("origin_reconciliation")
            if isinstance(reconciliation, dict):
                if result.get("status") == "callback-sent":
                    reconciliation["state"] = "sent"
                    reconciliation["sent_at"] = isoformat(utc_now())
                elif result.get("status") == "callback-pending":
                    reconciliation["state"] = "pending"
                else:
                    reconciliation["state"] = "indeterminate"
                atomic_write_json(callback_path(root, token), record)
            if result.get("status") == "callback-sent":
                result["status"] = "callback-origin-reconciled"
            result["origin_reconciliation"] = True
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


def mark_archive_verified(
    *,
    repository: str | Path,
    token: str,
    delivery_id: str,
    conversation_url: str,
    closed_tab_count: int,
) -> dict[str, Any]:
    """Persist the exact archive-and-close proof required by a done receipt."""
    root = resolve_repository(repository)
    with callback_lock(root, token):
        record = load_callback(root, token)
        if record.get("delivery_id") != delivery_id or not DELIVERY_ID_PATTERN.fullmatch(delivery_id):
            raise RuntimeError("archive proof delivery ID does not match")
        if record.get("callback_status") != "done":
            raise RuntimeError("archive proof is valid only for a done callback")
        conversation = record.get("conversation")
        expected_url = conversation.get("url") if isinstance(conversation, dict) else None
        if not expected_url or str(expected_url) != str(conversation_url):
            raise RuntimeError("archive proof conversation does not match callback binding")
        if int(closed_tab_count) < 1:
            raise RuntimeError("archive proof requires at least one closed matching tab")
        record["archive_gate"] = {
            "status": "verified",
            "delivery_id": delivery_id,
            "conversation_url": str(conversation_url),
            "closed_tab_count": int(closed_tab_count),
            "verified_at": isoformat(utc_now()),
        }
        atomic_write_json(callback_path(root, token), record)
    return {
        "ok": True,
        "status": "callback-archive-verified",
        "task_id": record.get("task_id"),
        "round": record.get("round"),
        "delivery_id": delivery_id,
    }


def acknowledge_callback(
    *,
    repository: str | Path,
    token: str,
    delivery_id: str,
) -> dict[str, Any]:
    """Record one receipt only after any required completion gate is proven."""
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
        if record.get("callback_status") == "done":
            gate = record.get("archive_gate")
            conversation = record.get("conversation")
            expected_url = conversation.get("url") if isinstance(conversation, dict) else None
            if (
                not isinstance(gate, dict)
                or gate.get("status") != "verified"
                or gate.get("delivery_id") != delivery_id
                or gate.get("conversation_url") != expected_url
                or int(gate.get("closed_tab_count") or 0) < 1
            ):
                raise RuntimeError("completed callback archive-and-close gate is not verified")
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


def abandon_callback(
    *,
    repository: str | Path,
    token: str,
    reason: str,
) -> dict[str, Any]:
    """Terminally abandon an active callback only after its target is absent.

    This is an explicit operator recovery path for orphaned callback records.
    Unlike ordinary cancellation, it permits a persisted completion that cannot
    be delivered, but it never abandons a callback whose exact target resolves.
    The record, delivery data, and original repository claim remain intact for
    audit; only a terminal ``abandoned`` state is appended.
    """
    root = resolve_repository(repository)
    rendered_reason = _compact(reason, 700)
    if not rendered_reason:
        raise ValueError("abandonment reason must not be empty")
    with callback_lock(root, token):
        # Deliberately validate the physical, repository-local capability here
        # without requiring its historical repository_root claim to still exist.
        # The latter is precisely the orphan-recovery case.
        path = callback_path(root, token)
        if not path.is_file():
            raise RuntimeError(f"callback capability not found: {token}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid callback capability: {token}") from error
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != CALLBACK_SCHEMA_VERSION
            or record.get("token") != token
        ):
            raise RuntimeError(f"invalid callback capability: {token}")
        status = record.get("status")
        if status == "abandoned":
            return {
                "ok": True,
                "status": "callback-abandoned",
                "callback": token,
                "reused": True,
            }
        if status not in {"open", "pending-delivery", "delivery-indeterminate"}:
            raise RuntimeError(
                f"a {status} callback cannot be abandoned; delivery may have occurred"
            )
        transport = record.get("origin_transport")
        if record.get("origin_agent") == "deepseek-harness":
            if not isinstance(transport, dict) or transport.get("transport") != "deepseek-harness":
                raise RuntimeError("invalid DeepSeek Harness callback transport")
            session_id = transport.get("session_id")
            if not isinstance(session_id, str):
                raise RuntimeError("invalid DeepSeek Harness callback transport")
            try:
                resolve_dsh_session(session_id, root)
            except (RuntimeError, ValueError):
                target_source = "dsh-session-offline"
            else:
                raise RuntimeError(
                    "callback target still resolves; use normal delivery or cancellation"
                )
        elif record.get("origin_agent") == "prime-agent":
            if not isinstance(transport, dict) or transport.get("transport") != "prime-agent":
                raise RuntimeError("invalid Prime Agent callback transport")
            session_id = transport.get("active_session_id")
            if not isinstance(session_id, str):
                raise RuntimeError("invalid Prime Agent callback transport")
            try:
                resolve_prime_agent_session(session_id)
            except RuntimeError:
                target_source = "prime-agent-session-offline"
            else:
                raise RuntimeError(
                    "callback target still resolves; use normal delivery or cancellation"
                )
        else:
            terminal, target_source = resolve_delivery_terminal(root, record)
            if terminal:
                raise RuntimeError(
                    "callback target still resolves; use normal delivery or cancellation"
                )
        record.update(
            {
                "status": "abandoned",
                "abandoned_at": isoformat(utc_now()),
                "abandon_reason": rendered_reason,
                "abandon_delivery_reason": target_source,
                "abandon_repository_root_mismatch": (
                    Path(str(record.get("repository_root", ""))).resolve() != root
                ),
            }
        )
        _remove_relay_fallback(root, record, reason="callback-abandoned")
        atomic_write_json(path, record)
    return {
        "ok": True,
        "status": "callback-abandoned",
        "callback": token,
        "reused": False,
        "task_id": record.get("task_id"),
        "round": record.get("round"),
        "delivery_reason": target_source,
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
        replaceable_pending = (
            status == "pending-delivery"
            and record.get("callback_status") == "blocked"
            and record.get("delivery_state") == "pending"
        )
        if status != "open" and not replaceable_pending:
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
