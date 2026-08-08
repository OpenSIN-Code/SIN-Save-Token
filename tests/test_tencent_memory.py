#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from sin_tencent_memory import (  # noqa: E402
    TencentMemoryClient,
    TencentMemoryConfig,
    TencentMemoryDisabled,
    TencentMemorySecurityError,
)


CONFIG = ROOT / "config" / "tencent-memory.json"


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


class TencentMemoryTests(unittest.TestCase):
    def test_repository_default_is_hard_read_only_and_disabled(self):
        config = TencentMemoryConfig.load(CONFIG, environ={})
        self.assertFalse(config.enabled)
        self.assertFalse(config.write_enabled)
        self.assertFalse(config.allow_remote)
        self.assertFalse(config.allow_l0_conversation_access)
        self.assertFalse(config.allow_l3_persona_access)
        self.assertEqual(
            config.allowed_read_endpoints,
            frozenset({"/health", "/v3/atomic/search", "/v3/scenario/ls"}),
        )

    def test_no_write_or_conversation_api_is_exposed(self):
        public = {name for name in dir(TencentMemoryClient) if not name.startswith("_")}
        self.assertNotIn("write", public)
        self.assertNotIn("conversation_add", public)
        self.assertNotIn("core_read", public)
        self.assertIn("search_atomic", public)
        self.assertIn("list_scenarios", public)

    def test_disabled_provider_refuses_network_access(self):
        config = TencentMemoryConfig.load(CONFIG, environ={})
        client = TencentMemoryClient(config, environ={})
        with self.assertRaises(TencentMemoryDisabled):
            client.search_atomic("architecture decision")

    def test_secret_shaped_query_is_rejected_before_network(self):
        config = TencentMemoryConfig.load(
            CONFIG,
            environ={"SIN_TENCENT_MEMORY_ENABLED": "1"},
        )
        client = TencentMemoryClient(config, environ={})
        with self.assertRaises(TencentMemorySecurityError):
            client.search_atomic("Authorization: Bearer abcdef123456")

    def test_remote_http_endpoint_is_rejected_even_when_remote_is_allowed(self):
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["base_url"] = "http://memory.example.test:8420"
        raw["allow_remote"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(TencentMemorySecurityError):
                TencentMemoryConfig.load(path, environ={})

    def test_search_uses_only_atomic_v3_endpoint_and_tenant_fields(self):
        config = TencentMemoryConfig.load(
            CONFIG,
            environ={"SIN_TENCENT_MEMORY_ENABLED": "1"},
        )
        client = TencentMemoryClient(config, environ={})
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"code": 0, "message": "ok", "data": {"items": []}})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.search_atomic("canonical memory", limit=3)

        self.assertEqual(result["code"], 0)
        self.assertEqual(captured["url"], "http://127.0.0.1:8420/v3/atomic/search")
        self.assertEqual(captured["body"]["team_id"], "sin")
        self.assertEqual(captured["body"]["agent_id"], "sin-context")
        self.assertEqual(captured["body"]["user_id"], "fleet")
        self.assertEqual(captured["body"]["query"], "canonical memory")
        self.assertEqual(captured["body"]["limit"], 3)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer local")

    def test_write_enabled_config_is_rejected(self):
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["write_enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(TencentMemorySecurityError):
                TencentMemoryConfig.load(path, environ={})


if __name__ == "__main__":
    unittest.main()
