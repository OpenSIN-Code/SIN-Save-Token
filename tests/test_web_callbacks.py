from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_orca.web_callbacks import (  # noqa: E402
    bind_callback,
    callback_path,
    callback_status,
    cancel_callback,
    open_callback,
    resolve_origin_session,
    resolve_origin_terminal,
    send_callback,
)


def git_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path.resolve()


def terminal_payload(
    repository: Path,
    *records: dict[str, object],
    **record_fields: object,
) -> dict[str, object]:
    defaults = {
        "worktreePath": str(repository),
        "connected": True,
        "writable": True,
    }
    combined = [*records]
    if record_fields:
        combined.append(record_fields)
    return {
        "ok": True,
        "result": {
            "terminals": [
                {**defaults, **record}
                for record in combined
            ]
        },
    }


def orca_router(payload: dict[str, object], sent: list[list[str]]):
    def run(arguments: list[str], *, timeout: int = 180):
        del timeout
        if arguments[:2] == ["terminal", "list"]:
            return payload
        if arguments[:2] == ["terminal", "send"]:
            sent.append(arguments)
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected Orca call: {arguments}")

    return run


def test_open_binds_exact_terminal_and_session(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · GPT-5.6",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        result = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            ttl_minutes=60,
            round_number=3,
            max_rounds=20,
        )

    assert result["status"] == "callback-open"
    assert result["origin_terminal"] == "term-origin"
    assert result["origin_session"]["id"] == "ses_ABC123"
    assert result["round"] == 3
    record = json.loads(Path(result["record"]).read_text(encoding="utf-8"))
    assert record["status"] == "open"
    assert record["repository_root"] == str(repository)
    assert Path(result["record"]).stat().st_mode & 0o777 == 0o600
    assert "web-callback-send" in result["callback_command_template"]


def test_multiple_opencode_terminals_require_explicit_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("SIN_GPT_WEB_ORIGIN_TERMINAL", raising=False)
    monkeypatch.delenv("SIN_ORCA_PARENT_TERMINAL", raising=False)
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-shell",
        title="shell",
        preview="zsh prompt",
        lastOutputAt=999,
    )
    payload["result"]["terminals"].extend([
        {
            "handle": "term-old-agent",
            "worktreePath": str(repository),
            "connected": True,
            "writable": True,
            "title": "OC | old",
            "preview": "Build · model",
            "lastOutputAt": 100,
        },
        {
            "handle": "term-current-agent",
            "worktreePath": str(repository),
            "connected": True,
            "writable": True,
            "title": "Terminal 2",
            "preview": "Build · GPT-5.6\nctrl+p commands",
            "lastOutputAt": 200,
        },
    ])
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        with pytest.raises(RuntimeError, match="ambiguous"):
            resolve_origin_terminal(repository)


def test_ambiguous_origin_requires_explicit_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("SIN_GPT_WEB_ORIGIN_TERMINAL", raising=False)
    monkeypatch.delenv("SIN_ORCA_PARENT_TERMINAL", raising=False)
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-a",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    payload["result"]["terminals"].append({
        "handle": "term-b",
        "worktreePath": str(repository),
        "connected": True,
        "writable": True,
        "title": "OpenCode",
        "preview": "Build · model",
        "lastOutputAt": 100,
    })
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        with pytest.raises(RuntimeError, match="ambiguous"):
            resolve_origin_terminal(repository)


