"""SIN callback broker service and loopback HTTP control plane."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .callback_broker import BROKER_SCHEMA_VERSION, TERMINAL_STATES, CallbackBrokerStore, deterministic_backoff

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 61369
DEFAULT_RECEIPT_WATCH_SECONDS = 30


def token_path() -> Path:
    override = os.getenv("SIN_CALLBACK_BROKER_TOKEN_FILE")
    path = Path(override).expanduser() if override else Path.home() / ".local" / "state" / "sin-orca" / "callback-broker.token"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def api_token() -> str:
    return token_path().read_text(encoding="utf-8").strip()


def sanitize_delivery(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "delivery_id", "relay_id", "repository_root", "callback_status", "transport",
        "target_id", "message_sha256", "expires_at", "state", "attempts",
        "available_at", "last_attempt_at", "last_reason_code", "created_at", "updated_at",
        "sent_at", "acknowledged_at",
    }
    return {key: row.get(key) for key in sorted(allowed)}


def deliver_claim(store: CallbackBrokerStore, row: dict[str, Any]) -> dict[str, Any]:
    """Delegate one already-authorized callback to canonical sin_orca delivery code.

    Resolve the non-secret relay_id back to the callback record. For OpenCode,
    canonical delivery code MUST first try direct exact-session delivery; Prime and
    DSH continue using their exact-session adapters. Never blindly resend an
    indeterminate delivery.
    """
    from .web_callbacks import (
        callback_status,
        relay_callback,
        resolve_callback_token_for_relay,
    )

    repository = Path(str(row["repository_root"])).resolve()
    token = resolve_callback_token_for_relay(repository, relay_id=str(row["relay_id"]))
    status = callback_status(repository=repository, token=token)
    store.mirror_terminal_state(str(row["delivery_id"]), str(status.get("status") or ""))
    refreshed = store.get(str(row["delivery_id"]))
    if refreshed and refreshed["state"] in {"acknowledged", "expired", "cancelled", "abandoned", "sent", "indeterminate"}:
        return sanitize_delivery(refreshed)

    result = relay_callback(repository=repository, token=token, scheduled=False)
    outcome = str(result.get("status") or "")
    if outcome in {"callback-sent", "callback-origin-reconciled"}:
        state, reason = "sent", "canonical-relay-sent"
        retry = None
    elif outcome == "callback-pending":
        state, reason = "retry_wait", str(result.get("delivery_reason") or "transport-offline")
        retry = deterministic_backoff(str(row["delivery_id"]), int(row["attempts"]))
    elif outcome in {"callback-delivery-indeterminate", "callback-awaiting-receipt"}:
        state, reason = "indeterminate", str(result.get("delivery_reason") or "delivery-indeterminate")
        retry = None
    elif outcome == "callback-expired":
        state, reason, retry = "expired", "callback-expired", None
    else:
        state, reason = "retry_wait", "unexpected-canonical-relay-result"
        retry = deterministic_backoff(str(row["delivery_id"]), int(row["attempts"]))
    return sanitize_delivery(
        store.finish_attempt(
            str(row["delivery_id"]),
            str(row["lease_token"]),
            state=state,
            reason_code=reason,
            retry_after_seconds=retry,
        )
    )


def sync_repository(store: CallbackBrokerStore, repository: Path) -> dict[str, Any]:
    """Idempotently rebuild transport rows from canonical callback JSON.

    Existing identities are required; this function never creates tokens or IDs.
    The repository registry is durable so a broker restart can discover callback
    records that were persisted immediately before an enqueue crash.
    """
    from .web_callbacks import callback_directory, DELIVERY_ID_PATTERN, RELAY_ID_PATTERN
    from .callback_transports import classify_callback_transport
    from .callback_broker import DeliveryRef
    counts = {"scanned": 0, "synced": 0, "already_present": 0, "skipped_inactive": 0,
              "skipped_invalid_identity": 0, "skipped_invalid_target": 0, "skipped_malformed": 0}
    root = repository.resolve()
    store.register_repository(root)
    for path in callback_directory(root).glob("*.json"):
        counts["scanned"] += 1
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["skipped_malformed"] += 1; continue
        if not isinstance(record, dict):
            counts["skipped_malformed"] += 1; continue
        if record.get("status") not in {"pending-delivery", "delivery-indeterminate", "sent"}:
            counts["skipped_inactive"] += 1; continue
        did, rid, digest = (str(record.get(k) or "") for k in ("delivery_id", "relay_id", "message_sha256"))
        if not DELIVERY_ID_PATTERN.fullmatch(did) or not RELAY_ID_PATTERN.fullmatch(rid) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            counts["skipped_invalid_identity"] += 1; continue
        try:
            transport, target = classify_callback_transport(record)
        except (RuntimeError, ValueError):
            counts["skipped_invalid_target"] += 1; continue
        before = store.get(did)
        store.enqueue(DeliveryRef(did, rid, str(root), str(record.get("callback_status") or ""), transport, target, digest, str(record.get("expires_at") or "")))
        store.mirror_terminal_state(did, str(record.get("status") or ""))
        counts["already_present" if before else "synced"] += 1
    store.mark_repository_synced(root)
    return counts


def drain_once(store: CallbackBrokerStore, *, limit: int = 10) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in store.claim_due(limit=limit):
        try:
            output.append(deliver_claim(store, row))
        except Exception:
            retry = deterministic_backoff(str(row["delivery_id"]), int(row["attempts"]))
            output.append(
                sanitize_delivery(
                    store.finish_attempt(
                        str(row["delivery_id"]),
                        str(row["lease_token"]),
                        state="retry_wait",
                        reason_code="broker-delivery-exception",
                        retry_after_seconds=retry,
                    )
                )
            )
    return output


def _watch_receipt(store: CallbackBrokerStore, row: dict[str, Any]) -> dict[str, Any]:
    """Reconcile ACK/TTL for a delivered or indeterminate callback without resend."""
    from .web_callbacks import callback_status, relay_callback, resolve_callback_token_for_relay

    delivery_id = str(row["delivery_id"])
    repository = Path(str(row["repository_root"])).resolve()
    try:
        token = resolve_callback_token_for_relay(repository, relay_id=str(row["relay_id"]))
        status = callback_status(repository=repository, token=token)
        store.mirror_terminal_state(delivery_id, str(status.get("status") or ""))
        current = store.get(delivery_id)
        if current is None:
            raise RuntimeError("broker delivery disappeared during receipt watch")
        if current["state"] in TERMINAL_STATES:
            return sanitize_delivery(current)

        # relay_callback is safe here because canonical sent/indeterminate states
        # never re-enter transport delivery; it only enforces canonical TTL and
        # returns awaiting-receipt until an ACK arrives.
        relay_callback(repository=repository, token=token, scheduled=False)
        status = callback_status(repository=repository, token=token)
        store.mirror_terminal_state(delivery_id, str(status.get("status") or ""))
        current = store.get(delivery_id)
        if current is None:
            raise RuntimeError("broker delivery disappeared after receipt reconcile")
        if current["state"] in {"sent", "indeterminate"}:
            current = store.defer_receipt_watch(
                delivery_id,
                seconds=DEFAULT_RECEIPT_WATCH_SECONDS,
            ) or current
        return sanitize_delivery(current)
    except Exception:
        current = store.defer_receipt_watch(
            delivery_id,
            seconds=DEFAULT_RECEIPT_WATCH_SECONDS,
        ) or store.get(delivery_id)
        if current is None:
            raise
        return sanitize_delivery(current)


def watch_receipts_once(store: CallbackBrokerStore, *, limit: int = 10) -> list[dict[str, Any]]:
    return [_watch_receipt(store, row) for row in store.due_receipt_watches(limit=limit)]


def reconcile_delivery(store: CallbackBrokerStore, delivery_id: str) -> dict[str, Any]:
    """Reconcile exactly one delivery ID without draining any other due row."""
    row = store.get(delivery_id)
    if row is None:
        raise KeyError(delivery_id)
    if row["state"] in TERMINAL_STATES:
        return sanitize_delivery(row)
    if row["state"] in {"sent", "indeterminate"}:
        return _watch_receipt(store, row)
    if row["state"] == "retry_wait":
        with store.transaction() as db:
            db.execute(
                "UPDATE deliveries SET available_at=?,updated_at=? WHERE delivery_id=? AND state='retry_wait'",
                (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), delivery_id),
            )
    claimed = store.claim_delivery(delivery_id)
    if claimed is None:
        current = store.get(delivery_id)
        if current is None:
            raise KeyError(delivery_id)
        return sanitize_delivery(current)
    return deliver_claim(store, claimed)


def run_cycle(store: CallbackBrokerStore, *, limit: int = 10) -> dict[str, Any]:
    repositories = store.list_repositories()
    sync_results: list[dict[str, Any]] = []
    for repository in repositories:
        try:
            sync_results.append({"repository": repository, "result": sync_repository(store, Path(repository))})
        except Exception:
            sync_results.append({"repository": repository, "result": {"error": "sync-failed"}})
    deliveries = drain_once(store, limit=limit)
    receipts = watch_receipts_once(store, limit=limit)
    return {
        "repositories": len(repositories),
        "sync": sync_results,
        "deliveries": deliveries,
        "receipts": receipts,
    }


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "sin-callback-broker/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    @property
    def store(self) -> CallbackBrokerStore:
        return self.server.store  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return secrets.compare_digest(auth, f"Bearer {api_token()}")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_object(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > 64 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("invalid JSON body") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path == "/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "sin-callback-broker", "schema": BROKER_SCHEMA_VERSION})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        if route.path == "/status":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "sin-callback-broker",
                    "schema": BROKER_SCHEMA_VERSION,
                    "repositories": len(self.store.list_repositories()),
                    "states": self.store.state_counts(),
                },
            )
            return
        if route.path == "/callbacks":
            query = parse_qs(route.query)
            state = query.get("state", [None])[0]
            self._json(HTTPStatus.OK, {"ok": True, "callbacks": [sanitize_delivery(row) for row in self.store.list(state=state)]})
            return
        if route.path.startswith("/callbacks/"):
            delivery_id = route.path.split("/", 2)[2]
            row = self.store.get(delivery_id)
            if row is None:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not-found"})
            else:
                self._json(HTTPStatus.OK, {"ok": True, "callback": sanitize_delivery(row)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not-found"})

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        if route.path == "/callbacks/drain":
            try:
                body = self._read_json_object()
                limit = int(body.get("limit", 10))
                if limit < 1 or limit > 100:
                    raise ValueError("limit must be between 1 and 100")
            except (TypeError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                return
            self._json(
                HTTPStatus.OK,
                {"ok": True, "processed": run_cycle(self.store, limit=limit)},
            )
            return
        if route.path.startswith("/callbacks/") and route.path.endswith("/reconcile"):
            parts = [part for part in route.path.split("/") if part]
            if len(parts) != 3:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not-found"})
                return
            delivery_id = parts[1]
            if self.store.get(delivery_id) is None:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not-found"})
                return
            self._json(
                HTTPStatus.OK,
                {"ok": True, "processed": reconcile_delivery(self.store, delivery_id)},
            )
            return
        if route.path == "/repositories/sync":
            try:
                body = self._read_json_object()
                rendered = str(body.get("repository") or "").strip()
                if not rendered:
                    raise ValueError("repository is required")
                repository = Path(rendered).expanduser().resolve()
                if not repository.is_dir():
                    raise ValueError("repository directory does not exist")
                result = sync_repository(self.store, repository)
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                return
            self._json(HTTPStatus.OK, {"ok": True, "sync": result})
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not-found"})


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, interval_seconds: float = 5.0) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("callback broker must bind loopback")
    store = CallbackBrokerStore()
    server = ThreadingHTTPServer((host, port), BrokerHandler)
    server.store = store  # type: ignore[attr-defined]
    stop = threading.Event()

    def drain_loop() -> None:
        while not stop.wait(interval_seconds):
            run_cycle(store)

    worker = threading.Thread(target=drain_loop, name="sin-callback-drain", daemon=True)
    worker.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=2)

