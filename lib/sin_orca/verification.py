"""
sin_orca.verification – Tatsächliche Git-Prüfung und Controller-Tests.

Verifikation läuft im Worker-Worktree.
Verwendet keinen Shell-String mit shell=True.
"""

import fnmatch
import hashlib
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SECRET_FLAG = re.compile(
    r"(?i)^--?(?:api[-_]?key|token|secret|password|passwd|authorization|cookie)$"
)
SECRET_INLINE = re.compile(
    r"(?i)^(--?(?:api[-_]?key|token|secret|password|passwd|authorization|cookie))=(.+)$"
)
SECRET_OUTPUT = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[-_]?key|authorization|cookie)"
    r"\s*[:=]\s*([^\s,;]+)"
)
AUTHORIZATION_HEADER = re.compile(
    r"(?im)\bAuthorization\s*:\s*[^\r\n]+"
)
BEARER_OUTPUT = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
URL_CREDENTIALS = re.compile(r"(https?://[^:/\s]+:)[^@/\s]+(@)")


def controller_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    return environment


def redact_text(value: str) -> str:
    value = AUTHORIZATION_HEADER.sub("Authorization: <redacted>", value)
    value = BEARER_OUTPUT.sub("Bearer <redacted>", value)
    value = SECRET_OUTPUT.sub(r"\1=<redacted>", value)
    return URL_CREDENTIALS.sub(r"\1<redacted>\2", value)


def redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for item in argv:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        inline = SECRET_INLINE.match(item)
        if inline:
            redacted.append(f"{inline.group(1)}=<redacted>")
            continue
        redacted.append(redact_text(item))
        if SECRET_FLAG.match(item):
            hide_next = True
    return redacted


def ensure_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, cwd=cwd, text=True, capture_output=True, check=False,
            timeout=timeout, env=env or controller_environment(),
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            argv,
            124,
            stdout=ensure_text(error.stdout),
            stderr=ensure_text(error.stderr) or "command timed out",
        )


def output_hash(process: subprocess.CompletedProcess[str]) -> str:
    material = (
        ensure_text(process.stdout)
        + "\n"
        + ensure_text(process.stderr)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def output_tail(process: subprocess.CompletedProcess[str], *, limit: int = 2000) -> str:
    text = redact_text(
        (
            ensure_text(process.stdout)
            + "\n"
            + ensure_text(process.stderr)
        ).strip()
    )
    if len(text) <= limit:
        return text
    return "...[truncated]\n" + text[-limit:]


@contextmanager
def snapshot_index(
    *,
    worktree: Path,
    base_sha: str,
) -> Iterator[dict[str, str]]:
    """Expose the current worktree as a temporary Git index.

    This compares against the synthetic dispatch baseline while leaving the
    repository's real index and HEAD untouched. Pre-existing staged, unstaged,
    and untracked files captured by the baseline therefore do not become worker
    changes later.
    """
    descriptor, raw_path = tempfile.mkstemp(prefix="sin-orca-index-")
    os.close(descriptor)
    index_path = Path(raw_path)
    index_path.unlink(missing_ok=True)
    environment = controller_environment()
    environment["GIT_INDEX_FILE"] = str(index_path)
    try:
        read_tree = run(
            ["git", "read-tree", base_sha],
            cwd=worktree,
            timeout=60,
            env=environment,
        )
        if read_tree.returncode != 0:
            raise RuntimeError(
                read_tree.stderr.strip() or "git read-tree baseline failed"
            )

        snapshot = run(
            ["git", "add", "-A", "--", "."],
            cwd=worktree,
            timeout=180,
            env=environment,
        )
        if snapshot.returncode != 0:
            raise RuntimeError(
                snapshot.stderr.strip() or "git worktree snapshot failed"
            )

        # Runtime coordination files are never task output. Restore their
        # baseline state inside the temporary index only.
        reset_runtime = run(
            [
                "git", "rm", "-r", "-q", "--cached", "--ignore-unmatch",
                "--", ".sin-worker",
            ],
            cwd=worktree,
            timeout=60,
            env=environment,
        )
        if reset_runtime.returncode != 0:
            raise RuntimeError(
                reset_runtime.stderr.strip()
                or "failed to exclude .sin-worker from snapshot"
            )
        yield environment
    finally:
        index_path.unlink(missing_ok=True)
        index_path.with_suffix(index_path.suffix + ".lock").unlink(missing_ok=True)


def actual_changed_files(*, worktree: Path, base_sha: str) -> list[str]:
    with snapshot_index(worktree=worktree, base_sha=base_sha) as environment:
        changed = run(
            [
                "git", "diff", "--cached", "--name-only", "--no-renames",
                base_sha, "--",
            ],
            cwd=worktree,
            timeout=60,
            env=environment,
        )
    if changed.returncode != 0:
        raise RuntimeError(changed.stderr.strip() or "git snapshot diff failed")
    return sorted({
        line.strip()
        for line in changed.stdout.splitlines()
        if line.strip() and not line.strip().startswith(".sin-worker/")
    })


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


def validate_diff(
    *,
    worktree: Path,
    base_sha: str | None = None,
) -> dict[str, Any]:
    if base_sha is None:
        result = run(["git", "diff", "--check"], cwd=worktree, timeout=60)
    else:
        with snapshot_index(worktree=worktree, base_sha=base_sha) as environment:
            result = run(
                ["git", "diff", "--cached", "--check", base_sha, "--"],
                cwd=worktree,
                timeout=60,
                env=environment,
            )
    return {
        "ok": result.returncode == 0,
        "argv": redact_argv(list(result.args)),
        "exit_code": result.returncode,
        "output_tail": output_tail(result),
        "output_sha256": output_hash(result),
    }


def full_worktree_diff(*, worktree: Path, base_sha: str) -> str:
    """Return a stable baseline-to-current diff without touching the real index."""
    with snapshot_index(worktree=worktree, base_sha=base_sha) as environment:
        diff = run(
            [
                "git", "diff", "--cached", "--no-ext-diff", "--no-renames",
                "--binary", "--unified=4", base_sha, "--",
            ],
            cwd=worktree,
            timeout=180,
            env=environment,
        )
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.strip() or "git snapshot diff failed")
    return diff.stdout


def bounded_diff(*, worktree: Path, base_sha: str, maximum_chars: int = 60000) -> dict[str, Any]:
    full = full_worktree_diff(worktree=worktree, base_sha=base_sha)
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
        result = {"argv": redact_argv(argv), "exit_code": process.returncode, "ok": process.returncode == 0, "output_tail": output_tail(process), "output_sha256": output_hash(process)}
        results.append(result)
        if process.returncode != 0:
            break
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}
