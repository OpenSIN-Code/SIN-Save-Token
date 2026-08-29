"""Durable transport-only broker for signed SIN web callbacks.

The canonical callback capability/HMAC record remains authoritative.  This DB is
only a reconstructible delivery queue and receipt/lease index.  It never owns
work-item state, verification, or completion.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

BROKER_SCHEMA_VERSION = 2
TERMINAL_STATES = {"acknowledged", "expired", "cancelled", "abandoned"}
DELIVERABLE_STATES = {"queued", "retry_wait"}
RECEIPT_WATCH_STATES = {"sent", "indeterminate"}
DEFAULT_LEASE_SECONDS = 120
DEFAULT_BASE_BACKOFF_SECONDS = 5
DEFAULT_MAX_BACKOFF_SECONDS = 15 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def broker_state_dir() -> Path:
    override = os.getenv("SIN_CALLBACK_BROKER_STATE_DIR")
    path = Path(override).expanduser() if override else Path.home() / ".local" / "state" / "sin-orca"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def broker_db_path() -> Path:
    override = os.getenv("SIN_CALLBACK_BROKER_DB")
    return Path(override).expanduser() if override else broker_state_dir() / "callback-broker.sqlite3"


@dataclass(frozen=True)
class DeliveryRef:
    delivery_id: str
    relay_id: str
    repository_root: str
    callback_status: str
    transport: str
    target_id: str
    message_sha256: str
    expires_at: str


class CallbackBrokerStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or broker_db_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS broker_meta(
                     key TEXT PRIMARY KEY,
                     value TEXT NOT NULL
                   )"""
            )
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deliveries'"
            ).fetchone()
            if exists is None:
                db.execute(
                    """CREATE TABLE deliveries(
                         delivery_id TEXT PRIMARY KEY,
                         relay_id TEXT NOT NULL UNIQUE,
                         repository_root TEXT NOT NULL,
                         callback_status TEXT NOT NULL,
                         transport TEXT NOT NULL,
                         target_id TEXT NOT NULL,
                         message_sha256 TEXT NOT NULL,
                         expires_at TEXT NOT NULL,
                         state TEXT NOT NULL,
                         attempts INTEGER NOT NULL DEFAULT 0,
                         available_at TEXT NOT NULL,
                         lease_token TEXT,
                         lease_expires_at TEXT,
                         last_attempt_at TEXT,
                         last_reason_code TEXT,
                         created_at TEXT NOT NULL,
                         updated_at TEXT NOT NULL,
                         sent_at TEXT,
                         acknowledged_at TEXT
                       )"""
                )
            else:
                columns = {
                    str(row[1]) for row in db.execute("PRAGMA table_info(deliveries)").fetchall()
                }
                additions = {
                    "available_at": "TEXT",
                    "lease_token": "TEXT",
                    "lease_expires_at": "TEXT",
                    "last_attempt_at": "TEXT",
                    "last_reason_code": "TEXT",
                    "sent_at": "TEXT",
                    "acknowledged_at": "TEXT",
                }
                for name, sql_type in additions.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE deliveries ADD COLUMN {name} {sql_type}")
                now = _iso()
                db.execute(
                    """UPDATE deliveries
                       SET available_at=COALESCE(NULLIF(available_at,''), updated_at, created_at, ?)
                       WHERE available_at IS NULL OR available_at=''""",
                    (now,),
                )

            db.execute(
                "CREATE INDEX IF NOT EXISTS deliveries_due_idx ON deliveries(state, available_at)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS repositories(
                     repository_root TEXT PRIMARY KEY,
                     registered_at TEXT NOT NULL,
                     last_sync_at TEXT
                   )"""
            )
            db.execute(
                """INSERT OR IGNORE INTO repositories(repository_root,registered_at)
                   SELECT repository_root, COALESCE(MIN(created_at), ?)
                   FROM deliveries
                   WHERE repository_root IS NOT NULL AND repository_root<>''
                   GROUP BY repository_root""",
                (_iso(),),
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS delivery_events(
                     sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                     delivery_id TEXT NOT NULL,
                     event_type TEXT NOT NULL,
                     reason_code TEXT,
                     created_at TEXT NOT NULL,
                     FOREIGN KEY(delivery_id) REFERENCES deliveries(delivery_id) ON DELETE CASCADE
                   )"""
            )
            db.execute(
                """INSERT INTO broker_meta(key,value) VALUES('schema_version',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(BROKER_SCHEMA_VERSION),),
            )
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def enqueue(self, ref: DeliveryRef) -> dict[str, Any]:
        now = _iso()
        payload = asdict(ref)
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (ref.delivery_id,)).fetchone()
            if existing is not None:
                for field in ("relay_id", "repository_root", "callback_status", "transport", "target_id", "message_sha256", "expires_at"):
                    if str(existing[field]) != str(payload[field]):
                        raise RuntimeError(f"broker delivery identity mismatch for {field}")
                return dict(existing)
            db.execute(
                """INSERT INTO deliveries(
                     delivery_id,relay_id,repository_root,callback_status,transport,target_id,
                     message_sha256,expires_at,state,attempts,available_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ref.delivery_id, ref.relay_id, ref.repository_root, ref.callback_status,
                    ref.transport, ref.target_id, ref.message_sha256, ref.expires_at,
                    "queued", 0, now, now, now,
                ),
            )
            db.execute(
                "INSERT INTO delivery_events(delivery_id,event_type,created_at) VALUES(?,?,?)",
                (ref.delivery_id, "queued", now),
            )
            row = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (ref.delivery_id,)).fetchone()
            assert row is not None
            return dict(row)

    def get(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            return dict(row) if row is not None else None

    def register_repository(self, repository: str | Path) -> str:
        root = str(Path(repository).expanduser().resolve())
        now = _iso()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO repositories(repository_root,registered_at) VALUES(?,?)",
                (root, now),
            )
        return root

    def list_repositories(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT repository_root FROM repositories ORDER BY repository_root"
            ).fetchall()
            return [str(row["repository_root"]) for row in rows]

    def mark_repository_synced(self, repository: str | Path) -> None:
        root = self.register_repository(repository)
        with self.transaction() as db:
            db.execute(
                "UPDATE repositories SET last_sync_at=? WHERE repository_root=?",
                (_iso(), root),
            )

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as db:
            if state:
                rows = db.execute(
                    "SELECT * FROM deliveries WHERE state=? ORDER BY created_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM deliveries ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def state_counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state ORDER BY state"
            ).fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}

    def claim_due(self, *, limit: int = 10, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> list[dict[str, Any]]:
        now = _now()
        now_s = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        claimed: list[dict[str, Any]] = []
        with self.transaction() as db:
            db.execute(
                """UPDATE deliveries
                   SET state='retry_wait',lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE state='delivering' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""",
                (now_s, now_s),
            )
            rows = db.execute(
                """SELECT * FROM deliveries
                   WHERE state IN ('queued','retry_wait')
                     AND available_at<=?
                   ORDER BY available_at,created_at LIMIT ?""",
                (now_s, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                token = secrets.token_hex(16)
                db.execute(
                    """UPDATE deliveries SET state='delivering',attempts=attempts+1,
                       lease_token=?,lease_expires_at=?,last_attempt_at=?,updated_at=?
                       WHERE delivery_id=? AND state IN ('queued','retry_wait')""",
                    (token, lease_until, now_s, now_s, row["delivery_id"]),
                )
                fresh = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (row["delivery_id"],)).fetchone()
                if fresh is not None and fresh["lease_token"] == token:
                    claimed.append(dict(fresh))
        return claimed

    def claim_delivery(self, delivery_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        now = _now()
        now_s = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        with self.transaction() as db:
            db.execute(
                """UPDATE deliveries
                   SET state='retry_wait',lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE delivery_id=? AND state='delivering'
                     AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""",
                (now_s, delivery_id, now_s),
            )
            row = db.execute(
                """SELECT * FROM deliveries
                   WHERE delivery_id=? AND state IN ('queued','retry_wait') AND available_at<=?""",
                (delivery_id, now_s),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            db.execute(
                """UPDATE deliveries SET state='delivering',attempts=attempts+1,
                   lease_token=?,lease_expires_at=?,last_attempt_at=?,updated_at=?
                   WHERE delivery_id=? AND state IN ('queued','retry_wait')""",
                (token, lease_until, now_s, now_s, delivery_id),
            )
            fresh = db.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if fresh is None or fresh["lease_token"] != token:
                return None
            return dict(fresh)

    def due_receipt_watches(self, *, limit: int = 100) -> list[dict[str, Any]]:
        now_s = _iso()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM deliveries
                   WHERE state IN ('sent','indeterminate') AND available_at<=?
                   ORDER BY available_at,created_at LIMIT ?""",
                (now_s, max(1, min(int(limit), 1000))),
            ).fetchall()
            return [dict(row) for row in rows]

    def defer_receipt_watch(self, delivery_id: str, *, seconds: int) -> dict[str, Any] | None:
        available = _iso(_now() + timedelta(seconds=max(1, int(seconds))))
        with self.transaction() as db:
            db.execute(
                """UPDATE deliveries SET available_at=?,updated_at=?
                   WHERE delivery_id=? AND state IN ('sent','indeterminate')""",
                (available, _iso(), delivery_id),
            )
            row = db.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def finish_attempt(self, delivery_id: str, lease_token: str, *, state: str, reason_code: str | None = None, retry_after_seconds: int | None = None) -> dict[str, Any]:
        allowed = {"sent", "retry_wait", "indeterminate", "acknowledged", "expired", "cancelled", "abandoned"}
        if state not in allowed:
            raise ValueError(f"invalid broker attempt state: {state}")
        now = _now()
        available = _iso(now + timedelta(seconds=max(0, int(retry_after_seconds or 0))))
        with self.transaction() as db:
            row = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if row is None:
                raise RuntimeError("broker delivery not found")
            if row["state"] != "delivering" or row["lease_token"] != lease_token:
                raise RuntimeError("broker delivery lease lost")
            db.execute(
                """UPDATE deliveries SET state=?,available_at=?,lease_token=NULL,lease_expires_at=NULL,
                   last_reason_code=?,updated_at=?,sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
                   acknowledged_at=CASE WHEN ?='acknowledged' THEN ? ELSE acknowledged_at END
                   WHERE delivery_id=?""",
                (state, available, reason_code, _iso(now), state, _iso(now), state, _iso(now), delivery_id),
            )
            db.execute(
                "INSERT INTO delivery_events(delivery_id,event_type,reason_code,created_at) VALUES(?,?,?,?)",
                (delivery_id, state, reason_code, _iso(now)),
            )
            fresh = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            assert fresh is not None
            return dict(fresh)

    def mirror_terminal_state(self, delivery_id: str, callback_state: str) -> None:
        mapping = {
            "acknowledged": "acknowledged",
            "expired": "expired",
            "cancelled": "cancelled",
            "abandoned": "abandoned",
            "sent": "sent",
            "delivery-indeterminate": "indeterminate",
        }
        state = mapping.get(callback_state)
        if not state:
            return
        with self.transaction() as db:
            db.execute(
                "UPDATE deliveries SET state=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE delivery_id=?",
                (state, _iso(), delivery_id),
            )


def deterministic_backoff(delivery_id: str, attempt: int, *, base: int = DEFAULT_BASE_BACKOFF_SECONDS, cap: int = DEFAULT_MAX_BACKOFF_SECONDS) -> int:
    exponent = min(max(attempt - 1, 0), 10)
    raw = min(cap, base * (2 ** exponent))
    jitter_span = max(1, min(15, raw // 4 or 1))
    digest = hashlib.sha256(f"{delivery_id}:{attempt}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") % jitter_span
    return min(cap, raw + jitter)
