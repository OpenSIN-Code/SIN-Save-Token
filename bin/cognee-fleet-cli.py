#!/usr/bin/env python3
"""Fleet-wide Cognee CLI (any agent: Claude / Codex / OpenCode / MiMo / Cline / Orca).

Talks HTTP to the shared Cognee API (default http://127.0.0.1:8011).
Auth: COGNEE_API_KEY or ~/.cognee-plugin/api_key.json
Dataset: COGNEE_PLUGIN_DATASET (default sin-fleet)

Usage:
  cognee-fleet-cli recall "What is L2 core MCP?"
  cognee-fleet-cli remember --file path/to/doc.md
  cognee-fleet-cli remember "short fact text"
  cognee-fleet-cli status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _api_key() -> str:
    k = (os.environ.get("COGNEE_API_KEY") or "").strip()
    if k:
        return k
    p = Path.home() / ".cognee-plugin" / "api_key.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return str(d.get("api_key") or d.get("key") or "").strip()
        except (OSError, ValueError):
            pass
    return ""


def _base() -> str:
    return (
        os.environ.get("COGNEE_BASE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or "http://127.0.0.1:8011"
    ).rstrip("/")


def _dataset() -> str:
    return (os.environ.get("COGNEE_PLUGIN_DATASET") or "sin-fleet").strip()


def _req(method: str, path: str, *, data: bytes | None = None, headers: dict | None = None):
    h = {"X-Api-Key": _api_key()}
    if headers:
        h.update(headers)
    req = urllib.request.Request(_base() + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body
    except Exception as e:
        return 0, str(e)


def cmd_status(_: argparse.Namespace) -> int:
    code, body = _req("GET", "/health")
    print(f"health HTTP {code}: {body[:300]}")
    key = _api_key()
    print(f"api_key: {'set' if key else 'MISSING'}  base={_base()}  dataset={_dataset()}")
    if not key:
        return 1
    code, body = _req("GET", "/api/v1/datasets")
    print(f"datasets HTTP {code}: {body[:500]}")
    return 0 if code in (200, 201) else 1


def cmd_recall(ns: argparse.Namespace) -> int:
    if not _api_key():
        print("error: no COGNEE_API_KEY", file=sys.stderr)
        return 1
    payload = {
        "query": ns.query,
        "datasets": [ns.dataset or _dataset()],
        "top_k": ns.top_k,
    }
    code, body = _req(
        "POST",
        "/api/v1/recall",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    if code != 200:
        print(f"error HTTP {code}: {body[:800]}", file=sys.stderr)
        return 1
    try:
        data = json.loads(body)
    except ValueError:
        print(body)
        return 0
    items = data if isinstance(data, list) else [data]
    for it in items:
        if not isinstance(it, dict):
            print(it)
            continue
        text = it.get("text") or (it.get("raw") or {}).get("value") or ""
        kind = it.get("search_type") or it.get("kind") or ""
        print(f"[{kind}] {text}".strip())
    return 0


def _multipart_file(content: bytes, filename: str, dataset: str) -> tuple[bytes, str]:
    import uuid

    boundary = f"----fleet{uuid.uuid4().hex}"
    parts = []
    for name, value in (("datasetName", dataset), ("run_in_background", "false")):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="data"; filename="{filename}"\r\n'
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def cmd_remember(ns: argparse.Namespace) -> int:
    """Write path — uses Boundless Terra for cognify (real money).

    Soft cost notice (does not block agents). Hard caps:
      - refuse files larger than 50k unless COGNEE_ALLOW_COSTLY=1
      - bulk script still requires COGNEE_ALLOW_COSTLY=1
    """
    if not _api_key():
        print("error: no COGNEE_API_KEY", file=sys.stderr)
        return 1
    costly = os.environ.get("COGNEE_ALLOW_COSTLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if ns.file:
        path = Path(ns.file)
        content = path.read_bytes()
        hard_cap = 200_000 if costly else 50_000
        if len(content) > hard_cap:
            print(
                f"REFUSED: file {len(content)} bytes > {hard_cap}. "
                "For large re-ingest set COGNEE_ALLOW_COSTLY=1 "
                "(Boundless Terra cognify is paid).",
                file=sys.stderr,
            )
            return 2
        filename = path.name
        prefix = f"# Source: {path}\n\n".encode()
        content = prefix + content
    else:
        text = ns.text or ""
        if not text:
            print("error: provide text or --file", file=sys.stderr)
            return 1
        content = text.encode()
        filename = "note.txt"
    if not costly and not os.environ.get("COGNEE_QUIET_COST", ""):
        print(
            "note: remember/cognify uses Boundless gpt-5.6-terra (paid). "
            "Prefer short durable notes, not whole READMEs. "
            "COGNEE_QUIET_COST=1 to silence.",
            file=sys.stderr,
        )
    body, boundary = _multipart_file(content, filename, ns.dataset or _dataset())
    code, resp = _req(
        "POST",
        "/api/v1/remember",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    print(f"HTTP {code}: {resp[:500]}")
    return 0 if code == 200 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fleet Cognee CLI for all coding agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="health + auth check")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("recall", help="query shared memory graph")
    r.add_argument("query")
    r.add_argument("-k", "--top-k", type=int, default=5)
    r.add_argument("-d", "--dataset", default=None)
    r.set_defaults(func=cmd_recall)

    m = sub.add_parser("remember", help="ingest text/file into graph (sync cognify)")
    m.add_argument("text", nargs="?", default=None)
    m.add_argument("-f", "--file", default=None)
    m.add_argument("-d", "--dataset", default=None)
    m.set_defaults(func=cmd_remember)

    ns = p.parse_args(argv)
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
