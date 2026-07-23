#!/usr/bin/env python3
"""Run local OpenSIN integration gates and write an external evidence report.

The runner never mutates Git state. Dirty repositories fail by default because a
release result is not reproducible otherwise; `--allow-dirty` keeps the check
visible but non-blocking during active development. Live Orca agents are started
only with explicit `--live`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE = re.compile(
    r"(?i)(authorization|bearer|token|secret|password|passwd|api[-_]?key|cookie)"
)


@dataclass(frozen=True)
class CheckSpec:
    name: str
    cwd: Path
    argv: list[str]
    timeout: int = 1_200
    env: dict[str, str] | None = None


@dataclass
class CheckResult:
    name: str
    cwd: str
    argv: list[str]
    exit_code: int
    ok: bool
    stdout_tail: str
    stderr_tail: str


def safe_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    if extra:
        environment.update(extra)
    return environment


def redact(value: str) -> str:
    redacted: list[str] = []
    for line in value.splitlines():
        redacted.append(
            "<redacted sensitive line>" if SENSITIVE.search(line) else line
        )
    return "\n".join(redacted)


def bounded(value: str | bytes | None, limit: int = 8_000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = redact(value.strip())
    if len(value) <= limit:
        return value
    return "...[truncated]\n" + value[-limit:]


def persisted_argv(argv: list[str]) -> list[str]:
    if not argv:
        return []
    return [argv[0], f"<{max(0, len(argv) - 1)} arguments omitted>"]


def run_check(spec: CheckSpec) -> CheckResult:
    try:
        process = subprocess.run(
            spec.argv,
            cwd=spec.cwd,
            env=spec.env or safe_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=spec.timeout,
        )
        return CheckResult(
            name=spec.name,
            cwd=str(spec.cwd),
            argv=persisted_argv(spec.argv),
            exit_code=process.returncode,
            ok=process.returncode == 0,
            stdout_tail=bounded(process.stdout),
            stderr_tail=bounded(process.stderr),
        )
    except FileNotFoundError as error:
        return CheckResult(
            name=spec.name,
            cwd=str(spec.cwd),
            argv=persisted_argv(spec.argv),
            exit_code=127,
            ok=False,
            stdout_tail="",
            stderr_tail=bounded(str(error)),
        )
    except subprocess.TimeoutExpired as error:
        return CheckResult(
            name=spec.name,
            cwd=str(spec.cwd),
            argv=persisted_argv(spec.argv),
            exit_code=124,
            ok=False,
            stdout_tail=bounded(error.stdout),
            stderr_tail=bounded(error.stderr or "command timed out"),
        )


def repository_status(root: Path) -> dict[str, Any]:
    process = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        env=safe_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if process.returncode != 0:
        return {
            "ok": False,
            "dirty": None,
            "error": bounded(process.stderr or process.stdout),
            "entries": [],
        }
    entries = [line for line in process.stdout.splitlines() if line.strip()]
    return {
        "ok": True,
        "dirty": bool(entries),
        "entries": entries[:500],
        "truncated": len(entries) > 500,
    }


def compile_command() -> list[str]:
    code = """
import compileall
import py_compile
import sys
from pathlib import Path

if not compileall.compile_dir('lib', quiet=1):
    raise SystemExit(1)
if not compileall.compile_dir('tests', quiet=1):
    raise SystemExit(1)
if not compileall.compile_dir('scripts', quiet=1):
    raise SystemExit(1)
for relative in sys.argv[1:]:
    py_compile.compile(relative, doraise=True)
