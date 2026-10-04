import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_python_bootstraps_supported_interpreter(tmp_path):
    supported = shutil.which("python3.12")
    assert supported
    (tmp_path / "python3.12").symlink_to(supported)
    result = subprocess.run(
        ["/usr/bin/python3", str(ROOT / "bin/sin-orca"), "--help"],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "SIN Orca Orchestrator" in result.stdout


def test_legacy_python_without_replacement_has_actionable_error(tmp_path):
    result = subprocess.run(
        ["/usr/bin/python3", str(ROOT / "bin/sin-orca"), "--help"],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "Python 3.10+" in result.stderr
    assert "Traceback" not in result.stderr
