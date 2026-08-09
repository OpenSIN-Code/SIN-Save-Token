from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_orca.web_callbacks import (  # noqa: E402
    acknowledge_callback,
    bind_callback,
    callback_path,
    callback_status,
    cancel_callback,
    install_callback_relay,
    open_callback,
    relay_callback,
    resolve_callback_token,
    resolve_callback_token_for_relay,
    resolve_origin_session,
    resolve_origin_terminal,
    send_callback,
    _default_next_action,
)
from sin_orca import cli as sin_orca_cli  # noqa: E402


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
        "result": {"terminals": [{**defaults, **record} for record in combined]},
    }


def orca_router(payload: dict[str, object], sent: list[list[str]]):
    def run(arguments: list[str], *, timeout: int = 180):
        del timeout
        if arguments[:2] == ["terminal", "list"]:
            return payload
        if arguments[:2] == ["terminal", "wait"]:
            return {"ok": True, "result": {"status": "tui-idle"}}
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
    template = result["callback_command_template"]
    assert "web-callback-send" in template
    assert "--task-id T-0020" in template
    assert "--round 3" in template
    assert result["callback"] not in template


def test_explicit_busy_origin_terminal_is_bound_and_waited_for(
    tmp_path: Path,
) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        writable=False,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0047",
            origin_terminal="term-origin",
            origin_session_id="ses_BUSYORIGIN123",
        )

    assert opened["origin_terminal"] == "term-origin"


def test_explicit_disconnected_origin_terminal_is_persisted_for_rebind(
    tmp_path: Path,
) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        connected=False,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0047",
            origin_terminal="term-origin",
            origin_session_id="ses_DISCONNECTED123",
        )

    assert opened["origin_terminal"] == "term-origin"
    assert opened["origin_terminal_source"] == "explicit-disconnected"


def test_unbounded_callback_round_never_requests_loop_stop() -> None:
    action = _default_next_action(
        {
            "round": 500,
            "max_rounds": 0,
            "conversation": {
                "page_id": "page-123",
                "url": "https://chatgpt.com/c/chat-12345678",
            },
        }
    )
    assert "do not auto-delegate another round" not in action
    assert "next highest-priority bounded task" in action


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
    payload["result"]["terminals"].extend(
        [
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
        ]
    )
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
    payload["result"]["terminals"].append(
        {
            "handle": "term-b",
            "worktreePath": str(repository),
            "connected": True,
            "writable": True,
            "title": "OpenCode",
            "preview": "Build · model",
            "lastOutputAt": 100,
        }
    )
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
        json.dumps(
            {
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
            }
        ),
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
    stdout = json.dumps(
        [
            {"id": "ses_OLD123", "directory": str(repository), "updated": 1},
            {"id": "ses_NEW123", "directory": str(repository), "updated": 999},
        ]
    )
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
    assert "RECEIPT_ACTION: sin-orca web-callback-ack" in message
    assert "--delivery-id gptwcd_" in message
    assert opened["callback"] not in message
    assert "Process this delivery ID at most once" in message
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


def test_receipt_acknowledgement_is_idempotent(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    sent: list[list[str]] = []
    with patch(
        "sin_orca.web_callbacks.run_orca", side_effect=orca_router(payload, sent)
    ):
        opened = open_callback(
            repository=repository,
            task_id="T-0021",
            origin_terminal="term-origin",
            origin_session_id="ses_ACK123",
        )
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="delivery ready for receipt",
        )

    delivery_id = callback_status(repository=repository, token=opened["callback"])[
        "delivery_id"
    ]
    first = acknowledge_callback(
        repository=repository, token=opened["callback"], delivery_id=delivery_id
    )
    second = acknowledge_callback(
        repository=repository, token=opened["callback"], delivery_id=delivery_id
    )

    assert first["status"] == "callback-acknowledged"
    assert first["reused"] is False
    assert second["reused"] is True
    state = callback_status(repository=repository, token=opened["callback"])
    assert state["status"] == "acknowledged"
    assert state["receipt_at"]


