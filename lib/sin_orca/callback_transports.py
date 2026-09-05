"""Exact-session callback transport adapters for the SIN callback broker."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlparse

OPENCODE_SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
DSH_SESSION_RE = re.compile(r"^session-[0-9a-fA-F-]{36}$")


@dataclass(frozen=True)
class TransportResult:
    state: str  # sent | retry_wait | indeterminate
    reason_code: str
    receipt: str | None = None


def _opencode_api_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    password = os.getenv("OPENCODE_SERVER_PASSWORD")
    if password:
        username = os.getenv("OPENCODE_SERVER_USERNAME") or "opencode"
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def _deliver_opencode_via_api(*, repository: Path, session_id: str, message: str, timeout_seconds: int) -> TransportResult:
    raw_url = str(os.getenv("SIN_OPENCODE_CALLBACK_URL") or "").strip().rstrip("/")
    parsed = urlparse(raw_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return TransportResult("retry_wait", "opencode-api-not-loopback")
    opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
    headers = _opencode_api_headers()
    session_url = f"{raw_url}/session/{quote(session_id, safe='')}"
    try:
        request = urllib_request.Request(session_url, headers=headers, method="GET")
        with opener.open(request, timeout=min(timeout_seconds, 10)) as response:
            session = json.load(response)
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return TransportResult("retry_wait", "opencode-exact-session-api-unavailable")
    if not isinstance(session, dict) or session.get("id") != session_id:
        return TransportResult("retry_wait", "opencode-exact-session-api-mismatch")
    directory = session.get("directory")
    if not isinstance(directory, str) or Path(directory).expanduser().resolve() != repository.resolve():
        return TransportResult("retry_wait", "opencode-exact-session-api-repository-mismatch")

    body = json.dumps({"parts": [{"type": "text", "text": message}]}).encode("utf-8")
    request = urllib_request.Request(
        f"{session_url}/prompt_async",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
    except urllib_error.HTTPError as error:
        if 400 <= int(error.code) < 500:
            return TransportResult("retry_wait", f"opencode-exact-session-api-http-{error.code}")
        return TransportResult("indeterminate", "opencode-exact-session-api-indeterminate")
    except (urllib_error.URLError, TimeoutError, OSError):
        return TransportResult("indeterminate", "opencode-exact-session-api-indeterminate")
    if 200 <= status < 300:
        return TransportResult("sent", "opencode-exact-session-api-delivered", "http-accepted")
    return TransportResult("indeterminate", "opencode-exact-session-api-indeterminate")


def deliver_opencode_exact_session(*, repository: Path, session_id: str, message: str, timeout_seconds: int = 90) -> TransportResult:
    """Deliver to the exact persisted OpenCode session without requiring a TUI terminal.

    If a loopback OpenCode server is explicitly configured, verify the exact
    session/repository identity and use its asynchronous prompt endpoint. Once the
    POST boundary is crossed, ambiguous failures are indeterminate and never fall
    through to CLI delivery. Without an API endpoint, use the exact-session CLI.
    """
    if not OPENCODE_SESSION_RE.fullmatch(session_id):
        raise ValueError("invalid OpenCode session ID")
    if str(os.getenv("SIN_OPENCODE_CALLBACK_URL") or "").strip():
        return _deliver_opencode_via_api(
            repository=repository,
            session_id=session_id,
            message=message,
            timeout_seconds=timeout_seconds,
        )

    binary = shutil.which("opencode")
    if not binary:
        return TransportResult("retry_wait", "opencode-cli-unavailable")
    try:
        process = subprocess.run(
            [
                binary,
                "run",
                "--session",
                session_id,
                "--format",
                "json",
                "--dir",
                str(repository.resolve()),
                message,
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in {"SIN_MANIFEST_HMAC_KEY", "SIN_CALLBACK_BROKER_TOKEN"}
            },
        )
    except subprocess.TimeoutExpired:
        return TransportResult("indeterminate", "opencode-exact-session-timeout")
    except OSError:
        return TransportResult("retry_wait", "opencode-exact-session-unavailable")
    if process.returncode != 0:
        material = (process.stderr or process.stdout or "").casefold()
        if any(marker in material for marker in ("not found", "no session", "unknown session")):
            return TransportResult("retry_wait", "opencode-exact-session-offline")
        return TransportResult("indeterminate", "opencode-exact-session-nonzero")
    # Do not persist raw stdout. A zero exit from exact-session run is sufficient
    # transport evidence; callback ACK still remains the processing receipt.
    return TransportResult("sent", "opencode-exact-session-delivered", "process-exit-0")


def classify_callback_transport(record: dict[str, Any]) -> tuple[str, str]:
    transport = record.get("origin_transport")
    if isinstance(transport, dict) and transport.get("transport") == "prime-agent":
        target = transport.get("active_session_id")
        if not isinstance(target, str) or not target:
            raise RuntimeError("invalid Prime Agent callback target")
        return "prime-agent", target
    if isinstance(transport, dict) and transport.get("transport") == "deepseek-harness":
        target = transport.get("session_id")
        if not isinstance(target, str) or not DSH_SESSION_RE.fullmatch(target):
            raise RuntimeError("invalid DeepSeek Harness callback target")
        return "deepseek-harness", target
    session = record.get("origin_session")
    target = session.get("id") if isinstance(session, dict) else None
    if not isinstance(target, str) or not OPENCODE_SESSION_RE.fullmatch(target):
        raise RuntimeError("OpenCode callback has no exact session identity")
    return "opencode", target

