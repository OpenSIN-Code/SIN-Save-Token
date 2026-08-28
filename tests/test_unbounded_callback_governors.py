from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from sin_orca import web_callbacks

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "lib/sin_orca/cli.py"


def test_callback_open_has_no_round_ceiling_api() -> None:
    assert "max_rounds" not in inspect.signature(web_callbacks.open_callback).parameters
    assert "max_rounds" not in inspect.getsource(web_callbacks.open_callback)


def test_callback_continuation_never_stops_on_legacy_round_budget() -> None:
    action = web_callbacks._default_next_action({"round": 999, "max_rounds": 1})
    lowered = action.casefold()
    assert "budget is exhausted" not in lowered
    assert "do not auto-delegate another round" not in lowered
    assert "continue" in lowered


def test_callback_relay_has_no_attempt_ceiling_api() -> None:
    assert "max_attempts" not in inspect.signature(web_callbacks.install_callback_relay).parameters
    assert "relay_max_attempts" not in inspect.signature(web_callbacks.send_callback).parameters
    relay_source = inspect.getsource(web_callbacks.relay_callback)
    assert "max_attempts" not in relay_source
    assert "retry-budget-exhausted" not in relay_source


def test_callback_cli_exposes_no_execution_ceiling_flags() -> None:
    source = CLI.read_text(encoding="utf-8")
    open_block = source[source.index('"web-callback-open"'):source.index('"web-callback-bind"')]
    send_block = source[source.index('"web-callback-send"'):source.index('"web-callback-status"')]
    relay_block = source[source.index('"web-callback-relay-install"'):source.index('"web-callback-relay-cancel"')]
    assert "--max-rounds" not in open_block
    assert "--relay-max-attempts" not in send_block
    assert "--max-attempts" not in relay_block


def test_tracked_sin_orca_sources_cannot_reintroduce_callback_execution_caps() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "lib/sin_orca/*.py"], cwd=ROOT, text=True
    ).splitlines()
    forbidden = ("max_rounds", "relay_max_attempts", "retry-budget-exhausted", "max_attempts")
    hits: list[str] = []
    for relative in tracked:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{relative}: {token}")
    assert hits == []
