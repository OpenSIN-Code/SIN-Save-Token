"""Create and verify reproducible task completion manifests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .verification import actual_changed_files, full_worktree_diff


def subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    return environment


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)

    return digest.hexdigest()


def run_git(
    root: Path,
    *args: str,
) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env=subprocess_environment(),
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or process.stdout.strip()
        )

    return process.stdout


def tool_version(
    executable: str,
) -> str | None:
    try:
        process = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = (
        process.stdout
        or process.stderr
    ).strip()

    return output[:500] or None


def build_manifest(
    *,
    task: dict[str, Any],
    worktree: Path,
    events_file: Path,
    artifacts: list[Path],
    verification: dict[str, Any],
    review: dict[str, Any] | None,
) -> dict[str, Any]:
    worktree = worktree.resolve()

    head_sha = run_git(
        worktree,
        "rev-parse",
        "HEAD",
    ).strip()

    diff = full_worktree_diff(
        worktree=worktree,
        base_sha=task["base_sha"],
    )

    changed_files = actual_changed_files(
        worktree=worktree,
        base_sha=task["base_sha"],
    )

    artifact_entries = []

    for path in sorted(
        (item.resolve() for item in artifacts),
        key=str,
    ):
        if not path.is_file():
            raise RuntimeError(
                f"manifest artifact missing: {path}"
            )

        artifact_entries.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )

    body = {
        "schema_version": 1,
        "created_at": task.get("created_at") or utc_now(),
        "task_id": task["task_id"],
        "task_hash": task["task_hash"],
        "repository_root": str(worktree),
        "base_sha": task["base_sha"],
        "baseline_ref": task.get("baseline_ref"),
        "repository_head_sha": task.get("repository_head_sha"),
        "head_sha": head_sha,
        "changed_files": changed_files,
        "diff_sha256": sha256_bytes(
            diff.encode("utf-8")
        ),
        "events": {
            "path": str(events_file.resolve()),
            "sha256": sha256_file(events_file),
        },
        "artifacts": artifact_entries,
        "verification_sha256": sha256_bytes(
            canonical_bytes(verification)
        ),
        "review_sha256": (
            sha256_bytes(canonical_bytes(review))
            if review is not None
            else None
        ),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git": tool_version("git"),
            "orca": tool_version("orca"),
            "codex": tool_version("codex"),
            "graphify": tool_version("graphify"),
            "gitnexus": tool_version("gitnexus"),
            "code_review_graph": tool_version(
                "code-review-graph"
            ),
            "oracle": tool_version("oracle"),
        },
    }

    integrity_hash = sha256_bytes(
        canonical_bytes(body)
    )

    signature: str | None = None
    key = os.getenv("SIN_MANIFEST_HMAC_KEY")

    if key:
        signature = hmac.new(
            key.encode("utf-8"),
            canonical_bytes(body),
            hashlib.sha256,
        ).hexdigest()

    return {
        "body": body,
        "integrity": {
            "algorithm": "sha256",
            "hash": integrity_hash,
            "hmac_sha256": signature,
            "authenticated": signature is not None,
        },
    }


def write_manifest(
    path: Path,
    manifest: dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)

    os.replace(temporary, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def verify_manifest(
    manifest: dict[str, Any],
) -> list[str]:
    errors: list[str] = []

    body = manifest.get("body")
    integrity = manifest.get("integrity")

    if not isinstance(body, dict):
        return ["manifest body missing"]

    if not isinstance(integrity, dict):
        return ["manifest integrity block missing"]

    if integrity.get("algorithm") != "sha256":
        errors.append("manifest integrity algorithm is invalid")
    authenticated = integrity.get("authenticated")
    if not isinstance(authenticated, bool):
        errors.append("manifest authenticated flag is invalid")

    expected_hash = sha256_bytes(
        canonical_bytes(body)
    )

    if integrity.get("hash") != expected_hash:
        errors.append("manifest integrity hash mismatch")

    key = os.getenv("SIN_MANIFEST_HMAC_KEY")
    stored_signature = integrity.get("hmac_sha256")
    if authenticated is not bool(stored_signature):
        errors.append("manifest authenticated flag does not match signature")
    if key and not stored_signature:
        errors.append("manifest HMAC is required by current controller policy")

    if stored_signature:
        if not key:
            errors.append(
                "manifest is authenticated but verification key is unavailable"
            )
        else:
            expected_signature = hmac.new(
                key.encode("utf-8"),
                canonical_bytes(body),
                hashlib.sha256,
            ).hexdigest()

            if not hmac.compare_digest(
                stored_signature,
                expected_signature,
            ):
                errors.append(
                    "manifest HMAC signature mismatch"
                )

    events = body.get("events")

    if not isinstance(events, dict):
        errors.append("manifest events block missing")
    else:
        path_value = events.get("path")
        expected = events.get("sha256")
        if not isinstance(path_value, str) or not path_value:
            errors.append("manifest events path is invalid")
        elif not isinstance(expected, str) or not expected:
            errors.append("manifest events hash is invalid")
        else:
            path = Path(path_value)
            if not path.is_file():
                errors.append("events file missing")
            elif sha256_file(path) != expected:
                errors.append("events file changed")

    artifacts = body.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("manifest artifacts list is invalid")
        artifacts = []

    for artifact in artifacts:
        if not isinstance(artifact, dict):
            errors.append("invalid artifact entry")
            continue

        path = Path(str(artifact.get("path", "")))

        if not path.is_file():
            errors.append(
                f"artifact missing: {path}"
            )
            continue

        if sha256_file(path) != artifact.get("sha256"):
            errors.append(
                f"artifact changed: {path}"
            )

    repository_value = body.get("repository_root")
    base_sha = body.get("base_sha")
    baseline_ref = body.get("baseline_ref")
    if isinstance(repository_value, str) and isinstance(base_sha, str):
        worktree = Path(repository_value).expanduser()
        if not worktree.is_dir():
            errors.append("manifest worktree is unavailable")
        else:
            try:
                if not isinstance(baseline_ref, str) or not baseline_ref.startswith(
                    "refs/sin-orca/baselines/"
                ):
                    errors.append("manifest baseline ref is invalid")
                else:
                    resolved_baseline = run_git(
                        worktree,
                        "rev-parse",
                        baseline_ref,
                    ).strip()
                    if resolved_baseline != base_sha:
                        errors.append("manifest baseline ref changed")

                current_head = run_git(
                    worktree,
                    "rev-parse",
                    "HEAD",
                ).strip()
                if current_head != body.get("head_sha"):
                    errors.append("worktree HEAD changed")

                current_changed = actual_changed_files(
                    worktree=worktree,
                    base_sha=base_sha,
                )
                if current_changed != body.get("changed_files"):
                    errors.append("worktree changed_files changed")

                current_diff = full_worktree_diff(
                    worktree=worktree,
                    base_sha=base_sha,
                )
                if sha256_bytes(current_diff.encode("utf-8")) != body.get(
                    "diff_sha256"
                ):
                    errors.append("worktree diff changed")
            except RuntimeError as error:
                errors.append(f"worktree verification failed: {error}")

    return errors
