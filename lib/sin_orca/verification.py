"""
sin_orca.verification – Tatsächliche Git-Prüfung und Controller-Tests.

Verifikation läuft im Worker-Worktree.
Verwendet keinen Shell-String mit shell=True.
"""

import fnmatch
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


def run(argv: list[str], *, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, cwd=cwd, text=True, capture_output=True, check=False,
            timeout=timeout, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(argv, 124, stdout=error.stdout or "", stderr=error.stderr or "command timed out")


def output_hash(process: subprocess.CompletedProcess[str]) -> str:
    material = f"{process.stdout}\n{process.stderr}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def output_tail(process: subprocess.CompletedProcess[str], *, limit: int = 4000) -> str:
    text = (process.stdout + "\n" + process.stderr).strip()
    if len(text) <= limit:
        return text
    return "...[truncated]\n" + text[-limit:]


def actual_changed_files(*, worktree: Path, base_sha: str) -> list[str]:
    tracked = run(["git", "diff", "--name-only", "--no-renames", base_sha, "--"], cwd=worktree, timeout=60)
    if tracked.returncode != 0:
        raise RuntimeError(tracked.stderr.strip() or "git diff failed")
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=worktree, timeout=60)
    if untracked.returncode != 0:
        raise RuntimeError(untracked.stderr.strip() or "git ls-files failed")
    files = {
        line.strip()
        for line in f"{tracked.stdout}\n{untracked.stdout}".splitlines()
        if line.strip() and not line.strip().startswith(".sin-worker/")
    }
    return sorted(files)


def path_allowed(path: str, allowed_patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in allowed_patterns:
        candidate = pattern.replace("\\", "/").lstrip("./").rstrip("/")
        if normalized == candidate:
            return True
        if normalized.startswith(candidate + "/"):
            return True
        if fnmatch.fnmatch(normalized, candidate):
            return True
    return False


def validate_scope(
    *, changed_files: list[str], allowed_paths: list[str],
    forbidden_paths: list[str], allow_edits: bool,
) -> list[str]:
    errors: list[str] = []
    if not allow_edits and changed_files:
        errors.append("read-only worker changed files: " + ", ".join(changed_files))
    outside = [p for p in changed_files if not path_allowed(p, allowed_paths)]
    if outside:
        errors.append("files outside allowlist: " + ", ".join(outside))
    forbidden = [p for p in changed_files if path_allowed(p, forbidden_paths)]
    if forbidden:
        errors.append("forbidden files changed: " + ", ".join(forbidden))
    return errors


def validate_diff(*, worktree: Path) -> dict[str, Any]:
    result = run(["git", "diff", "--check"], cwd=worktree, timeout=60)
    return {"ok": result.returncode == 0, "argv": result.args, "exit_code": result.returncode, "output_tail": output_tail(result), "output_sha256": output_hash(result)}


def bounded_diff(*, worktree: Path, base_sha: str, maximum_chars: int = 60000) -> dict[str, Any]:
    result = run(["git", "diff", "--no-ext-diff", "--no-renames", "--unified=4", base_sha, "--"], cwd=worktree, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    full = result.stdout
    clipped = full[:maximum_chars]
    if len(full) > maximum_chars:
        clipped += "\n...[bounded diff truncated]"
    return {"text": clipped, "full_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(), "full_chars": len(full), "truncated": len(full) > maximum_chars}


def run_controller_commands(*, worktree: Path, commands: list[list[str]], timeout: int = 600) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for argv in commands:
        if not argv or not all(isinstance(item, str) for item in argv):
            raise ValueError(f"invalid verification command: {argv!r}")
        process = run(argv, cwd=worktree, timeout=timeout)
        result = {"argv": argv, "exit_code": process.returncode, "ok": process.returncode == 0, "output_tail": output_tail(process), "output_sha256": output_hash(process)}
        results.append(result)
        if process.returncode != 0:
            break
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}
