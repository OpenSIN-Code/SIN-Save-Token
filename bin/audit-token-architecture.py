#!/usr/bin/env python3
"""Static cross-repository audit for the token-minimal architecture contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SST_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WOW = Path.home() / "dev" / "wow-my-zsh"
DEFAULT_GLOBAL = Path.home() / "dev" / "global-brain"


class Audit:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.passes: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        if condition:
            self.passes.append(message)
        else:
            self.errors.append(message)

    def json_file(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self.errors.append(f"cannot read JSON {path}: {error}")
            return {}
        if not isinstance(value, dict):
            self.errors.append(f"JSON root must be object: {path}")
            return {}
        return value

    def text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            self.errors.append(f"cannot read text {path}: {error}")
            return ""


def audit_sst(audit: Audit, root: Path) -> None:
    policy = audit.json_file(root / "config" / "context-policy.json")
    runtime = audit.json_file(root / "config" / "provider-runtime.json")
    routes = policy.get("routes", [])
    retrieval = policy.get("retrieval", {})
    budgets = policy.get("budgets", {})

    route_by_name = {
        route.get("name"): route
        for route in routes
        if isinstance(route, dict)
    }
    symbol = route_by_name.get("code_symbol", {})
    architecture = route_by_name.get("code_architecture", {})
    audit.require(
        symbol.get("providers") == ["simone", "graphify"],
        "symbol routing is Simone -> Graphify",
    )
    audit.require(
        architecture.get("providers") == ["graphify", "sin-code"],
        "architecture routing is Graphify -> sin-code",
    )
    audit.require(
        int(retrieval.get("maximum_provider_attempts", 99)) <= 2,
        "provider attempts are capped at two",
    )
    audit.require(
        int(budgets.get("maximum_tokens", 999999)) <= 1600,
        "context maximum is at most 1600 tokens",
    )
    routed = {
        provider
        for route in routes
        if isinstance(route, dict)
        for provider in route.get("providers", [])
    }
    configured = set(runtime.get("providers", {}))
    audit.require(
        routed <= configured,
        "every routed provider has a runtime specification",
    )

    broker = audit.text_file(root / "bin" / "sin-context")
    audit.require(
        "ProviderRuntime" in broker and "runtime.call" in broker,
        "sin-context uses the persistent provider runtime",
    )
    audit.require(
        "outcome.cache_negative" in broker,
        "negative caching is gated by provider outcome semantics",
    )

    sync = audit.text_file(root / "bin" / "brain-sync.py")
    audit.require(
        '"direction": "gbrain -> cognee"' in sync,
        "brain sync direction is gbrain -> Cognee",
    )
    audit.require(
        '"automatic_reverse_sync": False' in sync,
        "automatic reverse brain sync is disabled",
    )
    audit.require(
        "cognee2gbrain" not in sync
        and "def reverse_sync" not in sync
        and "add_parser(\"reverse\")" not in sync,
        "brain-sync implementation exposes no reverse command",
    )


def audit_wow(audit: Audit, root: Path) -> None:
    registry = audit.json_file(root / "shared" / "mcp" / "servers.json")
    profiles = audit.json_file(root / "shared" / "mcp" / "task-profiles.json")
    servers = registry.get("servers", {})
    budget = registry.get("_budget", {})

    audit.require(
        budget.get("default_profile") == "minimal",
        "default MCP profile is minimal",
    )
    audit.require(
        servers.get("cognee", {}).get("url") == "http://127.0.0.1:8011/mcp",
        "Cognee MCP port is 8011",
    )
    audit.require(
        not any(
            isinstance(spec, dict) and spec.get("always_on") is True
            for spec in servers.values()
        ),
        "current core profile has zero always-on MCP servers",
    )

    profile_values = profiles.get("profiles", {})
    maximums = [
        spec.get("maximum_servers", 999)
        for spec in profile_values.values()
        if isinstance(spec, dict)
    ]
    audit.require(
        profile_values.get("minimal", {}).get("maximum_servers") == 0,
        "minimal task profile has zero servers",
    )
    audit.require(
        bool(maximums) and max(maximums) <= 2,
        "all task profiles cap MCP servers at two",
    )

    install = audit.text_file(root / "install.sh")
    doctor = audit.text_file(root / "doctor.sh")
    house_rules = audit.text_file(root / "shared" / "AGENTS.md")
    audit.require(
        'PROFILE="${WOW_MCP_PROFILE:-minimal}"' in install,
        "wow installer defaults to minimal",
    )
    audit.require(
        'PROFILE="${WOW_MCP_PROFILE:-minimal}"' in doctor,
        "wow doctor defaults to minimal",
    )
    audit.require(
        "worktree shadowing guard" in doctor,
        "doctor checks transient worktree shadowing",
    )
    audit.require(
        "exactly **one** cheap worker" in house_rules,
        "house rules default to one cheap worker",
    )
    audit.require(
        "5-10" not in house_rules,
        "house rules contain no 5-10 worker mandate",
    )


def audit_global_brain(audit: Audit, root: Path) -> None:
    config = audit.json_file(root / ".opencode" / "pcpm-config.json")
    pcpm = config.get("pcpm", {})
    before = audit.text_file(root / ".opencode" / "hooks" / "pcpm-before-run.sh")
    after = audit.text_file(root / ".opencode" / "hooks" / "pcpm-after-run.sh")
    engine = audit.text_file(root / "src" / "engines" / "hook-engine.js")
    agents = audit.text_file(root / "AGENTS.md")

    audit.require(
        pcpm.get("autoInjectContext") is False,
        "global-brain automatic context injection is disabled",
    )
    audit.require(
        pcpm.get("autoSync") is False,
        "global-brain automatic archive sync is disabled",
    )
    audit.require(
        pcpm.get("extractKnowledge") is False,
        "global-brain automatic extraction is disabled",
    )
    audit.require(
        pcpm.get("canonicalMemoryProvider") == "cognee",
        "global-brain declares Cognee canonical",
    )
    audit.require(
        pcpm.get("role") == "archive-and-plan-store",
        "global-brain is limited to archive-and-plan-store role",
    )
    audit.require(
        "PCPM_AUTO_INJECT" in before and "PCPM_AUTO_INJECT" in engine,
        "beforeRun requires explicit injection opt-in",
    )
    audit.require(
        "PCPM_AUTO_EXTRACT" in after and "PCPM_AUTO_SYNC" in after,
        "afterRun extraction and sync are separate opt-ins",
    )
    audit.require(
        "sync-chat-turn" not in after,
        "live afterRun hook has no chat-turn memory loop",
    )
    audit.require(
        "PCPM_EXPORT_TO_CLAUDE_MEM" not in after
        and "brain-to-claude-mem" not in after,
        "live afterRun hook has no duplicate-memory export path",
    )
    audit.require(
        "5-10 parallele" not in agents and "5–10" not in agents,
        "global-brain rules contain no mass-explorer mandate",
    )


def audit_forbidden_drift(audit: Audit, files: list[Path]) -> None:
    forbidden = {
        "http://127.0.0.1:8001/mcp": "obsolete Cognee port 8001",
        "cognee2gbrain": "obsolete reverse-sync command",
        "gbrain2cognee": "obsolete brain-sync command name",
        '"autoSync": true': "automatic global-brain sync",
        "sync-chat-turn": "automatic chat-turn memory loop",
        "single source of truth for all AI coding agent knowledge": "global-brain canonical-memory ownership drift",
        "This repo — the single source of truth": "global-brain canonical-memory ownership drift",
        "5-10 parallele explore": "mass explorer mandate",
    }
    for path in files:
        text = audit.text_file(path)
        for pattern, label in forbidden.items():
            audit.require(pattern not in text, f"{path.name} has no {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sst", type=Path, default=SST_ROOT)
    parser.add_argument(
        "--wow",
        type=Path,
        default=Path(os.environ.get("WOW_HOME", DEFAULT_WOW)),
    )
    parser.add_argument(
        "--global-brain",
        type=Path,
        default=Path(os.environ.get("GLOBAL_BRAIN_HOME", DEFAULT_GLOBAL)),
    )
    args = parser.parse_args()

    sst = args.sst.resolve()
    wow = args.wow.resolve()
    global_brain = args.global_brain.resolve()
    audit = Audit()

    audit_sst(audit, sst)
    audit_wow(audit, wow)
    audit_global_brain(audit, global_brain)
    audit_forbidden_drift(
        audit,
        [
            sst / "README.md",
            sst / "docs" / "ECOSYSTEM.md",
            wow / "README.md",
            wow / "docs" / "ECOSYSTEM.md",
            global_brain / "README.md",
            global_brain / "AGENTS.md",
            global_brain / ".opencode" / "PCPM-AGENTS.md",
        ],
    )

    for message in audit.passes:
        print(f"PASS {message}")
    for message in audit.errors:
        print(f"FAIL {message}", file=sys.stderr)

    if audit.errors:
        print(
            f"token architecture audit: FAIL ({len(audit.errors)} errors)",
            file=sys.stderr,
        )
        return 1
    print(f"token architecture audit: PASS ({len(audit.passes)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
