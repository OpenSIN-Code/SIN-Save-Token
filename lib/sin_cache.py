"""
sin-cache v1 – 6-Schichten-Cache für SIN-Code.

Schichten:
  L0  OpenAI Prompt-/KV-Cache (prompt-stability, kein Modul)
  L1  Exact Query Cache (normalisierter Hash)
  L2  Semantic Query Cache (lexikalische Ähnlichkeit)
  L3  Evidence-/Content-Addressed Cache (dateibasiert)
  L4  Worker-Artefakt-Cache (Explorer/Reviewer/Tests)
  L5  Verification-/Review-Cache
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

STOP_WORDS = frozenset({
    "where", "what", "which", "find", "show", "list",
    "wo", "welche", "was", "wird", "zeige", "finde",
    "function", "method", "file", "class", "module",
    "funktion", "methode", "datei", "klasse", "modul",
    "the", "a", "an", "is", "are", "was", "were",
    "der", "die", "das", "ein", "eine", "ist", "sind",
})

WORD_PATTERN = re.compile(r"[a-z0-9_]+")

SCHEMA_VERSION = 1

REVIEW_SCHEMA_VERSION = 2


# ─── Hilfsfunktionen ────────────────────────────────────────────────────────

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def utc_now() -> int:
    return int(time.time())


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Repository Identity (über alle Worktrees geteilt) ──────────────────────

def safe_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("SIN_MANIFEST_HMAC_KEY", None)
    return environment


def repository_identity(cwd: str) -> str:
    environment = safe_subprocess_environment()
    common_dir_result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd, text=True, capture_output=True, check=False, timeout=3,
        env=environment,
    )

    remote_result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=cwd, text=True, capture_output=True, check=False, timeout=3,
        env=environment,
    )

    material = {
        "common_dir": os.path.realpath(
            os.path.join(cwd, common_dir_result.stdout.strip())
        ) if common_dir_result.returncode == 0 else os.path.realpath(cwd),
        "remote": remote_result.stdout.strip() if remote_result.returncode == 0 else "",
    }

    return sha256_json(material)[:24]


# ─── Semantic Query Normalization ────────────────────────────────────────────

def canonical_query(query: str) -> str:
    words = WORD_PATTERN.findall(query.lower())
    words = [w for w in words if w not in STOP_WORDS]
    return " ".join(sorted(set(words)))


def query_similarity(a: str, b: str) -> float:
    words_a = set(WORD_PATTERN.findall(a.lower()))
    words_b = set(WORD_PATTERN.findall(b.lower()))

    if not words_a or not words_b:
        return 0.0

    intersection = words_a & words_b
    union = words_a | words_b

    return len(intersection) / len(union) if union else 0.0


# ─── Evidence Validation ────────────────────────────────────────────────────

def file_content_hash(file_path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except (OSError, IOError):
        return None


def evidence_is_current(
    repository: Path,
    evidence: list[dict[str, Any]],
) -> bool:
    repo_resolved = repository.resolve()

    for item in evidence:
        if not isinstance(item, dict):
            return False
        relative = item.get("path")
        expected_hash = item.get("content_sha256")
        if not isinstance(relative, str) or not relative.strip():
            return False
        if not isinstance(expected_hash, str) or not expected_hash:
            return False

        file_path = (repository / relative).resolve()

        try:
            file_path.relative_to(repo_resolved)
        except ValueError:
            return False

        if not file_path.is_file():
            return False

        actual_hash = file_content_hash(file_path)
        if actual_hash is None or actual_hash != expected_hash:
            return False

    return True


# ─── SQL Schema ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cache_blobs (
    content_hash TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    content_type TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    repository_path TEXT NOT NULL DEFAULT '.',
    route TEXT NOT NULL,
    provider TEXT NOT NULL,
    normalized_query TEXT NOT NULL,
    semantic_signature TEXT,
    blob_hash TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    provider_version TEXT,
    created_at INTEGER NOT NULL,
    last_accessed_at INTEGER NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(blob_hash) REFERENCES cache_blobs(content_hash)
);

CREATE TABLE IF NOT EXISTS cache_views (
    blob_hash TEXT NOT NULL,
    view_type TEXT NOT NULL,
    token_budget INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(blob_hash, view_type, token_budget)
);

CREATE TABLE IF NOT EXISTS command_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    error_hash TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_stats (
    date TEXT NOT NULL,
    route TEXT NOT NULL,
    hit_type TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(date, route, hit_type)
);
"""