def test_callback_survives_terminal_restart_and_rebinds_session(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    replacement_payload = terminal_payload(
        repository,
        handle="term-restarted",
        title="OpenCode",
        preview="Build · model",
    )
    sent: list[list[str]] = []
    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0030",
            origin_terminal="term-origin",
            origin_session_id="ses_REBOUND123",
        )
    with (
        patch(
            "sin_orca.web_callbacks.run_orca",
            side_effect=orca_router(replacement_payload, sent),
        ),
        patch(
            "sin_orca.web_callbacks.resolve_session_from_orca_state",
            return_value={"id": "ses_REBOUND123"},
        ),
    ):
        result = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="completed after terminal restart",
        )

    assert result["status"] == "callback-sent"
    assert result["origin_terminal"] == "term-restarted"
    assert len(sent) == 1
    assert (
        callback_status(repository=repository, token=opened["callback"])["status"]
        == "sent"
    )


def test_callback_without_terminal_stays_pending_and_is_relayable(
    tmp_path: Path,
) -> None:
    repository = git_repository(tmp_path / "repo")
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    replacement_payload = terminal_payload(
        repository,
        handle="term-restarted",
        title="OpenCode",
        preview="Build · model",
    )
    sent: list[list[str]] = []
    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0031",
            origin_terminal="term-origin",
            origin_session_id="ses_RELAY123",
        )
    with patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload):
        pending = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="durably queued before delivery",
        )
    assert pending["status"] == "callback-pending"
    persisted = callback_status(repository=repository, token=opened["callback"])
    assert persisted["status"] == "pending-delivery"
    assert persisted["callback_status"] == "done"
    path = callback_path(repository, opened["callback"])
    legacy_record = json.loads(path.read_text(encoding="utf-8"))
    legacy_record.pop("delivery_id")
    legacy_record.pop("delivery_state")
    path.write_text(json.dumps(legacy_record), encoding="utf-8")
    with (
        patch(
            "sin_orca.web_callbacks.run_orca",
            side_effect=orca_router(replacement_payload, sent),
        ),
        patch(
            "sin_orca.web_callbacks.resolve_session_from_orca_state",
            return_value={"id": "ses_RELAY123"},
        ),
    ):
        relayed = relay_callback(repository=repository, token=opened["callback"])
    assert relayed["status"] == "callback-sent"
    assert len(sent) == 1
    assert "delivery=gptwcd_" in sent[0][sent[0].index("--text") + 1]


def test_busy_terminal_keeps_callback_pending_until_tui_is_idle(
    tmp_path: Path,
) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    sent: list[list[str]] = []

    def busy_router(arguments: list[str], *, timeout: int = 180):
        del timeout
        if arguments[:2] == ["terminal", "list"]:
            return payload
        if arguments[:2] == ["terminal", "wait"]:
            raise RuntimeError("terminal did not become idle before timeout")
        if arguments[:2] == ["terminal", "send"]:
            sent.append(arguments)
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected Orca call: {arguments}")

    with patch("sin_orca.web_callbacks.run_orca", side_effect=busy_router):
        opened = open_callback(
            repository=repository,
            task_id="T-0047",
            origin_terminal="term-origin",
            origin_session_id="ses_BUSY123",
        )
        result = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="wait for the active OpenCode turn",
        )

    assert result["status"] == "callback-pending"
    assert result["delivery_reason"] == "terminal-not-idle"
    assert sent == []
    assert callback_status(repository=repository, token=opened["callback"])[
        "status"
    ] == ("pending-delivery")


def test_ambiguous_rebind_stays_pending_without_delivery(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    ambiguous_payload = terminal_payload(
        repository,
        {"handle": "term-a", "title": "OpenCode", "preview": "Build · model"},
        {"handle": "term-b", "title": "OpenCode", "preview": "Build · model"},
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0032",
            origin_terminal="term-origin",
            origin_session_id="ses_AMBIG123",
        )
    with (
        patch("sin_orca.web_callbacks.run_orca", return_value=ambiguous_payload),
        patch(
            "sin_orca.web_callbacks.resolve_session_from_orca_state",
            return_value={"id": "ses_AMBIG123"},
        ),
    ):
        result = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="do not guess a restarted session",
        )

    assert result["status"] == "callback-pending"
    assert result["delivery_reason"] == "origin-terminal-gone-and-session-ambiguous"
    assert (
        callback_status(repository=repository, token=opened["callback"])["status"]
        == "pending-delivery"
    )