""".strip()
    return [
        sys.executable,
        "-c",
        code,
        "bin/sin-context",
        "bin/sin-orca",
        "bin/gitnexus-query",
        "bin/benchmark-context",
        "bin/audit-token-architecture.py",
        "scripts/verify-local-integration.py",
        "scripts/live-orca-smoke.py",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sst",
        type=Path,
        default=Path.home() / "dev" / "SIN-Save-Token",
    )
    parser.add_argument(
        "--wow",
        type=Path,
        default=Path.home() / "dev" / "wow-my-zsh",
    )
    parser.add_argument(
        "--simone",
        type=Path,
        default=Path.home() / "dev" / "Simone-MCP",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--deep-doctor", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the real same-worktree Orca callback/review smoke test",
    )
    parser.add_argument("--live-agent", default="mimo-code")
    parser.add_argument("--parent-terminal")
    parser.add_argument("--simone-task-id")
    args = parser.parse_args()

    sst = args.sst.expanduser().resolve()
    wow = args.wow.expanduser().resolve()
    simone = args.simone.expanduser().resolve()
    repositories = {"sst": sst, "wow": wow, "simone": simone}
    missing = [str(path) for path in repositories.values() if not path.is_dir()]
    if missing:
        print(json.dumps({"ok": False, "missing_repositories": missing}, indent=2))
        return 2

    statuses = {
        name: repository_status(root)
        for name, root in repositories.items()
    }
    dirty_blockers = [
        name
        for name, status in statuses.items()
        if status.get("ok") is not True
        or (status.get("dirty") is True and not args.allow_dirty)
    ]

    checks: list[CheckSpec] = [
        CheckSpec("sst: compile Python", sst, compile_command(), 300),
        CheckSpec(
            "sst: structural audit",
            sst,
            [sys.executable, "scripts/audit-python-structure.py", "."],
            300,
        ),
        CheckSpec(
            "sst: ruff critical",
            sst,
            ["ruff", "check", "--select", "E9,F63,F7,F82", "bin", "lib", "tests", "scripts"],
            300,
        ),
        CheckSpec(
            "sst: complete pytest",
            sst,
            [sys.executable, "-m", "pytest", "-q"],
            1_200,
        ),
        CheckSpec(
            "sst: same-worktree static contract",
            sst,
            [sys.executable, "tests/test_same_worktree_contract.py"],
            120,
        ),
        CheckSpec(
            "sst: GitNexus adapter",
            sst,
            [sys.executable, "bin/gitnexus-query", "provider routing architecture"],
            180,
        ),
        CheckSpec(
            "wow: shell syntax",
            wow,
            ["bash", "-n", "bin/sin-orca", "install.sh", "doctor.sh"],
            120,
        ),
        CheckSpec(
            "wow: registry validation",
            wow,
            [sys.executable, "scripts/validate-mcp-registry.py"],
            120,
        ),
        CheckSpec(
            "wow: Orca policy validation",
            wow,
            [sys.executable, "scripts/validate-orca-policy.py"],
            120,
        ),
        CheckSpec(
            "wow: Orca same-worktree team contract",
            wow,
            [sys.executable, "scripts/test-orca-sin-team-contract.py"],
            120,
        ),
        CheckSpec(
            "wow: generator tests",
            wow,
            [sys.executable, "scripts/test_gen_mcp.py"],
            300,
        ),
        CheckSpec(
            "wow: profile contracts",
            wow,
            [sys.executable, "scripts/test_mcp_profile_contract.py"],
            300,
        ),
        CheckSpec(
            "wow: RBAC contracts",
            wow,
            [sys.executable, "scripts/test_mcp_rbac_contract.py"],
            300,
        ),
        CheckSpec(
            "wow: worktree shadow contracts",
            wow,
            [sys.executable, "scripts/test_worktree_shadow_contract.py"],
            300,
        ),
        CheckSpec(
            "wow: sin-orca ownership",
            wow,
            [sys.executable, "scripts/test-sin-orca-ownership.py"],
            120,
        ),
        CheckSpec(
            "wow: Codex Orca gate",
            wow,
            ["node", "tests/test-codex-orca-gate.js"],
            300,
        ),
        CheckSpec(
            "wow: doctor",
            wow,
            ["bash", "doctor.sh"],
            300,
            safe_environment({
                "WOW_HOME": str(wow),
                "WOW_MCP_PROFILE": "minimal",
                "WOW_DOCTOR_DEEP": "1" if args.deep_doctor else "0",
            }),
        ),
        CheckSpec(
            "simone: compileall",
            simone,
            [sys.executable, "-m", "compileall", "-q", "src", "tests", "scripts"],
            300,
        ),
        CheckSpec(
            "simone: structural audit",
            simone,
            [sys.executable, "scripts/audit-python-structure.py", "src"],
            300,
        ),
        CheckSpec(
            "simone: ruff",
            simone,
            ["ruff", "check", "src", "tests", "scripts"],
            300,
        ),
        CheckSpec(
            "simone: pytest",
            simone,
            [sys.executable, "-m", "pytest", "tests", "-q"],
            1_200,
        ),
        CheckSpec(
            "simone: mypy",
            simone,
            ["mypy", "src", "--ignore-missing-imports"],
            600,
        ),
        CheckSpec("runtime: GitNexus version", sst, ["gitnexus", "--version"], 60),
        CheckSpec("runtime: Orca version", sst, ["orca", "--version"], 60),
        CheckSpec(
            "runtime: Orca terminal create help",
            sst,
            ["orca", "terminal", "create", "--help"],
            60,
        ),
        CheckSpec(
            "runtime: Orca terminal send help",
            sst,
            ["orca", "terminal", "send", "--help"],
            60,
        ),
        CheckSpec(
            "runtime: Orca terminal read help",
            sst,
            ["orca", "terminal", "read", "--help"],
            60,
        ),
    ]

    if args.live:
        live_argv = [
            sys.executable,
            "scripts/live-orca-smoke.py",
            "--sst",
            str(sst),
            "--agent",
            args.live_agent,
        ]
        if args.parent_terminal:
            live_argv.extend(["--parent-terminal", args.parent_terminal])
        if args.simone_task_id:
            live_argv.extend(["--simone-task-id", args.simone_task_id])
        checks.append(CheckSpec("live: same-worktree Orca E2E", sst, live_argv, 1_200))

    results: list[CheckResult] = []
    for spec in checks:
        result = run_check(spec)
        results.append(result)
        marker = "PASS" if result.ok else "FAIL"
        print(f"[{marker}] {result.name}")
        if not result.ok:
            detail = result.stderr_tail or result.stdout_tail
            if detail:
                print(detail)

    expected_runtime = (sst / "bin" / "sin-orca").resolve()
    installed_runtime_raw = shutil.which("sin-orca")
    installed_runtime = (
        Path(installed_runtime_raw).resolve()
        if installed_runtime_raw
        else None
    )
    runtime_ok = installed_runtime == expected_runtime
    checks_ok = all(result.ok for result in results)
    report: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": checks_ok and runtime_ok and not dirty_blockers,
        "allow_dirty": args.allow_dirty,
        "live_requested": args.live,
        "repositories": {
            name: {
                "path": str(root),
                "git": statuses[name],
            }
            for name, root in repositories.items()
        },
        "dirty_blockers": dirty_blockers,
        "runtime": {
            "expected_sin_orca": str(expected_runtime),
            "installed_sin_orca": (
                str(installed_runtime) if installed_runtime else None
            ),
            "canonical": runtime_ok,
        },
        "checks": [asdict(result) for result in results],
    }

    report_dir = Path.home() / ".local" / "state" / "sin-verification"
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        report_dir.chmod(0o700)
    except OSError:
        pass
    report_path = report_dir / "local-integration-latest.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, report_path)
    try:
        report_path.chmod(0o600)
    except OSError:
        pass

    print(json.dumps({
        "ok": report["ok"],
        "report": str(report_path),
        "passed": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
        "dirty_blockers": dirty_blockers,
        "canonical_sin_orca": runtime_ok,
        "live_requested": args.live,
    }, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
