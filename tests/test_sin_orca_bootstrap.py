import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Exercise the legacy path regardless of the host's /usr/bin/python3 version.
LEGACY_LAUNCH = (
    "import runpy,sys; sys.version_info=(3,9,0); "
    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
)


def test_legacy_python_bootstraps_supported_interpreter(tmp_path):
    supported = sys.executable
    (tmp_path / "python3.12").symlink_to(supported)
    result = subprocess.run(
        [sys.executable, "-c", LEGACY_LAUNCH, str(ROOT / "bin/sin-orca"), "--help"],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "SIN Orca Orchestrator" in result.stdout


def test_legacy_python_without_replacement_has_actionable_error(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", LEGACY_LAUNCH, str(ROOT / "bin/sin-orca"), "--help"],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "Python 3.10+" in result.stderr
    assert "Traceback" not in result.stderr
