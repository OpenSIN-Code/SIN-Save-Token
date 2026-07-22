#!/usr/bin/env python3
"""Thin proxy: adds input_type to NVIDIA NIM embedding requests.

Cognee's OpenAI-compatible client doesn't send input_type, but NIM's
nv-embedqa-e5-v5 requires it. This proxy injects it transparently.

Listens on :8012 (replaces the old Gemini embed-proxy).
"""
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

NIM_URL = "https://integrate.api.nvidia.com/v1/embeddings"
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
PORT = int(os.environ.get("EMBED_PROXY_PORT", "8012"))
MODEL = "nvidia/nemotron-3-embed-1b"

stats = {"ok": 0, "errors": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "backend": "nvidia-nim",
                             "model": "nemotron-3-embed-1b", "dims": 2048,
                             "stats": stats})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if "/embeddings" not in self.path:
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if "model" not in body:
            body["model"] = MODEL

        req = Request(NIM_URL, data=json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {NVIDIA_API_KEY}",
                               "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
                stats["ok"] += 1
                self._raw(200, data)
        except HTTPError as e:
            stats["errors"] += 1
            err = e.read().decode()
            self._json(e.code, {"error": err})
        except Exception as e:
            stats["errors"] += 1
            self._json(502, {"error": str(e)})

    def _json(self, code, obj):
        self._raw(code, json.dumps(obj).encode())

    def _raw(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if not NVIDIA_API_KEY:
    print("error: NVIDIA_API_KEY not set", file=sys.stderr)
    sys.exit(1)

print(f"nim-embed-proxy on :{PORT} → {NIM_URL} (model={MODEL})")
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
