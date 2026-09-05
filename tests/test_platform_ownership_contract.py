from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WOW = ROOT.parent / "wow-my-zsh"


def test_sst_owns_memory_control_plane_not_fleet_deployment():
    readme = (ROOT / "README.md").read_text()
    agents = (ROOT / "AGENTS.md").read_text()
    control = (ROOT / "docs/MEMORY_CONTROL_PLANE.md").read_text()
    assert "Context-/Memory-Control-Plane" in readme
    assert "wow-my-zsh" in agents
    assert "wow-my-zsh" in control
    assert (ROOT / "lib/sin_memory_gateway.py").is_file()
    assert (ROOT / "bin/sin-memory-write").is_file()
    assert (ROOT / "bin/sin-context").is_file()
    assert not (ROOT / "docs/diagrams/openviking-fleet.architecture.json").exists()
    assert not (ROOT / "docs/diagrams/deployment-topology.architecture.json").exists()


def test_wow_owns_fleet_artifacts_when_sibling_checkout_exists():
    if not WOW.is_dir():
        return
    assert (WOW / "docs/MEMORY-PLATFORM.md").is_file()
    assert (WOW / "docs/INFERENCE-PLATFORM.md").is_file()
    assert (WOW / "docs/diagrams/openviking-fleet.architecture.json").is_file()
    assert (WOW / "docs/diagrams/deployment-topology.architecture.json").is_file()
    assert not (WOW / "infra/memory-gateway/sin_memory_gateway.py").exists()
