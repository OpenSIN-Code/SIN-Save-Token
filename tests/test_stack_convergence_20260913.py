import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    return json.loads((ROOT / "config" / name).read_text())

def routes():
    policy = load("context-policy.json")
    return {item["name"]: item for item in policy["routes"]}

def test_canonical_context_routes_have_single_owners():
    r = routes()
    assert r["code_symbol"]["providers"] == ["gitnexus"]
    assert r["code_architecture"]["providers"] == ["gitnexus"]
    assert r["code_orientation"]["providers"] == ["graft"]
    assert r["mixed_corpus_graph"]["providers"] == ["graphify"]
    assert r["domain_memory"]["providers"] == ["openviking"]
    assert r["session_resume"]["providers"] == ["session-digest"]
    assert r["text_search"]["providers"] == ["agent-grep"]

def test_active_runtime_excludes_retired_providers_and_includes_graft():
    providers = load("provider-runtime.json")["providers"]
    assert "graft" in providers
    for retired in ("simone", "cognee", "tencent-memory"):
        assert retired not in providers

def test_optimizer_stack_excludes_retired_tools():
    stack = load("token-optimizer-stack.json")
    assert "ponytail" in stack["upstreams"]
    assert "pxpipe" in stack["upstreams"]
    assert "caveman" not in stack["upstreams"]
    assert "gigatoken" not in stack["upstreams"]

def test_intelligence_registry_matches_canonical_roles():
    p = load("intelligence-providers.json")["providers"]
    assert p["symbol_navigation"]["primary"] == "gitnexus"
    assert "fallback" not in p["symbol_navigation"]
    assert p["general_code_graph"]["primary"] == "gitnexus"
    assert "fallback" not in p["general_code_graph"]
    assert p["code_orientation"]["primary"] == "graft"
    assert p["project_memory"]["primary"] == "openviking"