def test_rebind_requires_a_state_bound_opencode_pane(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    replacement_payload = terminal_payload(
        repository,
        {"handle": "term-shell", "title": "zsh", "preview": "shell prompt"},
        {"handle": "term-opencode", "title": "OpenCode", "preview": "Build · model"},
    )
    sent: list[list[str]] = []
    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0033",
            origin_terminal="term-origin",
            origin_session_id="ses_PANE123",
        )
    with (
        patch(
            "sin_orca.web_callbacks.run_orca",
            side_effect=orca_router(replacement_payload, sent),
        ),
        patch(
            "sin_orca.web_callbacks.resolve_origin_session",
            side_effect=AssertionError(
                "environment and repository session fallbacks are forbidden"
            ),
        ),
        patch(
            "sin_orca.web_callbacks.resolve_session_from_orca_state",
            side_effect=lambda item: (
                {"id": "ses_PANE123"} if item["handle"] == "term-shell" else None
            ),
        ),
    ):
        result = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="do not rebind a matching shell",
        )

    assert result["status"] == "callback-pending"
    assert result["delivery_reason"] == "origin-terminal-gone-and-session-offline"
    assert sent == []


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

    with pytest.raises(ValueError, match="canonical"):
        bind_callback(
            repository=repository,
            token=opened["callback"],
            page_id="page-123",
            conversation_url="https://chatgpt.com/",
        )

    with pytest.raises(ValueError, match="synthetic WEB aliases"):
        bind_callback(
            repository=repository,
            token=opened["callback"],
            page_id="page-123",
            conversation_url=(
                "https://chatgpt.com/c/WEB:e4b50e11-47b2-40d7-beec-2cac6981ecdf"
            ),
        )


