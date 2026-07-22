#!/usr/bin/env python3
"""Thin proxy: adds input_type to NVIDIA NIM embedding requests and truncates dims.

NIM's nemotron-3-embed-1b outputs 2048 dims. gbrain's OpenAI recipe only
allows [256,512,768,1024,1536,3072]. This proxy truncates embeddings to
the requested dimensions (default 1024) so gbrain accepts them.

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
NIM_FULL_DIMS = 2048
TARGET_DIMS = int(os.environ.get("EMBED_PROXY_TARGET_DIMS", "1024"))

stats = {"ok": 0, "errors": 0, "truncated": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "backend": "nvidia-nim",
                             "model": "nemotron-3-embed-1b", "dims": NIM_FULL_DIMS,
                             "target_dims": TARGET_DIMS,
                             "stats": stats})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if "/embeddings" not in self.path:
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        # Always override model name to match NIM's expected format
        body["model"] = MODEL

        req = Request(NIM_URL, data=json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {NVIDIA_API_KEY}",
                               "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                # Always truncate to TARGET_DIMS (gbrain's configured dimension)
                if TARGET_DIMS < NIM_FULL_DIMS:
                    for item in data.get("data", []):
                        item["embedding"] = item["embedding"][:TARGET_DIMS]
                    stats["truncated"] += 1
                stats["ok"] += 1
                self._json(200, data)
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

print(f"nim-embed-proxy on :{PORT} → {NIM_URL} (model={MODEL}, target_dims={TARGET_DIMS})")
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
