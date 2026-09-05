from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from unittest.mock import patch

import pytest

from sin_orca.callback_broker import CallbackBrokerStore, DeliveryRef, deterministic_backoff
from sin_orca import callback_broker_service as broker_service
from sin_orca.callback_broker_service import drain_once
from sin_orca.callback_transports import deliver_opencode_exact_session


def ref(tmp_path: Path, delivery: str = "gptwcd_" + "a" * 32) -> DeliveryRef:
    return DeliveryRef(
        delivery_id=delivery,
        relay_id="gptwcr_" + "b" * 32,
        repository_root=str(tmp_path),
        callback_status="done",
        transport="opencode",
        target_id="ses_EXACT123",
        message_sha256="c" * 64,
        expires_at="2035-01-01T00:00:00+00:00",
    )


def test_broker_uses_wal_and_transport_only_schema(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    with store._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
    assert "delivery_id" in columns
    assert "relay_id" in columns
    assert "callback_token" not in columns
    assert "message" not in columns
    assert "summary" not in columns
    assert "completion_report" not in columns


def test_enqueue_is_idempotent_but_identity_mismatch_fails(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    first = store.enqueue(ref(tmp_path))
    second = store.enqueue(ref(tmp_path))
    assert first["delivery_id"] == second["delivery_id"]
    bad = DeliveryRef(**{**ref(tmp_path).__dict__, "target_id": "ses_WRONG123"})
    with pytest.raises(RuntimeError, match="identity mismatch"):
        store.enqueue(bad)


def test_claim_is_exclusive_and_expired_lease_is_reclaimable(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    store.enqueue(ref(tmp_path))
    first = store.claim_due(limit=1, lease_seconds=60)
    assert len(first) == 1
    assert store.claim_due(limit=1, lease_seconds=60) == []
    with store.transaction() as db:
        db.execute("UPDATE deliveries SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (first[0]["delivery_id"],))
    second = store.claim_due(limit=1, lease_seconds=60)
    assert len(second) == 1
    assert second[0]["lease_token"] != first[0]["lease_token"]


def test_lost_lease_cannot_complete_delivery(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    store.enqueue(ref(tmp_path))
    row = store.claim_due(limit=1)[0]
    with pytest.raises(RuntimeError, match="lease lost"):
        store.finish_attempt(row["delivery_id"], "wrong", state="sent")


def test_backoff_is_deterministic_bounded_and_increases() -> None:
    values = [deterministic_backoff("gptwcd_" + "d" * 32, i) for i in range(1, 8)]
    assert values == [deterministic_backoff("gptwcd_" + "d" * 32, i) for i in range(1, 8)]
    assert values[0] >= 5
    assert max(values) <= 15 * 60
    assert values[-1] >= values[0]


def test_exact_opencode_transport_never_guesses_session(tmp_path: Path) -> None:
    completed = __import__("subprocess").CompletedProcess([], 0, "{}\n", "")
    with patch("sin_orca.callback_transports.shutil.which", return_value="/opt/homebrew/bin/opencode"), patch("sin_orca.callback_transports.subprocess.run", return_value=completed) as run:
        result = deliver_opencode_exact_session(repository=tmp_path, session_id="ses_EXACT123", message="delivery=gptwcd_test")
    assert result.state == "sent"
    argv = run.call_args.args[0]
    assert argv[argv.index("--session") + 1] == "ses_EXACT123"
    assert "--continue" not in argv


def test_opencode_timeout_is_indeterminate_not_retry(tmp_path: Path) -> None:
    import subprocess
    with patch("sin_orca.callback_transports.shutil.which", return_value="opencode"), patch("sin_orca.callback_transports.subprocess.run", side_effect=subprocess.TimeoutExpired("opencode", 1)):
        result = deliver_opencode_exact_session(repository=tmp_path, session_id="ses_EXACT123", message="x", timeout_seconds=1)
    assert result.state == "indeterminate"


def test_db_contains_no_raw_capability_or_message(tmp_path: Path) -> None:
    path = tmp_path / "broker.sqlite3"
    store = CallbackBrokerStore(path)
    store.enqueue(ref(tmp_path))
    raw = path.read_bytes()
    assert b"gptwcb_" not in raw
    assert b"private summary" not in raw

def test_retry_lifetime_is_governed_by_callback_ttl_not_attempt_count(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    store.enqueue(ref(tmp_path))
    for _ in range(30):
        claimed = store.claim_due(limit=1)
        assert len(claimed) == 1
        row = claimed[0]
        store.finish_attempt(
            row["delivery_id"],
            row["lease_token"],
            state="retry_wait",
            retry_after_seconds=0,
        )
    assert store.get(ref(tmp_path).delivery_id)["attempts"] == 30


def test_past_due_callback_is_claimed_for_canonical_expiry_instead_of_locally_expired(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    expired = DeliveryRef(**{**ref(tmp_path).__dict__, "expires_at": "2000-01-01T00:00:00+00:00"})
    store.enqueue(expired)
    claimed = store.claim_due(limit=1)
    assert len(claimed) == 1
    assert claimed[0]["state"] == "delivering"


def test_restart_persistence_keeps_due_delivery(tmp_path: Path) -> None:
    db = tmp_path / "broker.sqlite3"
    first = CallbackBrokerStore(db)
    first.enqueue(ref(tmp_path))
    second = CallbackBrokerStore(db)
    claimed = second.claim_due(limit=1)
    assert [row["delivery_id"] for row in claimed] == [ref(tmp_path).delivery_id]


def test_repository_registry_is_idempotent_and_persistent(tmp_path: Path) -> None:
    db = tmp_path / "broker.sqlite3"
    first = CallbackBrokerStore(db)
    first.register_repository(tmp_path)
    first.register_repository(tmp_path)
    second = CallbackBrokerStore(db)
    assert second.list_repositories() == [str(tmp_path.resolve())]


def test_claim_delivery_never_claims_a_different_due_row(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    first = ref(tmp_path, "gptwcd_" + "1" * 32)
    second = DeliveryRef(**{**ref(tmp_path, "gptwcd_" + "2" * 32).__dict__, "relay_id": "gptwcr_" + "2" * 32})
    store.enqueue(first)
    store.enqueue(second)
    claimed = store.claim_delivery(second.delivery_id)
    assert claimed is not None
    assert claimed["delivery_id"] == second.delivery_id
    assert store.get(first.delivery_id)["state"] == "queued"


def test_sent_and_indeterminate_rows_are_receipt_watch_candidates_not_delivery_claims(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    sent = ref(tmp_path, "gptwcd_" + "3" * 32)
    uncertain = DeliveryRef(**{**ref(tmp_path, "gptwcd_" + "4" * 32).__dict__, "relay_id": "gptwcr_" + "4" * 32})
    store.enqueue(sent)
    store.enqueue(uncertain)
    for delivery_id, state in ((sent.delivery_id, "sent"), (uncertain.delivery_id, "indeterminate")):
        row = store.claim_delivery(delivery_id)
        assert row is not None
        store.finish_attempt(delivery_id, row["lease_token"], state=state)
    assert store.claim_due(limit=10) == []
    assert {row["delivery_id"] for row in store.due_receipt_watches(limit=10)} == {sent.delivery_id, uncertain.delivery_id}


def test_reconcile_delivery_claims_only_requested_id(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    first = ref(tmp_path, "gptwcd_" + "5" * 32)
    second = DeliveryRef(**{**ref(tmp_path, "gptwcd_" + "6" * 32).__dict__, "relay_id": "gptwcr_" + "6" * 32})
    store.enqueue(first)
    store.enqueue(second)
    seen: list[str] = []

    def fake_deliver(_store: CallbackBrokerStore, row: dict) -> dict:
        seen.append(str(row["delivery_id"]))
        return {"delivery_id": row["delivery_id"], "state": "sent"}

    with patch.object(broker_service, "deliver_claim", side_effect=fake_deliver):
        result = broker_service.reconcile_delivery(store, second.delivery_id)
    assert result["delivery_id"] == second.delivery_id
    assert seen == [second.delivery_id]
    assert store.get(first.delivery_id)["state"] == "queued"


def test_sync_repository_registers_repo_for_restart_recovery(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    with patch("sin_orca.web_callbacks.callback_directory", return_value=tmp_path / "callbacks"):
        (tmp_path / "callbacks").mkdir()
        broker_service.sync_repository(store, tmp_path)
    assert store.list_repositories() == [str(tmp_path.resolve())]


def test_receipt_watch_reconciles_sent_without_retransmission(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    item = ref(tmp_path, "gptwcd_" + "7" * 32)
    store.enqueue(item)
    claimed = store.claim_delivery(item.delivery_id)
    assert claimed is not None
    store.finish_attempt(item.delivery_id, claimed["lease_token"], state="sent")

    with patch("sin_orca.web_callbacks.resolve_callback_token_for_relay", return_value="gptwcb_" + "8" * 32), patch(
        "sin_orca.web_callbacks.callback_status",
        return_value={"status": "sent", "expired": False},
    ), patch(
        "sin_orca.web_callbacks.relay_callback",
        return_value={"status": "callback-awaiting-receipt"},
    ) as relay:
        watched = broker_service.watch_receipts_once(store, limit=10)

    assert [row["delivery_id"] for row in watched] == [item.delivery_id]
    assert store.get(item.delivery_id)["state"] == "sent"
    relay.assert_called_once()


def test_run_cycle_syncs_registered_repositories_before_delivery(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    store.register_repository(tmp_path)
    order: list[str] = []
    with patch.object(broker_service, "sync_repository", side_effect=lambda _store, _repo: order.append("sync") or {"scanned": 0}), patch.object(
        broker_service, "drain_once", side_effect=lambda _store, limit=10: order.append("drain") or []
    ), patch.object(
        broker_service, "watch_receipts_once", side_effect=lambda _store, limit=10: order.append("watch") or []
    ):
        result = broker_service.run_cycle(store, limit=10)
    assert order == ["sync", "drain", "watch"]
    assert result["repositories"] == 1


def _broker_http_server(store: CallbackBrokerStore) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), broker_service.BrokerHandler)
    server.store = store  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _broker_request(server: ThreadingHTTPServer, path: str, *, method: str = "GET", token: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib_request.Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=(json.dumps(body or {}).encode("utf-8") if method != "GET" else None),
        headers=headers,
        method=method,
    )
    return urllib_request.build_opener(urllib_request.ProxyHandler({})).open(request, timeout=3)


def test_api_health_is_public_and_contains_no_sensitive_state(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    server, thread = _broker_http_server(store)
    try:
        with _broker_request(server, "/health") as response:
            payload = json.load(response)
        assert payload == {"ok": True, "schema": 2, "service": "sin-callback-broker"}
        assert "token" not in json.dumps(payload).casefold()
        assert "repository" not in json.dumps(payload).casefold()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_api_callback_endpoints_require_bearer_token(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    server, thread = _broker_http_server(store)
    token_file = tmp_path / "broker.token"
    with patch.dict(os.environ, {"SIN_CALLBACK_BROKER_TOKEN_FILE": str(token_file)}):
        token = broker_service.api_token()
        try:
            with pytest.raises(urllib_error.HTTPError) as missing:
                _broker_request(server, "/callbacks")
            assert missing.value.code == 401
            with _broker_request(server, "/callbacks", token=token) as response:
                assert json.load(response)["ok"] is True
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_api_reconcile_routes_exact_delivery_id(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    item = ref(tmp_path, "gptwcd_" + "9" * 32)
    store.enqueue(item)
    server, thread = _broker_http_server(store)
    token_file = tmp_path / "broker.token"
    with patch.dict(os.environ, {"SIN_CALLBACK_BROKER_TOKEN_FILE": str(token_file)}):
        token = broker_service.api_token()
        with patch.object(broker_service, "reconcile_delivery", return_value={"delivery_id": item.delivery_id, "state": "sent"}) as reconcile:
            try:
                with _broker_request(server, f"/callbacks/{item.delivery_id}/reconcile", method="POST", token=token) as response:
                    payload = json.load(response)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert payload["processed"]["delivery_id"] == item.delivery_id
    reconcile.assert_called_once_with(store, item.delivery_id)


def test_api_status_is_authenticated_and_reports_only_operational_counts(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    store.register_repository(tmp_path)
    store.enqueue(ref(tmp_path))
    server, thread = _broker_http_server(store)
    token_file = tmp_path / "broker.token"
    with patch.dict(os.environ, {"SIN_CALLBACK_BROKER_TOKEN_FILE": str(token_file)}):
        token = broker_service.api_token()
        try:
            with _broker_request(server, "/status", token=token) as response:
                payload = json.load(response)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert payload["ok"] is True
    assert payload["schema"] == 2
    assert payload["repositories"] == 1
    assert payload["states"]["queued"] == 1
    rendered = json.dumps(payload)
    assert "gptwcb_" not in rendered
    assert "lease_token" not in rendered


def test_api_repository_sync_is_authenticated(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    server, thread = _broker_http_server(store)
    token_file = tmp_path / "broker.token"
    with patch.dict(os.environ, {"SIN_CALLBACK_BROKER_TOKEN_FILE": str(token_file)}):
        token = broker_service.api_token()
        with patch.object(broker_service, "sync_repository", return_value={"scanned": 0, "synced": 0}) as sync:
            try:
                with _broker_request(server, "/repositories/sync", method="POST", token=token, body={"repository": str(tmp_path)}) as response:
                    payload = json.load(response)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert payload["ok"] is True
    sync.assert_called_once_with(store, tmp_path.resolve())


def test_opencode_prefers_exact_loopback_session_api(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        posted: dict | None = None
        authorization = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            Handler.authorization = self.headers.get("Authorization", "")
            body = json.dumps({"id": "ses_EXACT123", "directory": str(tmp_path.resolve())}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            Handler.posted = json.loads(self.rfile.read(length))
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with patch.dict(os.environ, {
            "SIN_OPENCODE_CALLBACK_URL": url,
            "OPENCODE_SERVER_USERNAME": "callback-user",
            "OPENCODE_SERVER_PASSWORD": "secret-test-value",
        }, clear=False), patch("sin_orca.callback_transports.subprocess.run") as run:
            result = deliver_opencode_exact_session(
                repository=tmp_path,
                session_id="ses_EXACT123",
                message="delivery=gptwcd_test",
            )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert result.state == "sent"
    assert result.reason_code == "opencode-exact-session-api-delivered"
    assert Handler.posted == {"parts": [{"type": "text", "text": "delivery=gptwcd_test"}]}
    expected = base64.b64encode(b"callback-user:secret-test-value").decode("ascii")
    assert Handler.authorization == f"Basic {expected}"
    run.assert_not_called()


def test_opencode_api_post_timeout_is_indeterminate_and_never_falls_back(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"id": "ses_EXACT123", "directory": str(tmp_path.resolve())}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            self.connection.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with patch.dict(os.environ, {"SIN_OPENCODE_CALLBACK_URL": url}, clear=False), patch("sin_orca.callback_transports.subprocess.run") as run:
            result = deliver_opencode_exact_session(
                repository=tmp_path,
                session_id="ses_EXACT123",
                message="delivery=gptwcd_test",
                timeout_seconds=1,
            )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert result.state == "indeterminate"
    assert result.reason_code == "opencode-exact-session-api-indeterminate"
    run.assert_not_called()


def test_opencode_rejects_non_loopback_callback_api(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"SIN_OPENCODE_CALLBACK_URL": "https://example.com"}, clear=False), patch("sin_orca.callback_transports.subprocess.run") as run:
        result = deliver_opencode_exact_session(
            repository=tmp_path,
            session_id="ses_EXACT123",
            message="delivery=gptwcd_test",
        )
    assert result.state == "retry_wait"
    assert result.reason_code == "opencode-api-not-loopback"
    run.assert_not_called()


def test_schema_v1_migrates_in_place_without_losing_delivery(tmp_path: Path) -> None:
    db_path = tmp_path / "broker.sqlite3"
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            CREATE TABLE broker_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO broker_meta(key,value) VALUES('schema_version','1');
            CREATE TABLE deliveries(
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
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            """INSERT INTO deliveries(
                 delivery_id,relay_id,repository_root,callback_status,transport,target_id,
                 message_sha256,expires_at,state,attempts,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "gptwcd_" + "e" * 32,
                "gptwcr_" + "f" * 32,
                str(tmp_path),
                "done",
                "opencode",
                "ses_EXACT123",
                "a" * 64,
                "2035-01-01T00:00:00+00:00",
                "queued",
                0,
                "2026-08-28T00:00:00+00:00",
                "2026-08-28T00:00:00+00:00",
            ),
        )
        db.commit()
    finally:
        db.close()

    store = CallbackBrokerStore(db_path)
    migrated = store.get("gptwcd_" + "e" * 32)
    assert migrated is not None
    assert migrated["state"] == "queued"
    assert migrated["available_at"]
    assert "lease_token" in migrated
    with store._connect() as migrated_db:
        assert migrated_db.execute(
            "SELECT value FROM broker_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_schema_init_backfills_repository_registry_from_existing_deliveries(tmp_path: Path) -> None:
    db_path = tmp_path / "broker.sqlite3"
    store = CallbackBrokerStore(db_path)
    item = ref(tmp_path)
    store.enqueue(item)
    with store.transaction() as db:
        db.execute("DELETE FROM repositories")
    restarted = CallbackBrokerStore(db_path)
    assert restarted.list_repositories() == [str(tmp_path.resolve())]


def test_api_drain_honors_bounded_requested_limit(tmp_path: Path) -> None:
    store = CallbackBrokerStore(tmp_path / "broker.sqlite3")
    server, thread = _broker_http_server(store)
    token_file = tmp_path / "broker.token"
    with patch.dict(os.environ, {"SIN_CALLBACK_BROKER_TOKEN_FILE": str(token_file)}):
        token = broker_service.api_token()
        with patch.object(broker_service, "run_cycle", return_value={"deliveries": []}) as cycle:
            try:
                with _broker_request(
                    server,
                    "/callbacks/drain",
                    method="POST",
                    token=token,
                    body={"limit": 7},
                ) as response:
                    payload = json.load(response)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert payload["ok"] is True
    cycle.assert_called_once_with(store, limit=7)


# Remaining Prime/DSH and canonical-callback coverage is added below.