def test_resolve_callback_token_by_exact_task_and_round(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        first = open_callback(
            repository=repository,
            task_id="T-0025",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            round_number=1,
            max_rounds=2,
        )
        second = open_callback(
            repository=repository,
            task_id="T-0025",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            round_number=2,
            max_rounds=2,
        )

    assert (
        resolve_callback_token(repository, task_id="T-0025", round_number=1)
        == first["callback"]
    )
    assert (
        resolve_callback_token(repository, task_id="T-0025", round_number=2)
        == second["callback"]
    )


def test_resolve_callback_token_prefers_unique_open_retry(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        cancelled = open_callback(
            repository=repository,
            task_id="T-0025",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            round_number=2,
            max_rounds=2,
        )
        cancel_callback(
            repository=repository,
            token=cancelled["callback"],
            reason="delegation UI unavailable before submission",
        )
        retry = open_callback(
            repository=repository,
            task_id="T-0025",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            round_number=2,
            max_rounds=2,
        )

    assert (
        resolve_callback_token(repository, task_id="T-0025", round_number=2)
        == retry["callback"]
    )


def test_resolve_callback_token_ignores_expired_open_retry(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        expired = open_callback(
            repository=repository,
            task_id="T-0025",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            round_number=2,
            max_rounds=2,
        )
        expired_path = callback_path(repository, expired["callback"])
        expired_record = json.loads(expired_path.read_text(encoding="utf-8"))
        expired_record["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        expired_path.write_text(
            json.dumps(expired_record, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        retry = open_callback(
            repository=repository,
            task_id="T-0025",
            origin_terminal="term-origin",
            origin_session_id="ses_ABC123",
            round_number=2,
            max_rounds=2,
        )

    assert (
        resolve_callback_token(repository, task_id="T-0025", round_number=2)
        == retry["callback"]
    )


def test_resolve_callback_token_refuses_ambiguity(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )
    with patch("sin_orca.web_callbacks.run_orca", return_value=payload):
        for _ in range(2):
            open_callback(
                repository=repository,
                task_id="T-0025",
                origin_terminal="term-origin",
                origin_session_id="ses_ABC123",
                round_number=1,
                max_rounds=2,
            )

    with pytest.raises(RuntimeError, match="ambiguous"):
        resolve_callback_token(repository, task_id="T-0025", round_number=1)


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
    assert (
        callback_status(
            repository=repository,
            token=opened["callback"],
        )["status"]
        == "open"
    )


def test_delivery_failure_stays_pending_for_relay(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
        lastOutputAt=100,
    )

    sends: list[list[str]] = []

    def failing_orca(arguments: list[str], *, timeout: int = 180):
        del timeout
        if arguments[:2] == ["terminal", "list"]:
            return payload
        if arguments[:2] == ["terminal", "wait"]:
            return {"ok": True, "result": {"status": "tui-idle"}}
        if arguments[:2] == ["terminal", "send"]:
            sends.append(arguments)
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
        result = send_callback(
            repository=repository,
            token=token,
            final_status="done",
            summary="implementation complete",
        )
        retried = relay_callback(repository=repository, token=token)

    state = callback_status(repository=repository, token=token)
    assert result["status"] == "callback-delivery-indeterminate"
    assert state["status"] == "delivery-indeterminate"
    assert state["dispatch_started_at"]
    assert "outcome unknown" in state["delivery_error"]
    assert state["delivery_id"]
    assert retried["status"] == "callback-awaiting-receipt"
    assert len(sends) == 1


def test_optional_relay_is_bounded_and_keeps_tokens_and_results_out_of_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    launched: list[list[str]] = []

    def launchctl(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        launched.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0040",
            origin_terminal="term-origin",
            origin_session_id="ses_RETRY123",
        )
    with (
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch("sin_orca.web_callbacks._launchctl_process", side_effect=launchctl),
    ):
        pending = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="sensitive callback result must not reach scheduler metadata",
            relay_fallback=True,
            relay_interval_seconds=30,
            relay_max_attempts=2,
        )
        reused = install_callback_relay(repository=repository, token=opened["callback"])
        first_retry = relay_callback(
            repository=repository, token=opened["callback"], scheduled=True
        )
        second_retry = relay_callback(
            repository=repository, token=opened["callback"], scheduled=True
        )

    assert pending["status"] == "callback-pending"
    assert pending["relay_fallback"]["status"] == "callback-relay-installed"
    assert reused["reused"] is True
    assert first_retry["status"] == "callback-pending"
    assert second_retry["status"] == "callback-pending"
    plist = next(launch_agents.glob("*.plist"), None)
    assert plist is None
    # Capture the installed plist before the retry budget removes it from disk.
    bootstrap = next(arguments for arguments in launched if arguments[1] == "bootstrap")
    assert bootstrap[0] == "/usr/bin/launchctl"
    state = callback_status(repository=repository, token=opened["callback"])
    assert state["status"] == "pending-delivery"
    assert state["relay_fallback"]["status"] == "inert"
    assert state["relay_fallback"]["deactivate_reason"] == "retry-budget-exhausted"
    assert any(arguments[1] == "bootout" for arguments in launched)


def test_relay_plist_has_no_callback_token_or_result_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []

    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0041",
            origin_terminal="term-origin",
            origin_session_id="ses_PLIST123",
        )
    with (
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch(
            "sin_orca.web_callbacks._launchctl_process",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
    ):
        summary = "private summary not for launchd"
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary=summary,
            relay_fallback=True,
        )

    plist_path = next(launch_agents.glob("*.plist"))
    plist_text = plist_path.read_text(encoding="utf-8")
    plist = plistlib.loads(plist_path.read_bytes())
    assert opened["callback"] not in plist_text
    assert summary not in plist_text
    assert "--relay-id" in plist["ProgramArguments"]
    assert "--scheduled" in plist["ProgramArguments"]
    assert plist["StandardOutPath"] == "/dev/null"
    assert plist["StandardErrorPath"] == "/dev/null"


def test_duplicate_task_round_relays_have_distinct_exact_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        callbacks = [
            open_callback(
                repository=repository,
                task_id="T-0041",
                origin_terminal="term-origin",
                origin_session_id="ses_COLLIDE123",
            )
            for _ in range(2)
        ]
    with (
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch(
            "sin_orca.web_callbacks._launchctl_process",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
    ):
        for callback in callbacks:
            send_callback(
                repository=repository,
                token=callback["callback"],
                final_status="done",
                summary="same task and round must not share a relay",
                relay_fallback=True,
            )

    records = [
        callback_status(repository=repository, token=item["callback"])
        for item in callbacks
    ]
    relay_ids = [
        json.loads(callback_path(repository, item["callback"]).read_text())["relay_id"]
        for item in callbacks
    ]
    plists = list(launch_agents.glob("*.plist"))
    assert len(plists) == 2
    assert len({record["relay_fallback"]["label"] for record in records}) == 2
    assert len(set(relay_ids)) == 2
    assert [
        resolve_callback_token_for_relay(repository, relay_id=relay_id)
        for relay_id in relay_ids
    ] == [item["callback"] for item in callbacks]
    with patch(
        "sin_orca.cli.relay_callback",
        return_value={"ok": True, "status": "callback-dry-run"},
    ) as relay:
        assert (
            sin_orca_cli._cmd_web_callback_relay(
                Namespace(
                    callback=None,
                    repo=str(repository),
                    relay_id=relay_ids[0],
                    dry_run=True,
                    scheduled=True,
                )
            )
            == 0
        )
    relay.assert_called_once_with(
        repository=str(repository),
        token=callbacks[0]["callback"],
        dry_run=True,
        scheduled=True,
    )
    for plist_path, callback in zip(sorted(plists), callbacks, strict=True):
        plist_text = plist_path.read_text(encoding="utf-8")
        assert callback["callback"] not in plist_text
        assert "--relay-id" in plist_text


def test_receipt_removes_optional_relay_without_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    online_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    sent: list[list[str]] = []
    launched: list[list[str]] = []

    def launchctl(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        launched.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0042",
            origin_terminal="term-origin",
            origin_session_id="ses_RECEIPT123",
        )
    with (
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch("sin_orca.web_callbacks._launchctl_process", side_effect=launchctl),
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
    ):
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="wait for TUI receipt",
            relay_fallback=True,
        )
        with patch(
            "sin_orca.web_callbacks.run_orca",
            side_effect=orca_router(online_payload, sent),
        ):
            relayed = relay_callback(repository=repository, token=opened["callback"])
        awaiting_receipt = callback_status(
            repository=repository, token=opened["callback"]
        )
        acknowledged = acknowledge_callback(
            repository=repository,
            token=opened["callback"],
            delivery_id=callback_status(
                repository=repository, token=opened["callback"]
            )["delivery_id"],
        )

    assert relayed["status"] == "callback-sent"
    assert awaiting_receipt["relay_fallback"]["status"] == "installed"
    assert acknowledged["status"] == "callback-acknowledged"
    assert len(sent) == 1
    assert any(arguments[1] == "bootout" for arguments in launched)
    state = callback_status(repository=repository, token=opened["callback"])
    assert state["relay_fallback"]["remove_reason"] == "receipt-acknowledged"


def test_scheduled_relay_expires_and_removes_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []

    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0043",
            origin_terminal="term-origin",
            origin_session_id="ses_EXPIRY123",
        )
    with (
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch(
            "sin_orca.web_callbacks._launchctl_process",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
    ):
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="failed",
            summary="bounded relay expires",
            relay_fallback=True,
        )
        path = callback_path(repository, opened["callback"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
        expired = relay_callback(
            repository=repository, token=opened["callback"], scheduled=True
        )

    assert expired["status"] == "callback-expired"
    state = callback_status(repository=repository, token=opened["callback"])
    assert state["status"] == "expired"
    assert state["relay_fallback"]["remove_reason"] == "callback-expired"


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


def test_pending_callback_expires_before_retry_delivery(tmp_path: Path) -> None:
    repository = git_repository(tmp_path / "repo")
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0044",
            origin_terminal="term-origin",
            origin_session_id="ses_PENDINGEXP123",
        )
    with patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload):
        pending = send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="pending until the origin returns",
        )
    assert pending["status"] == "callback-pending"

    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    path = callback_path(repository, opened["callback"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["expires_at"] = expiry.isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")

    with (
        patch(
            "sin_orca.web_callbacks.utc_now", return_value=expiry + timedelta(seconds=1)
        ),
        patch(
            "sin_orca.web_callbacks.run_orca",
            side_effect=AssertionError("expired callback must not attempt delivery"),
        ),
        pytest.raises(RuntimeError, match="expired"),
    ):
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="must not retry after expiry",
        )

    assert (
        callback_status(repository=repository, token=opened["callback"])["status"]
        == "expired"
    )


def test_scheduled_sent_callback_expires_and_removes_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    sent: list[list[str]] = []
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)

    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0045",
            origin_terminal="term-origin",
            origin_session_id="ses_SENTEXP123",
        )
    with (
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch(
            "sin_orca.web_callbacks._launchctl_process",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
    ):
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="scheduler owns the receipt window",
            relay_fallback=True,
        )
        with patch(
            "sin_orca.web_callbacks.run_orca",
            side_effect=orca_router(origin_payload, sent),
        ):
            relayed = relay_callback(
                repository=repository, token=opened["callback"], scheduled=True
            )
        assert relayed["status"] == "callback-sent"
        assert (
            callback_status(repository=repository, token=opened["callback"])[
                "relay_fallback"
            ]["status"]
            == "installed"
        )

        path = callback_path(repository, opened["callback"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["expires_at"] = expiry.isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
        with patch(
            "sin_orca.web_callbacks.utc_now", return_value=expiry + timedelta(seconds=1)
        ):
            expired = relay_callback(
                repository=repository, token=opened["callback"], scheduled=True
            )

    assert expired["status"] == "callback-expired"
    state = callback_status(repository=repository, token=opened["callback"])
    assert state["status"] == "expired"
    assert state["relay_fallback"]["remove_reason"] == "callback-expired"
    assert list(launch_agents.glob("*.plist")) == []


def test_indeterminate_callback_expires_before_recovery_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = git_repository(tmp_path / "repo")
    launch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SIN_ORCA_LAUNCH_AGENTS_DIR", str(launch_agents))
    origin_payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    offline_payload = terminal_payload(repository)
    offline_payload["result"]["terminals"] = []
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def indeterminate_send(arguments: list[str], *, timeout: int = 180):
        del timeout
        if arguments[:2] == ["terminal", "list"]:
            return origin_payload
        if arguments[:2] == ["terminal", "wait"]:
            return {"ok": True, "result": {"status": "tui-idle"}}
        if arguments[:2] == ["terminal", "send"]:
            raise RuntimeError("transport outcome unknown")
        raise AssertionError(f"unexpected Orca call: {arguments}")

    with patch("sin_orca.web_callbacks.run_orca", return_value=origin_payload):
        opened = open_callback(
            repository=repository,
            task_id="T-0046",
            origin_terminal="term-origin",
            origin_session_id="ses_INDETERMINATEEXP123",
        )
    with (
        patch(
            "sin_orca.web_callbacks.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ),
        patch(
            "sin_orca.web_callbacks._launchctl_process",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        patch("sin_orca.web_callbacks.run_orca", return_value=offline_payload),
    ):
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="do not retry an uncertain delivery",
            relay_fallback=True,
            relay_max_attempts=1,
        )
        with patch("sin_orca.web_callbacks.run_orca", side_effect=indeterminate_send):
            relayed = relay_callback(
                repository=repository, token=opened["callback"], scheduled=True
            )
        assert relayed["status"] == "callback-delivery-indeterminate"
        state = callback_status(repository=repository, token=opened["callback"])
        assert state["relay_fallback"]["status"] == "installed"

        path = callback_path(repository, opened["callback"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["expires_at"] = expiry.isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
        with (
            patch(
                "sin_orca.web_callbacks.utc_now",
                return_value=expiry + timedelta(seconds=1),
            ),
            pytest.raises(RuntimeError, match="expired"),
        ):
            send_callback(
                repository=repository,
                token=opened["callback"],
                final_status="done",
                summary="must not recover after expiry",
            )

    state = callback_status(repository=repository, token=opened["callback"])
    assert state["status"] == "expired"
    assert state["relay_fallback"]["remove_reason"] == "callback-expired"


def test_expired_receipt_is_rejected_and_marks_sent_callback_expired(
    tmp_path: Path,
) -> None:
    repository = git_repository(tmp_path / "repo")
    payload = terminal_payload(
        repository,
        handle="term-origin",
        title="OpenCode",
        preview="Build · model",
    )
    sent: list[list[str]] = []
    with patch(
        "sin_orca.web_callbacks.run_orca", side_effect=orca_router(payload, sent)
    ):
        opened = open_callback(
            repository=repository,
            task_id="T-0047",
            origin_terminal="term-origin",
            origin_session_id="ses_RECEIPTEXP123",
        )
        send_callback(
            repository=repository,
            token=opened["callback"],
            final_status="done",
            summary="receipt is valid only before expiry",
        )

    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    path = callback_path(repository, opened["callback"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["expires_at"] = expiry.isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")

    with (
        patch(
            "sin_orca.web_callbacks.utc_now", return_value=expiry + timedelta(seconds=1)
        ),
        pytest.raises(RuntimeError, match="expired"),
    ):
        acknowledge_callback(
            repository=repository,
            token=opened["callback"],
            delivery_id=record["delivery_id"],
        )

    assert sent
    assert (
        callback_status(repository=repository, token=opened["callback"])["status"]
        == "expired"
    )


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
