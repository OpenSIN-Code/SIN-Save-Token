#!/usr/bin/env python3
"""OpenAI-compatible embedding proxy: Gemini first, local mxbai fallback.

Why a proxy:
  Cognee only speaks one EMBEDDING_* backend. Automatic rate-limit fallback
  needs a single endpoint that tries Gemini, then local fastembed.

Dim contract:
  Both paths MUST return the same dimension (default 1024) so Lance vectors
  stay compatible. Gemini uses output_dimensionality; local uses mxbai-large.

Auth for Cognee: any Bearer token accepted (local only). Gemini key is loaded
from ~/.cognee-plugin/secrets/gemini_api_key (chmod 600) — never from argv.

Env:
  COGNEE_EMBED_PROXY_PORT   default 8012
  COGNEE_EMBED_PROXY_HOST   default 127.0.0.1
  GEMINI_API_KEY_FILE       default ~/.cognee-plugin/secrets/gemini_api_key
  GEMINI_EMBED_MODEL        default gemini-embedding-001
  EMBEDDING_DIMENSIONS      default 1024
  COGNEE_FALLBACK_EMBED_MODEL  default mixedbread-ai/mxbai-embed-large-v1
  COGNEE_EMBED_FORCE_LOCAL  1 = skip Gemini
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("COGNEE_EMBED_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("COGNEE_EMBED_PROXY_PORT", "8012"))
DIMS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))
GEMINI_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
FALLBACK_MODEL = os.environ.get(
    "COGNEE_FALLBACK_EMBED_MODEL", "mixedbread-ai/mxbai-embed-large-v1"
)
KEY_FILE = Path(
    os.environ.get(
        "GEMINI_API_KEY_FILE",
        str(Path.home() / ".cognee-plugin" / "secrets" / "gemini_api_key"),
    )
).expanduser()
FORCE_LOCAL = os.environ.get("COGNEE_EMBED_FORCE_LOCAL", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

_stats = {"gemini_ok": 0, "fallback_ok": 0, "errors": 0, "last_backend": None}
_fallback_engine = None


def _log(msg: str) -> None:
    print(f"[embed-proxy] {msg}", file=sys.stderr, flush=True)


def _load_gemini_key() -> str:
    if not KEY_FILE.is_file():
        return ""
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _as_list(inp: Any) -> list[str]:
    if inp is None:
        return []
    if isinstance(inp, str):
        return [inp]
    if isinstance(inp, list):
        out = []
        for x in inp:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict) and "text" in x:
                out.append(str(x["text"]))
            else:
                out.append(str(x))
        return out
    return [str(inp)]


def _gemini_embed(texts: list[str], key: str) -> list[list[float]]:
    """Call Gemini embedContent / batchEmbedContents; return vectors of len DIMS."""
    if not texts:
        return []
    # Prefer batch for multi, single for one
    if len(texts) == 1:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:embedContent"
        )
        body = {
            "content": {"parts": [{"text": texts[0]}]},
            "outputDimensionality": DIMS,
            "taskType": "RETRIEVAL_DOCUMENT",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        emb = (data.get("embedding") or {}).get("values")
        if not emb:
            raise RuntimeError(f"gemini empty embedding: keys={list(data.keys())}")
        return [_normalize_dim(emb)]

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:batchEmbedContents"
    )
    body = {
        "requests": [
            {
                "model": f"models/{GEMINI_MODEL}",
                "content": {"parts": [{"text": t}]},
                "outputDimensionality": DIMS,
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            for t in texts
        ]
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    embeddings = data.get("embeddings") or []
    if len(embeddings) != len(texts):
        raise RuntimeError(
            f"gemini batch size mismatch got={len(embeddings)} want={len(texts)}"
        )
    out = []
    for e in embeddings:
        vals = e.get("values") if isinstance(e, dict) else None
        if not vals:
            raise RuntimeError("gemini batch empty vector")
        out.append(_normalize_dim(vals))
    return out


def _normalize_dim(vec: list[float]) -> list[float]:
    if len(vec) == DIMS:
        return [float(x) for x in vec]
    if len(vec) > DIMS:
        # truncate + L2 renorm (Matryoshka-style safety)
        cut = [float(x) for x in vec[:DIMS]]
        n = sum(x * x for x in cut) ** 0.5 or 1.0
        return [x / n for x in cut]
    raise RuntimeError(f"vector dim {len(vec)} < required {DIMS}")


def _local_embed(texts: list[str]) -> list[list[float]]:
    global _fallback_engine
    from fastembed import TextEmbedding  # type: ignore

    if _fallback_engine is None:
        _log(f"loading local fallback {FALLBACK_MODEL} dims={DIMS}")
        _fallback_engine = TextEmbedding(FALLBACK_MODEL)
    vecs = list(_fallback_engine.embed(texts))
    out = []
    for v in vecs:
        arr = list(map(float, v))
        if len(arr) != DIMS:
            # mxbai is 1024; if mismatch, fail hard rather than corrupt index
            raise RuntimeError(
                f"fallback dim {len(arr)} != {DIMS}; set EMBEDDING_DIMENSIONS to match"
            )
        out.append(arr)
    return out


def _should_fallback(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        # rate limit, quota, overloaded, auth flake → fallback
        if exc.code in (401, 403, 408, 429, 500, 502, 503, 504):
            return True
        return True  # any HTTP error: prefer availability
    if isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError, OSError)):
        return True
    return True  # conservative: always try local on gemini failure


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    if not texts:
        return [], "empty"
    if FORCE_LOCAL:
        return _local_embed(texts), "local-forced"

    key = _load_gemini_key()
    if not key:
        _log("no gemini key file — using local fallback")
        return _local_embed(texts), "local-no-key"

    try:
        vecs = _gemini_embed(texts, key)
        _stats["gemini_ok"] += 1
        _stats["last_backend"] = "gemini"
        return vecs, "gemini"
    except Exception as e:
        body = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                body = ""
            code = e.code
        else:
            code = type(e).__name__
        if _should_fallback(e):
            _log(f"gemini fail ({code}) → local fallback: {body or e}")
            try:
                vecs = _local_embed(texts)
                _stats["fallback_ok"] += 1
                _stats["last_backend"] = "local-fallback"
                return vecs, "local-fallback"
            except Exception as e2:
                _stats["errors"] += 1
                raise RuntimeError(f"gemini+local failed: gemini={e!r} local={e2!r}") from e2
        _stats["errors"] += 1
        raise


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        _log(fmt % args)

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/v1/health", "/"):
            self._json(
                200,
                {
                    "status": "ok",
                    "gemini_model": GEMINI_MODEL,
                    "fallback_model": FALLBACK_MODEL,
                    "dims": DIMS,
                    "force_local": FORCE_LOCAL,
                    "has_gemini_key": bool(_load_gemini_key()),
                    "stats": _stats,
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/embeddings", "/embeddings"):
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(n).decode() or "{}")
            texts = _as_list(payload.get("input"))
            if not texts:
                self._json(400, {"error": {"message": "input required", "type": "invalid"}})
                return
            t0 = time.perf_counter()
            vecs, backend = embed_texts(texts)
            elapsed = time.perf_counter() - t0
            _log(f"embed n={len(texts)} backend={backend} dims={len(vecs[0])} {elapsed:.2f}s")
            data = [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vecs)
            ]
            self._json(
                200,
                {
                    "object": "list",
                    "data": data,
                    "model": payload.get("model") or f"proxy/{backend}",
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                    "proxy_backend": backend,
                },
            )
        except Exception as e:
            _stats["errors"] += 1
            _log(f"error: {e}\n{traceback.format_exc()}")
            self._json(
                500,
                {"error": {"message": str(e), "type": "proxy_error"}},
            )


def main() -> int:
    key_ok = bool(_load_gemini_key())
    _log(
        f"listen http://{HOST}:{PORT} gemini={GEMINI_MODEL} "
        f"fallback={FALLBACK_MODEL} dims={DIMS} key_file={'yes' if key_ok else 'NO'}"
    )
    if not key_ok and not FORCE_LOCAL:
        _log("WARN: no gemini key — all traffic will use local fallback")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
