#!/usr/bin/env python3

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTEXT_GUARD = ROOT / "hooks" / "context-budget-guard.js"
RTK_REWRITE = ROOT / "hooks" / "rtk-auto-rewrite.js"


def run_hook(path: Path, payload: dict, *, env: dict[str, str] | None = None):
    return subprocess.run(
        ["node", str(path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
        env=env,
    )


def test_context_guard_allows_targeted_single_file_read() -> None:
    result = run_hook(
        CONTEXT_GUARD,
        {"tool_name": "Read", "tool_input": {"file_path": "/repo/README.md"}},
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_context_guard_does_not_treat_grep_pattern_as_shell_command() -> None:
    result = run_hook(
        CONTEXT_GUARD,
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "git log", "path": "src", "head_limit": 20},
        },
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_context_guard_allows_targeted_pytest_and_nudges_bare_pytest() -> None:
    targeted = run_hook(
        CONTEXT_GUARD,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/test_sin_cache.py -q"},
        },
    )
    assert targeted.returncode == 0
    assert targeted.stdout == ""

    broad = run_hook(
        CONTEXT_GUARD,
        {"tool_name": "Bash", "tool_input": {"command": "pytest"}},
    )
    payload = json.loads(broad.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "Broad context operation" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_context_guard_nudges_directory_read() -> None:
    result = run_hook(
        CONTEXT_GUARD,
        {"tool_name": "Read", "tool_input": {"file_path": "."}},
    )
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "Broad context operation" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def rtk_environment(tmp_path: Path) -> dict[str, str]:
    fake_rtk = tmp_path / "rtk"
    fake_rtk.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_rtk.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_rtk_rewrite_preserves_quoted_arguments(tmp_path: Path) -> None:
    command = 'git log --format="%h %s" -- README.md'
    result = run_hook(
        RTK_REWRITE,
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env=rtk_environment(tmp_path),
    )
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["updatedInput"]["command"] == (
        'rtk git log --format="%h %s" -- README.md'
    )


def test_rtk_rewrite_preserves_quotes_after_simple_environment_assignment(
    tmp_path: Path,
) -> None:
    command = 'FOO=1 git log --format="%h %s"'
    result = run_hook(
        RTK_REWRITE,
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env=rtk_environment(tmp_path),
    )
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["updatedInput"]["command"] == (
        'FOO=1 rtk git log --format="%h %s"'
    )


def test_rtk_rewrite_leaves_complex_environment_assignment_unchanged(
    tmp_path: Path,
) -> None:
    result = run_hook(
        RTK_REWRITE,
        {
            "tool_name": "Bash",
            "tool_input": {"command": 'FOO="x y" git status'},
        },
        env=rtk_environment(tmp_path),
    )
    assert result.returncode == 0
    assert result.stdout == ""
