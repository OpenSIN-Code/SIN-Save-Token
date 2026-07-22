#!/usr/bin/env python3
"""Controlled one-way export from gbrain to canonical Cognee memory.

There is deliberately no automatic Cognee -> gbrain bulk sync.
Cognee owns durable domain memory. Automatic bidirectional replication creates
duplicates, feedback loops, stale copies, and higher retrieval cost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = (
    Path.home()
    / ".local"
    / "share"
    / "sin-save-token"
    / "brain-sync.sqlite3"
)
MEMORY_WRITER = Path(__file__).resolve().parent / "sin-memory-write"


def initialize() -> sqlite3.Connection:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATE_FILE)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS exports (
            source_system TEXT NOT NULL,
            source_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            exported_at INTEGER NOT NULL,
            PRIMARY KEY(source_system, source_id)
        )
        """
    )
    connection.commit()
    return connection


def run(argv: list[str], timeout: int = 30) -> tuple[str, int]:
    try:
        process = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "OPENAI_BASE_URL": "http://127.0.0.1:8012/v1",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", 1

    return process.stdout.strip(), process.returncode


def list_gbrain_pages() -> list[str]:
    output, returncode = run(["gbrain", "list", "-n", "500"])
    if returncode != 0:
        return []

    pages: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        slug = line.split("\t", 1)[0].strip()
        if not slug or slug.startswith(("Pages:", "By ")):
            continue

        pages.append(slug)

    return pages


def read_gbrain_page(slug: str) -> str:
    output, returncode = run(["gbrain", "get", slug])
    return output if returncode == 0 else ""


def hash_content(content: str) -> str:
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def previously_exported(
    connection: sqlite3.Connection,
    slug: str,
    digest: str,
) -> bool:
    row = connection.execute(
        """
        SELECT content_hash
        FROM exports
        WHERE source_system = 'gbrain' AND source_id = ?
        """,
        (slug,),
    ).fetchone()
    return row is not None and row[0] == digest


def mark_exported(
    connection: sqlite3.Connection,
    slug: str,
    digest: str,
) -> None:
    connection.execute(
        """
        INSERT INTO exports(source_system, source_id, content_hash, exported_at)
        VALUES ('gbrain', ?, ?, ?)
        ON CONFLICT(source_system, source_id) DO UPDATE SET
            content_hash = excluded.content_hash,
            exported_at = excluded.exported_at
        """,
        (slug, digest, int(time.time())),
    )
    connection.commit()


def eligible(content: str) -> bool:
    lowered = content.lower()

    # Only explicitly curated pages cross the boundary.
    accepted_markers = (
        "memory-export: true",
        "memory_export: true",
        "tags: [decision",
        "type: decision",
        "type: constraint",
        "type: verified_fact",
        "type: gotcha",
        "type: resolved_failure",
    )
    return any(marker in lowered for marker in accepted_markers)


def infer_type(content: str) -> str:
    lowered = content.lower()

    for memory_type in (
        "decision",
        "constraint",
        "verified_fact",
        "gotcha",
        "resolved_failure",
    ):
        if f"type: {memory_type}" in lowered:
            return memory_type

    return "verified_fact"


def export_page(
    connection: sqlite3.Connection,
    slug: str,
    *,
    dry_run: bool,
) -> str:
    content = read_gbrain_page(slug)
    if not content:
        return "read-failed"

    if not eligible(content):
        return "not-curated"

    digest = hash_content(content)
    if previously_exported(connection, slug, digest):
        return "unchanged"

    command = [
        str(MEMORY_WRITER),
        content,
        "--type",
        infer_type(content),
        "--scope",
        "fleet",
        "--source",
        f"gbrain:{slug}",
    ]
    if dry_run:
        command.append("--dry-run")

    output, returncode = run(command, timeout=150)
    if returncode != 0:
        print(output, file=sys.stderr)
        return "write-failed"

    if not dry_run:
        mark_exported(connection, slug, digest)

    return "exported"


def status(connection: sqlite3.Connection) -> int:
    count = connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
    newest = connection.execute(
        "SELECT MAX(exported_at) FROM exports"
    ).fetchone()[0]

    print(
        json.dumps(
            {
                "exported_pages": count,
                "newest_export_unix": newest,
                "direction": "gbrain -> cognee",
                "automatic_reverse_sync": False,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--slug")
    export_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("status")
    args = parser.parse_args()

    connection = initialize()

    if args.command == "status":
        return status(connection)

    pages = [args.slug] if args.slug else list_gbrain_pages()
    counters: dict[str, int] = {}

    for slug in pages:
        result = export_page(connection, slug, dry_run=args.dry_run)
        counters[result] = counters.get(result, 0) + 1
        print(f"{slug}: {result}")

    print(json.dumps(counters, sort_keys=True))
    return 0 if counters.get("write-failed", 0) == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
