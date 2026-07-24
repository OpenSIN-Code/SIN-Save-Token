from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_orca.cli import _cmd_doctor  # noqa: E402


def test_doctor_reports_capabilities_and_default_mode(tmp_path: Path) -> None:
    command_paths = {
        "git": "/usr/bin/git",
        "orca": "/usr/local/bin/orca",
        "rtk": "/usr/local/bin/rtk",
        "graphify": None,
        "gitnexus": "/usr/local/bin/gitnexus",
        "sin": None,
    }
    output = io.StringIO()
    with (
        patch("sin_orca.cli.repository_root", return_value=tmp_path),
        patch("sin_orca.cli.state_root", return_value=tmp_path / "state"),
        patch("sin_orca.cli.repository_id", return_value="repo-id"),
        patch("sin_orca.cli.run_git", return_value="true"),
        patch("sin_orca.cli._probe_writable_directory", return_value=(True, None)),
        patch("sin_orca.cli.shutil.which", side_effect=command_paths.get),
        redirect_stdout(output),
    ):
        result = _cmd_doctor(argparse.Namespace(strict=False))

    payload = json.loads(output.getvalue())
    assert result == 0
    assert payload["status"] == "degraded"
    assert payload["ready_for_dispatch"] is True
    assert payload["default_approval_mode"] == "continuous-preauthorized"
    assert payload["capabilities"]["architecture_context"] is True
    assert "missing executable: sin" in payload["issues"]


def test_doctor_strict_fails_for_missing_optional_tools(tmp_path: Path) -> None:
    output = io.StringIO()
    with (
        patch("sin_orca.cli.repository_root", return_value=tmp_path),
        patch("sin_orca.cli.state_root", return_value=tmp_path / "state"),
        patch("sin_orca.cli.repository_id", return_value="repo-id"),
        patch("sin_orca.cli.run_git", return_value="true"),
        patch("sin_orca.cli._probe_writable_directory", return_value=(True, None)),
        patch(
            "sin_orca.cli.shutil.which",
            side_effect=lambda name: f"/bin/{name}" if name in {"git", "orca"} else None,
        ),
        redirect_stdout(output),
    ):
        result = _cmd_doctor(argparse.Namespace(strict=True))

    assert result == 1
    assert json.loads(output.getvalue())["status"] == "degraded"


def test_python_module_entrypoint_shows_help() -> None:
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "lib")}
    process = subprocess.run(
        [sys.executable, "-m", "sin_orca", "--help"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 0
    assert "SIN Orca Orchestrator" in process.stdout
    assert "continuous-preauthorized" not in process.stderr
