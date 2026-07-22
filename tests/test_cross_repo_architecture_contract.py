#!/usr/bin/env python3
"""Cross-repository audit contract using local, disposable fixtures."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "bin" / "audit-token-architecture.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("token_architecture_audit_contract", AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {AUDIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = load_audit_module()


def write_text(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(root: Path, relative: str, content: dict) -> None:
    write_text(root, relative, json.dumps(content, indent=2) + "\n")


class CrossRepositoryArchitectureContractTests(unittest.TestCase):
    def test_audit_accepts_explicit_fixture_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            wow = fixture / "wow-my-zsh"
            global_brain = fixture / "global-brain"

            write_json(
                wow,
                "shared/mcp/servers.json",
                {
                    "servers": {
                        "cognee": {
                            "url": "http://127.0.0.1:8011/mcp",
                            "always_on": False,
                        }
                    },
                    "_budget": {"default_profile": "minimal"},
                },
            )
            write_json(
                wow,
                "shared/mcp/task-profiles.json",
                {
                    "profiles": {
                        "minimal": {"maximum_servers": 0},
                        "coding": {"maximum_servers": 2},
                    }
                },
            )
            write_text(wow, "install.sh", 'PROFILE="${WOW_MCP_PROFILE:-minimal}"\n')
            write_text(
                wow,
                "doctor.sh",
                'PROFILE="${WOW_MCP_PROFILE:-minimal}"\n# worktree shadowing guard\n',
            )
            write_text(
                wow,
                "shared/AGENTS.md",
                "Use exactly **one** cheap worker by default.\n",
            )

            write_json(
                global_brain,
                ".opencode/pcpm-config.json",
                {
                    "pcpm": {
                        "autoInjectContext": False,
                        "autoSync": False,
                        "extractKnowledge": False,
                        "canonicalMemoryProvider": "cognee",
                        "role": "archive-and-plan-store",
                    }
                },
            )
            write_text(
                global_brain,
                ".opencode/hooks/pcpm-before-run.sh",
                "PCPM_AUTO_INJECT=0\n",
            )
            write_text(
                global_brain,
                ".opencode/hooks/pcpm-after-run.sh",
                "PCPM_AUTO_EXTRACT=0\nPCPM_AUTO_SYNC=0\n",
            )
            write_text(
                global_brain,
                "src/engines/hook-engine.js",
                "const gate = 'PCPM_AUTO_INJECT';\n",
            )
            write_text(
                global_brain,
                "AGENTS.md",
                "Default to one worker; use a second only for an independent question.\n",
            )

            audit = AUDIT.Audit()
            AUDIT.audit_sst(audit, ROOT)
            AUDIT.audit_wow(audit, wow)
            AUDIT.audit_global_brain(audit, global_brain)
            self.assertEqual(audit.errors, [], "\n".join(audit.errors))


if __name__ == "__main__":
    unittest.main()