# ─── Cache-Klasse ───────────────────────────────────────────────────────────

class SinCache:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path.home() / ".local" / "state" / "sin-cache" / "cache.db"

        db_path = db_path.expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            db_path.parent.chmod(0o700)
        except OSError:
            pass
        self.db_path = db_path

        self.conn: sqlite3.Connection = sqlite3.connect(
            str(db_path),
            timeout=10,
        )
        self._closed = False
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        try:
            db_path.chmod(0o600)
        except OSError:
            pass

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("cache is closed")
        return self.conn

    def close(self) -> None:
        if not self._closed:
            self.conn.close()
            self._closed = True

    # ─── L1: Exact Query Cache ───────────────────────────────────────────

    def _exact_key(
        self,
        route: str,
        provider: str,
        query: str,
        repository_id: str,
        *,
        policy_hash: str = "",
        provider_version: str = "",
    ) -> str:
        material = {
            "route": route,
            "provider": provider,
            "normalized_query": canonical_query(query),
            "repository_id": repository_id,
            "policy_hash": policy_hash,
            "provider_version": provider_version,
            "schema_version": SCHEMA_VERSION,
        }
        return sha256_json(material)

    def get(
        self,
        route: str,
        provider: str,
        query: str,
        repository_id: str,
        *,
        repository_path: Path | str | None = None,
        policy_hash: str = "",
        provider_version: str = "",
    ) -> Optional[dict[str, Any]]:
        key = self._exact_key(
            route, provider, query, repository_id,
            policy_hash=policy_hash,
            provider_version=provider_version,
        )

        row = self.conn.execute(
            "SELECT ce.*, cb.content, cb.content_type "
            "FROM cache_entries ce "
            "JOIN cache_blobs cb ON ce.blob_hash = cb.content_hash "
            "WHERE ce.cache_key = ?",
            (key,),
        ).fetchone()

        if row is None:
            self._record_stat(route, "miss")
            return None

        entry = self._row_to_entry(row)
        try:
            evidence = json.loads(entry.get("evidence_json", "[]"))
        except json.JSONDecodeError:
            evidence = None

        if evidence is None or not isinstance(evidence, list):
            self.invalidate_by_key(key)
            self._record_stat(route, "invalidated")
            return None
        if evidence:
            repo_path = Path(
                repository_path
                or entry.get("repository_path")
                or "."
            )
            if not evidence_is_current(repo_path, evidence):
                self.invalidate_by_key(key)
                self._record_stat(route, "invalidated")
                return None

        self.conn.execute(
            "UPDATE cache_entries SET last_accessed_at = ?, hit_count = hit_count + 1 "
            "WHERE cache_key = ?",
            (utc_now(), key),
        )
        self.conn.commit()

        self._record_stat(route, "exact_hit")
        return entry

    def put(
        self,
        route: str,
        provider: str,
        query: str,
        repository_id: str,
        content: str,
        content_type: str = "text",
        evidence: Optional[list[dict[str, Any]]] = None,
        policy_hash: str = "",
        provider_version: str = "",
        repository_path: str = ".",
    ) -> str:
        key = self._exact_key(
            route, provider, query, repository_id,
            policy_hash=policy_hash,
            provider_version=provider_version,
        )
        blob_hash = sha256_text(content)
        previous = self.conn.execute(
            "SELECT blob_hash FROM cache_entries WHERE cache_key = ?",
            (key,),
        ).fetchone()
        previous_blob = str(previous[0]) if previous is not None else None

        self.conn.execute(
            "INSERT OR IGNORE INTO cache_blobs "
            "(content_hash, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?)",
            (blob_hash, content.encode("utf-8"), content_type, utc_now()),
        )

        evidence_json = json.dumps(evidence or [], ensure_ascii=False)

        self.conn.execute(
            "INSERT OR REPLACE INTO cache_entries "
            "(cache_key, repository_id, repository_path, route, provider, normalized_query, "
            "semantic_signature, blob_hash, evidence_json, policy_hash, "
            "provider_version, created_at, last_accessed_at, hit_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                key, repository_id, str(repository_path), route, provider, canonical_query(query),
                canonical_query(query), blob_hash, evidence_json,
                policy_hash, provider_version, utc_now(), utc_now(),
            ),
        )

        if previous_blob and previous_blob != blob_hash:
            remaining = self.conn.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE blob_hash = ?",
                (previous_blob,),
            ).fetchone()[0]
            if remaining == 0:
                self.conn.execute(
                    "DELETE FROM cache_views WHERE blob_hash = ?",
                    (previous_blob,),
                )
                self.conn.execute(
                    "DELETE FROM cache_blobs WHERE content_hash = ?",
                    (previous_blob,),
                )

        self.conn.commit()
        return key

    # ─── L2: Semantic Query Cache ────────────────────────────────────────

    def get_semantic(
        self,
        route: str,
        provider: str,
        query: str,
        repository_id: str,
        threshold: float = 0.7,
        *,
        repository_path: Path | str | None = None,
        policy_hash: str = "",
        provider_version: str = "",
        require_evidence: bool = True,
    ) -> Optional[dict[str, Any]]:
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not 0.0 <= float(threshold) <= 1.0
        ):
            raise ValueError("semantic threshold must be between 0 and 1")

        canonical = canonical_query(query)

        rows = self.conn.execute(
            "SELECT ce.*, cb.content, cb.content_type "
            "FROM cache_entries ce "
            "JOIN cache_blobs cb ON ce.blob_hash = cb.content_hash "
            "WHERE ce.route = ? AND ce.provider = ? AND ce.repository_id = ? "
            "AND ce.policy_hash = ? AND COALESCE(ce.provider_version, '') = ?",
            (
                route,
                provider,
                repository_id,
                policy_hash,
                provider_version,
            ),
        ).fetchall()

        best_match = None
        best_score = 0.0

        for row in rows:
            entry = self._row_to_entry(row)
            stored_canonical = entry.get("normalized_query", "")
            score = _jaccard_similarity(canonical, stored_canonical)

            if score > best_score and score >= threshold:
                best_score = score
                best_match = entry

        if best_match is None:
            self._record_stat(route, "semantic_miss")
            return None

        try:
            evidence = json.loads(best_match.get("evidence_json", "[]"))
        except json.JSONDecodeError:
            evidence = None
        if evidence is None or not isinstance(evidence, list):
            self.invalidate_by_key(str(best_match["cache_key"]))
            self._record_stat(route, "semantic_invalidated")
            return None
        if require_evidence and not evidence:
            self._record_stat(route, "semantic_unverified")
            return None
        if evidence:
            repo_path = Path(
                repository_path
                or best_match.get("repository_path")
                or "."
            )
            if not evidence_is_current(repo_path, evidence):
                self.invalidate_by_key(str(best_match["cache_key"]))
                self._record_stat(route, "semantic_invalidated")
                return None

        self.conn.execute(
            "UPDATE cache_entries SET last_accessed_at = ?, "
            "hit_count = hit_count + 1 WHERE cache_key = ?",
            (utc_now(), best_match["cache_key"]),
        )
        self.conn.commit()
        self._record_stat(route, "semantic_hit")
        return best_match

    # ─── L3: Evidence/Content-Addressed Cache ────────────────────────────

    def put_evidence(
        self,
        route: str,
        provider: str,
        query: str,
        repository_id: str,
        content: str,
        evidence: list[dict[str, Any]],
        *,
        repository_path: Path | str | None = None,
        **kwargs: Any,
    ) -> str:
        root = Path(repository_path or ".").resolve()
        normalized_evidence: list[dict[str, Any]] = []

        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("evidence entries must be objects")
            relative_value = item.get("path")
            if not isinstance(relative_value, str) or not relative_value.strip():
                raise ValueError("evidence entries require a non-empty path")
            relative = relative_value.strip()

            file_path = (root / relative).resolve()

            try:
                file_path.relative_to(root)
            except ValueError as error:
                raise ValueError(
                    f"evidence path escapes repository: {relative}"
                ) from error

            if not file_path.is_file():
                raise ValueError(
                    f"evidence file does not exist: {relative}"
                )

            normalized_evidence.append(
                {
                    **item,
                    "path": str(file_path.relative_to(root)),
                    "content_sha256": file_content_hash(file_path) or "",
                }
            )

        return self.put(
            route=route,
            provider=provider,
            query=query,
            repository_id=repository_id,
            content=content,
            evidence=normalized_evidence,
            repository_path=str(root),
            **kwargs,
        )

    def invalidate_by_path(self, changed_path: str) -> int:
        rows = self.conn.execute(
            "SELECT cache_key, evidence_json FROM cache_entries"
        ).fetchall()

        to_invalidate = []
        normalized_changed = str(Path(changed_path))
        for key, evidence_json in rows:
            try:
                evidence = json.loads(evidence_json)
            except json.JSONDecodeError:
                to_invalidate.append(key)
                continue
            if not isinstance(evidence, list):
                to_invalidate.append(key)
                continue
            for item in evidence:
                if not isinstance(item, dict):
                    to_invalidate.append(key)
                    break
                item_path = item.get("path")
                if isinstance(item_path, str) and str(Path(item_path)) == normalized_changed:
                    to_invalidate.append(key)
                    break

        for key in to_invalidate:
            self.invalidate_by_key(key)

        return len(to_invalidate)

    def invalidate_by_key(self, key: str) -> None:
        row = self.conn.execute(
            "SELECT blob_hash FROM cache_entries WHERE cache_key = ?", (key,)
        ).fetchone()

        if row is None:
            return

        blob_hash = row[0]

        self.conn.execute(
            "DELETE FROM cache_entries WHERE cache_key = ?", (key,)
        )

        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM cache_entries WHERE blob_hash = ?", (blob_hash,)
        ).fetchone()[0]

        if remaining == 0:
            self.conn.execute("DELETE FROM cache_views WHERE blob_hash = ?", (blob_hash,))
            self.conn.execute("DELETE FROM cache_blobs WHERE content_hash = ?", (blob_hash,))

        self.conn.commit()

    # ─── L4: Worker Artifact Cache ───────────────────────────────────────

    def cache_explorer_result(
        self,
        repository_id: str,
        task_hash: str,
        claims: list[str],
        evidence: list[dict[str, Any]],
        candidate_paths: list[str],
    ) -> str:
        content = canonical_json({
            "claims": claims,
            "evidence": evidence,
            "candidate_paths": candidate_paths,
        })

        return self.put(
            route="explorer_result",
            provider="worker",
            query=task_hash,
            repository_id=repository_id,
            content=content,
            content_type="json",
        )

    def cache_review_result(
        self,
        repository_id: str,
        task_hash: str,
        acceptance_hash: str,
        diff_hash: str,
        test_hash: str,
        reviewer_model: str,
        verdict: str,
        criteria: list[dict[str, Any]],
    ) -> str:
        content = canonical_json({
            "verdict": verdict,
            "criteria": criteria,
        })

        key_material = {
            "task_hash": task_hash,
            "acceptance_hash": acceptance_hash,
            "diff_hash": diff_hash,
            "test_hash": test_hash,
            "reviewer": reviewer_model,
            "schema": REVIEW_SCHEMA_VERSION,
        }

        return self.put(
            route="review_result",
            provider=reviewer_model,
            query=sha256_json(key_material),
            repository_id=repository_id,
            content=content,
            content_type="json",
        )

    def cache_test_result(
        self,
        repository_id: str,
        argv: list[str],
        source_hashes: dict[str, str],
        lockfile_hash: str,
        toolchain: dict[str, str],
        exit_code: int,
        summary: str,
        full_output: str,
    ) -> str:
        content = canonical_json({
            "exit_code": exit_code,
            "summary": summary,
            "duration_ms": 0,
        })

        key_query = canonical_json({
            "argv": argv,
            "source_hashes": source_hashes,
            "lockfile_hash": lockfile_hash,
            "toolchain": toolchain,
        })

        return self.put(
            route="test_result",
            provider="command",
            query=key_query,
            repository_id=repository_id,
            content=content,
            content_type="json",
        )

    # ─── Views (350/650/1200 Token Ansichten) ───────────────────────────

    def store_view(
        self,
        blob_hash: str,
        view_type: str,
        token_budget: int,
        content: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache_views (blob_hash, view_type, token_budget, content) "
            "VALUES (?, ?, ?, ?)",
            (blob_hash, view_type, token_budget, content),
        )
        self.conn.commit()

    def get_view(
        self,
        blob_hash: str,
        view_type: str,
        token_budget: int,
    ) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content FROM cache_views "
            "WHERE blob_hash = ? AND view_type = ? AND token_budget = ?",
            (blob_hash, view_type, token_budget),
        ).fetchone()
        return row[0] if row else None

    # ─── Stats ───────────────────────────────────────────────────────────

    def _record_stat(self, route: str, hit_type: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO cache_stats (date, route, hit_type, count) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(date, route, hit_type) DO UPDATE SET count = count + 1",
            (today, route, hit_type),
        )
        self.conn.commit()

    def stats(self, days: int = 1) -> dict[str, Any]:
        if (
            not isinstance(days, int)
            or isinstance(days, bool)
            or days < 1
            or days > 3650
        ):
            raise ValueError("days must be an integer from 1 to 3650")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days - 1)
        ).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT route, hit_type, SUM(count) as total "
            "FROM cache_stats WHERE date >= ? "
            "GROUP BY route, hit_type",
            (cutoff,),
        ).fetchall()

        stats: dict[str, Any] = {
            "exact_hits": 0,
            "semantic_hits": 0,
            "misses": 0,
            "invalidated": 0,
            "total": 0,
        }

        for route, hit_type, total in rows:
            stats["total"] += total
            if hit_type == "exact_hit":
                stats["exact_hits"] += total
            elif hit_type == "semantic_hit":
                stats["semantic_hits"] += total
            elif hit_type == "miss":
                stats["misses"] += total
            elif "invalidated" in hit_type:
                stats["invalidated"] += total

        total_entries = self.conn.execute(
            "SELECT COUNT(*) FROM cache_entries"
        ).fetchone()[0]

        total_blobs = self.conn.execute(
            "SELECT COUNT(*) FROM cache_blobs"
        ).fetchone()[0]

        stats["total_entries"] = total_entries
        stats["total_blobs"] = total_blobs
        stats["provider_calls_avoided"] = stats["exact_hits"] + stats["semantic_hits"]

        return stats

    # ─── GC ──────────────────────────────────────────────────────────────

    def gc(self, max_age_seconds: int = 86400 * 30) -> int:
        if (
            not isinstance(max_age_seconds, int)
            or isinstance(max_age_seconds, bool)
            or max_age_seconds < 0
        ):
            raise ValueError("max_age_seconds must be a non-negative integer")
        cutoff = utc_now() - max_age_seconds

        stale = self.conn.execute(
            "SELECT cache_key, blob_hash FROM cache_entries "
            "WHERE last_accessed_at < ? AND hit_count < 2",
            (cutoff,),
        ).fetchall()

        for key, blob_hash in stale:
            self.invalidate_by_key(key)

        self.conn.commit()
        return len(stale)

    # ─── Helper ──────────────────────────────────────────────────────────

    def _row_to_entry(self, row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        result = {}
        for key in keys:
            value = row[key]
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            result[key] = value
        return result


def _jaccard_similarity(a: str, b: str) -> float:
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0
