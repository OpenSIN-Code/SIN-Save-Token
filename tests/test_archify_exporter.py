from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "scripts" / "export-archify-svg.mjs"
INPUT = ROOT / "docs" / "diagrams" / "memory-write.workflow.html"


def test_archify_exporter_emits_standalone_dual_theme_svg(tmp_path: Path) -> None:
    output = tmp_path / "memory-write.svg"
    result = subprocess.run(
        ["node", str(EXPORTER), str(INPUT), str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    svg = output.read_text(encoding="utf-8")
    assert svg.count("<svg") == 1
    assert "viewBox=" in svg
    assert "c-bg-rect" in svg
    assert "prefers-color-scheme" in svg
    assert "--bg" in svg
    assert "<html" not in svg.lower()
    assert "<body" not in svg.lower()
    assert "<button" not in svg.lower()
    assert all(line == line.rstrip() for line in svg.splitlines()), "exported SVG contains trailing whitespace"
