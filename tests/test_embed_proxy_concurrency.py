from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "cognee-embed-proxy.py"


def load_module():
    spec = importlib.util.spec_from_file_location("cognee_embed_proxy_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_embedding_serializes_shared_onnx_session(monkeypatch) -> None:
    module = load_module()
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    class FakeEngine:
        def embed(self, texts):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                time.sleep(0.03)
                return [[0.0] * module.DIMS for _ in texts]
            finally:
                with state_lock:
                    state["active"] -= 1

    fake_fastembed = types.ModuleType("fastembed")
    fake_fastembed.TextEmbedding = object
    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    module._fallback_engine = FakeEngine()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda value: module._local_embed([value]), ["a", "b", "c", "d"]))

    assert state["max_active"] == 1
    assert all(len(result) == 1 and len(result[0]) == module.DIMS for result in results)
