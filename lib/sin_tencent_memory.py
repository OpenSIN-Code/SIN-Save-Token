"""Safe read-only adapter for TencentDB Agent Memory MemoryCore v3.

The adapter intentionally exposes only read operations. It never sends raw
conversations, persona/core requests, tool output, prompts, or write payloads.
Tencent MemoryCore remains an optional provider; OpenViking owns canonical durable semantic memory.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


DEFAULT_CONFIG = (
    Path(__file__).resolve().parent.parent / "config" / "tencent-memory.json"
)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SENSITIVE_QUERY_PATTERNS = (
    re.compile(
        r"(?i)\b(?:api[_-]?key|authorization|bearer|password|passwd|secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+"
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b"),
)


class TencentMemoryError(RuntimeError):
    """Base error for the optional Tencent Memory provider."""


class TencentMemoryDisabled(TencentMemoryError):
    """Raised when a network operation is attempted while disabled."""


class TencentMemorySecurityError(TencentMemoryError):
    """Raised when configuration or outbound data violates policy."""


@dataclass(frozen=True)
class TencentMemoryConfig:
    enabled: bool
    write_enabled: bool
    allow_remote: bool
    base_url: str
    timeout_seconds: int
    maximum_response_bytes: int
    team_id: str
    agent_id: str
    user_id: str
    api_key_env: str
    service_id: str
    allowed_read_endpoints: frozenset[str]
    allow_l0_conversation_access: bool
    allow_l3_persona_access: bool
    upstream_url: str
    assessed_commit: str
    api_generation: str

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_CONFIG,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "TencentMemoryConfig":
        env = os.environ if environ is None else environ
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        tenancy = raw.get("tenancy", {})
        auth = raw.get("auth", {})
        privacy = raw.get("privacy", {})
        upstream = raw.get("upstream", {})

        enabled = bool(raw.get("enabled", False))
        override = env.get("SIN_TENCENT_MEMORY_ENABLED")
        if override is not None:
            enabled = override.strip().lower() in {"1", "true", "yes", "on"}

        config = cls(
            enabled=enabled,
            write_enabled=bool(raw.get("write_enabled", False)),
            allow_remote=bool(raw.get("allow_remote", False)),
            base_url=str(raw.get("base_url", "http://127.0.0.1:8420")).rstrip("/"),
            timeout_seconds=int(raw.get("timeout_seconds", 8)),
            maximum_response_bytes=int(raw.get("maximum_response_bytes", 1_048_576)),
            team_id=env.get(
                "SIN_TENCENT_MEMORY_TEAM_ID", str(tenancy.get("team_id", "sin"))
            ),
            agent_id=env.get(
                "SIN_TENCENT_MEMORY_AGENT_ID",
                str(tenancy.get("agent_id", "sin-context")),
            ),
            user_id=env.get(
                "SIN_TENCENT_MEMORY_USER_ID", str(tenancy.get("user_id", "fleet"))
            ),
            api_key_env=str(auth.get("api_key_env", "TENCENT_MEMORY_API_KEY")),
            service_id=str(auth.get("service_id", "sin-save-token")),
            allowed_read_endpoints=frozenset(
                str(item) for item in privacy.get("allowed_read_endpoints", [])
            ),
            allow_l0_conversation_access=bool(
                privacy.get("allow_l0_conversation_access", False)
            ),
            allow_l3_persona_access=bool(privacy.get("allow_l3_persona_access", False)),
            upstream_url=str(upstream.get("url", "")),
            assessed_commit=str(upstream.get("assessed_commit", "")),
            api_generation=str(upstream.get("api_generation", "")),
        )
        config.validate_security_posture()
        return config

    def validate_security_posture(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise TencentMemorySecurityError("base_url must be an http(s) URL")

        is_loopback = parsed.hostname.lower() in LOOPBACK_HOSTS
        if not is_loopback:
            if not self.allow_remote:
                raise TencentMemorySecurityError(
                    "remote Tencent Memory endpoint is disabled"
                )
            if parsed.scheme != "https":
                raise TencentMemorySecurityError(
                    "remote Tencent Memory endpoint requires HTTPS"
                )

        if self.write_enabled:
            raise TencentMemorySecurityError(
                "Tencent writes are intentionally unsupported: MemoryCore v3 exposes conversation ingest, not a safe curated atomic-write API"
            )
        if self.allow_l0_conversation_access:
            raise TencentMemorySecurityError(
                "L0 conversation access must remain disabled"
            )
        if self.allow_l3_persona_access:
            raise TencentMemorySecurityError(
                "L3 persona/core access must remain disabled"
            )

        expected = {"/health", "/v3/atomic/search", "/v3/scenario/ls"}
        if self.allowed_read_endpoints != expected:
            raise TencentMemorySecurityError(
                "Tencent read endpoint allowlist drift detected"
            )
        if not (1 <= self.timeout_seconds <= 30):
            raise TencentMemorySecurityError("timeout_seconds must be between 1 and 30")
        if not (1024 <= self.maximum_response_bytes <= 4 * 1024 * 1024):
            raise TencentMemorySecurityError(
                "maximum_response_bytes outside safe bounds"
            )
        if self.api_generation != "v3":
            raise TencentMemorySecurityError("only MemoryCore v3 API is supported")
        if not re.fullmatch(r"[0-9a-f]{40}", self.assessed_commit):
            raise TencentMemorySecurityError(
                "upstream assessed_commit must be a pinned 40-char Git SHA"
            )

    @property
    def is_loopback(self) -> bool:
        parsed = urlparse(self.base_url)
        return bool(parsed.hostname and parsed.hostname.lower() in LOOPBACK_HOSTS)


class TencentMemoryClient:
    """Strict read-only MemoryCore v3 client."""

    def __init__(
        self,
        config: TencentMemoryConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.environ = os.environ if environ is None else environ

    def sanitized_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "write_supported": False,
            "write_enabled": False,
            "allow_remote": self.config.allow_remote,
            "base_url": self.config.base_url,
            "api_generation": self.config.api_generation,
            "assessed_commit": self.config.assessed_commit,
            "allowed_read_endpoints": sorted(self.config.allowed_read_endpoints),
            "l0_conversation_access": False,
            "l3_persona_access": False,
        }

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def search_atomic(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        self._validate_query(query)
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        return self._request(
            "POST",
            "/v3/atomic/search",
            {
                "team_id": self.config.team_id,
                "agent_id": self.config.agent_id,
                "user_id": self.config.user_id,
                "query": query,
                "limit": limit,
            },
        )

    def list_scenarios(self) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v3/scenario/ls",
            {
                "team_id": self.config.team_id,
                "agent_id": self.config.agent_id,
                "user_id": self.config.user_id,
            },
        )

    def _validate_query(self, query: str) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be non-empty")
        if len(query) > 1000:
            raise TencentMemorySecurityError(
                "query exceeds 1000-character outbound limit"
            )
        for pattern in SENSITIVE_QUERY_PATTERNS:
            if pattern.search(query):
                raise TencentMemorySecurityError(
                    "query appears to contain secret material"
                )

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        key = self.environ.get(self.config.api_key_env, "").strip()
        if not self.config.is_loopback and not key:
            raise TencentMemorySecurityError(
                "remote Tencent Memory endpoint requires an API key"
            )
        headers = {
            "Authorization": f"Bearer {key or 'local'}",
            "x-tdai-service-id": self.config.service_id,
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise TencentMemoryDisabled(
                "Tencent Memory provider is disabled; set SIN_TENCENT_MEMORY_ENABLED=1 for an explicit pilot"
            )
        if path not in self.config.allowed_read_endpoints:
            raise TencentMemorySecurityError(f"endpoint is not allowlisted: {path}")
        if method not in {"GET", "POST"}:
            raise TencentMemorySecurityError(
                "only read-oriented GET/POST requests are supported"
            )

        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            data=data,
            headers=self._headers(json_body=data is not None),
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                payload = response.read(self.config.maximum_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise TencentMemoryError(
                f"MemoryCore HTTP {error.code} for {path}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TencentMemoryError(
                f"MemoryCore request failed for {path}: {type(error).__name__}"
            ) from error

        if len(payload) > self.config.maximum_response_bytes:
            raise TencentMemorySecurityError(
                "MemoryCore response exceeds configured size limit"
            )
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TencentMemoryError("MemoryCore returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise TencentMemoryError("MemoryCore response root must be an object")

        code = decoded.get("code")
        if code is not None and code != 0:
            message = str(decoded.get("message", "request failed"))[:200]
            raise TencentMemoryError(f"MemoryCore v3 error code={code}: {message}")
        return decoded