def test_session_resolves_exactly_from_orca_pane_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = git_repository(tmp_path / "repo")
    state_path = tmp_path / "orca-data.json"
    terminal = {
        "handle": "term-origin",
        "tabId": "tab-1",
        "leafId": "leaf-2",
        "ptyId": "pty-exact",
        "worktreeId": f"repo::{repository}",
    }
    state_path.write_text(
        json.dumps({
            "workspaceSession": {
                "terminalLayoutsByTabId": {
                    "tab-1": {
                        "ptyIdsByLeafId": {"leaf-2": "pty-exact"},
                    },
                },
                "sleepingAgentSessionsByPaneKey": {
                    "tab-1:leaf-2": {
                        "tabId": "tab-1",
                        "worktreeId": f"repo::{repository}",
                        "agent": "opencode",
                        "providerSession": {
                            "key": "session_id",
                            "id": "ses_EXACT123",
                        },
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SIN_ORCA_STATE_FILE", str(state_path))

    result = resolve_origin_session(repository, terminal)

    assert result["id"] == "ses_EXACT123"
    assert result["source"] == "orca-agent-session-state"
    assert result["confidence"] == "exact"
    assert result["pane_key"] == "tab-1:leaf-2"


def test_multiple_repository_sessions_are_never_guessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = git_repository(tmp_path / "repo")
    missing_state = tmp_path / "missing-orca-data.json"
    monkeypatch.setenv("SIN_ORCA_STATE_FILE", str(missing_state))
    terminal = {
        "handle": "term-origin",
        "tabId": "tab-1",
        "leafId": "leaf-1",
        "ptyId": "pty-1",
    }
    stdout = json.dumps([
        {"id": "ses_OLD123", "directory": str(repository), "updated": 1},
        {"id": "ses_NEW123", "directory": str(repository), "updated": 999},
    ])
    completed = subprocess.CompletedProcess(
        args=["opencode"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )
    with (
        patch("sin_orca.web_callbacks.shutil.which", return_value="/usr/bin/opencode"),
        patch("sin_orca.web_callbacks.subprocess.run", return_value=completed),
    ):
        result = resolve_origin_session(repository, terminal)

    assert result["id"] is None
    assert result["source"] == "ambiguous-repository-sessions"
    assert result["confidence"] == "none"
    assert result["candidate_count"] == 2


def test_bind_then_send_wakes_origin_once(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    sent: list[list[str]] = []
    with patch(
        "sin_orca.web_callbacks.run_orca",
        side_effect=orca_router(payload, sent),
    ):
        opened = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            ttl_minutes=60,
            round_number=1,
            max_rounds=10,
        )
        token = opened["callback"]
        bound = bind_callback(
            repository=repository,
            token=token,
            page_id="page-123",
            conversation_url="https://chatgpt.com/c/conversation-123",
            title="CEO Loop",
            chatgpt_project="wow-my-zsh",
        )
        result = send_callback(
            repository=repository,
            token=token,
            final_status="done",
            summary="Implemented and verified the callback loop",
            changed=["lib/a.py, tests/test_a.py"],
            verification="pytest: passed",
        )

    assert bound["status"] == "callback-bound"
    assert result["status"] == "callback-sent"
    assert len(sent) == 1
    argv = sent[0]
    assert argv[argv.index("--terminal") + 1] == "term-origin"
    message = argv[argv.index("--text") + 1]
    assert "SIN_GPT_WEB_CALLBACK task=T-0020 status=done" in message
    assert "session=ses_ABC123" in message
    assert "https://chatgpt.com/c/conversation-123" in message
    assert "continue the CEO loop" in message
    assert "Treat this callback as a wake-up event" in message
    state = callback_status(repository=repository, token=token)
    assert state["status"] == "sent"
    assert state["callback_status"] == "done"
    with pytest.raises(RuntimeError, match="one-shot"):
        send_callback(
            repository=repository,
            token=token,
            final_status="done",
            summary="duplicate",
        )


def test_binding_rejects_non_conversation_chatgpt_url(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
        )

    with pytest.raises(ValueError, match="containing /c/"):
        bind_callback(
            repository=repository,
            token=opened["callback"],
            page_id="page-123",
            conversation_url="https://chatgpt.com/",
        )


def test_dry_run_does_not_consume_callback(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    sent: list[list[str]] = []
    with patch(
        "sin_orca.web_callbacks.run_orca",
        side_effect=orca_router(payload, sent),
    ):
        opened = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
        )
        result = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="blocked",
            summary="external credential required",
            dry_run=True,
        )

    assert result["status"] == "callback-dry-run"
    assert sent == []
    assert callback_status(
        repository=repository,
        token=opened["callback"],
    )["status"] == "open"


def test_delivery_failure_consumes_capability_without_replay(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )

    def failing_orca(arguments: list[str], *, timeout: int = 180):
        del timeout
        if arguments[:2] == ["terminal", "list"]:
            return payload
        if arguments[:2] == ["terminal", "send"]:
            raise RuntimeError("transport outcome unknown")
        raise AssertionError(f"unexpected Orca call: {arguments}")

    with patch("sin_orca.web_callbacks.run_orca", side_effect=failing_orca):
        opened = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
        )
        token = opened["callback"]
        with pytest.raises(RuntimeError, match="outcome unknown"):
            send_callback(
                repository=repository,
                token=token,
                final_status="done",
                summary="implementation complete",
            )

    state = callback_status(repository=repository, token=token)
    assert state["status"] == "delivery-failed"
    assert state["dispatch_started_at"]
    assert state["delivery_failed_at"]
    assert "outcome unknown" in state["delivery_error"]
    with pytest.raises(RuntimeError, match="one-shot"):
        send_callback(
            repository=repository,
            token=token,
            final_status="done",
            summary="must not replay",
        )


def test_expired_callback_is_rejected_and_marked(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
        )
    token = opened["callback"]
    path = callback_path(repository, token)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(RuntimeError, match="expired"):
        send_callback(
            repository=repository,
            token=token,
            final_status="failed",
            summary="timeout",
        )
    assert callback_status(repository=repository, token=token)["status"] == "expired"


def test_cancel_is_idempotent_and_prevents_send(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0020",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
        )
    token = opened["callback"]
    first = cancel_callback(
        repository=repository,
        token=token,
        reason="browser delegation failed before prompt submission",
    )
    second = cancel_callback(
        repository=repository,
        token=token,
        reason="same reason",
    )

    assert first["reused"] is False
    assert second["reused"] is True
    with pytest.raises(RuntimeError, match="one-shot"):
        send_callback(
            repository=repository,
            token=token,
            final_status="done",
            summary="should not send",
        )
