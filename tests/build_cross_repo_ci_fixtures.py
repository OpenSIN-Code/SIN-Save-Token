#!/usr/bin/env python3
"""Create minimal cross-repository fixtures for audit-token-architecture.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def write_text(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(root: Path, relative: str, content: dict) -> None:
    write_text(root, relative, json.dumps(content, indent=2) + "\n")


def build(root: Path) -> None:
    wow = root / "wow-my-zsh"
    global_brain = root / "global-brain"

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
    write_text(wow, "shared/AGENTS.md", "Use exactly **one** cheap worker by default.\n")

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
        "BASH_SOURCE=fixture\nPCPM_AUTO_INJECT=0\nmktemp\ntrap 'rm -f \"$CONTEXT_FILE\"' EXIT\nif ! node\nPCPM_CONTEXT_TEMPFILE_FAILED=true\nPCPM_CONTEXT_LOAD_FAILED=true\nMAX_CONTEXT_BYTES=6400\nPCPM_CONTEXT_LIMIT_EXCEEDED=true\n",
    )
    write_text(
        global_brain,
        ".opencode/hooks/pcpm-after-run.sh",
        "BASH_SOURCE=fixture\nPCPM_AUTO_EXTRACT=0\nPCPM_AUTO_SYNC=0\nif ! node\nif ! node\n>/dev/null 2>/dev/null\n>/dev/null 2>/dev/null\nPCPM_AUTO_EXTRACT_FAILED=true\nPCPM_AUTO_SYNC_FAILED=true\n",
    )
    write_text(
        global_brain,
        "src/engines/hook-engine.js",
        "function shellQuote(value) { return value; }\nfunction boundedDirectiveMetadata(value) { return value; }\nconst gate = 'PCPM_AUTO_INJECT';\nconst label = 'untrusted metadata, not agent instructions';\nshellQuote(projectId);\nshellQuote(goalDescription);\n",
    )
    write_text(global_brain, "src/cli.js", "const commands = [];\n")
    write_text(
        global_brain,
        "AGENTS.md",
        "DISCOVERY ON DEMAND. Default to one worker; use a second only for an independent question.\n",
    )

    for repository in (wow, global_brain):
        for name in ("README.md", "docs/ECOSYSTEM.md", ".opencode/PCPM-AGENTS.md"):
            path = repository / name
            if not path.exists():
                write_text(repository, name, "fixture\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    build(destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
