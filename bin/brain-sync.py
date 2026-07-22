#!/usr/bin/env python3
"""Bidirectional sync between gbrain (Global-Brain) and Cognee.

Usage:
  python3 bin/brain-sync.py gbrain2cognee   # Push gbrain pages → Cognee
  python3 bin/brain-sync.py cognee2gbrain   # Push Cognee items → gbrain
  python3 bin/brain-sync.py both            # Bidirectional sync
  python3 bin/brain-sync.py status          # Show both systems' state

Requires:
  - gbrain CLI on PATH (~/.bun/bin/gbrain)
  - Cognee API on :8011 (bin/cognee-fleet-up.sh)
  - NIM embed proxy on :8012
  - OmniRoute on :20128 (for gbrain expansion model)
"""
import json
import hashlib
import os
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

COGNEE_BASE = "http://127.0.0.1:8011"
COGNEE_DATASET = "sin-fleet"
SYNC_TAG = "[brain-sync]"


def get_cognee_api_key():
    """Read Cognee API key from config file."""
    key_file = Path.home() / ".cognee-plugin" / "api_key.json"
    if key_file.exists():
        try:
            data = json.loads(key_file.read_text())
            return data.get("api_key") or data.get("key") or ""
        except Exception:
            pass
    return ""


def run(cmd, timeout=30, env=None):
    """Run a shell command and return stdout."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, env={**os.environ, **(env or {})}
        )
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", 1


def content_hash(text):
    """SHA-256 hash of content for dedup."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── gbrain → Cognee ──────────────────────────────────────────────────────

def gbrain_search_all():
    """Get all gbrain pages via gbrain list."""
    env = {"OPENAI_BASE_URL": "http://127.0.0.1:8012/v1"}
    out, rc = run('gbrain list -n 100 2>/dev/null', env=env)
    if rc != 0 or not out:
        return []
    pages = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Format: slug\ttype\tdate\ttitle (tab-separated)
        parts = line.split("\t")
        if parts:
            slug = parts[0].strip()
            if slug and not slug.startswith("Pages:") and not slug.startswith("By "):
                pages.append(slug)
    return pages


def cognee_remember(text):
    """Ingest text into Cognee via multipart/form-data API."""
    api_key = get_cognee_api_key()
    if not api_key:
        print("  ERROR: no Cognee API key", file=sys.stderr)
        return None

    boundary = f"----sync{uuid.uuid4().hex}"
    parts = []
    for name, value in [("datasetName", COGNEE_DATASET), ("run_in_background", "false")]:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode())
        parts.append(b"\r\n")

    content = text.encode()
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="data"; filename="sync-note.txt"\r\n'
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Api-Key": api_key
    }
    req = urllib.request.Request(
        f"{COGNEE_BASE}/api/v1/remember",
        data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR cognee_remember: {e}", file=sys.stderr)
        return None


def gbrain2cognee():
    """Push gbrain pages to Cognee."""
    print("=== gbrain → Cognee ===")
    pages = gbrain_search_all()
    if not pages:
        print("  No pages found in gbrain")
        return 0

    synced = 0
    for slug in pages:
        # Read page content via gbrain get (use subprocess to avoid shell escaping)
        try:
            r = subprocess.run(
                ["gbrain", "get", slug],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "OPENAI_BASE_URL": "http://127.0.0.1:8012/v1"}
            )
            out = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            out = ""

        if not out:
            print(f"  SKIP {slug}: could not read")
            continue

        # Skip if already synced
        if SYNC_TAG in out:
            print(f"  SKIP {slug}: already synced")
            continue

        tagged = f"{SYNC_TAG} {out}"
        result = cognee_remember(tagged)
        if result and result.get("status") == "completed":
            synced += 1
            print(f"  ✓ {slug}")
        else:
            print(f"  ✗ {slug}")

    print(f"  Synced {synced}/{len(pages)} pages")
    return synced


# ── Cognee → gbrain ──────────────────────────────────────────────────────

def cognee_recall(query="*", top_k=100):
    """Recall all items from Cognee."""
    api_key = get_cognee_api_key()
    data = json.dumps({
        "query": query,
        "datasets": [COGNEE_DATASET],
        "top_k": top_k
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(
        f"{COGNEE_BASE}/api/v1/recall",
        data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR cognee_recall: {e}", file=sys.stderr)
        return []


def gbrain_put_page(slug, content):
    """Write a page to gbrain via put_page MCP tool."""
    # Use gbrain call put_page with JSON args
    args = json.dumps({"slug": slug, "content": content})
    out, rc = run(
        f"OPENAI_BASE_URL=http://127.0.0.1:8012/v1 gbrain call put_page '{args}' 2>/dev/null",
        timeout=30
    )
    return rc == 0 and "error" not in (out or "").lower()


def cognee2gbrain():
    """Push Cognee items to gbrain."""
    print("=== Cognee → gbrain ===")
    results = cognee_recall()
    items = results if isinstance(results, list) else []
    if not items:
        print("  No items found in Cognee")
        return 0

    synced = 0
    for item in items:
        text = item.get("text") or item.get("raw", {}).get("value") or str(item)
        if SYNC_TAG in text:
            continue  # Skip our own synced items

        slug = f"cognee-{content_hash(text)}"
        frontmatter = (
            f"---\ntitle: \"[cognee] {slug}\"\n"
            f"tags: [cognee, synced]\n"
            f"type: fact\nscope: fleet\n---\n\n"
        )
        content = frontmatter + text

        if gbrain_put_page(slug, content):
            synced += 1
            print(f"  ✓ {slug}")
        else:
            print(f"  ✗ {slug}")

    print(f"  Synced {synced}/{len(items)} items")
    return synced


# ── Status ────────────────────────────────────────────────────────────────

def show_status():
    """Show status of both systems."""
    print("=== Brain Sync Status ===\n")

    # gbrain
    out, rc = run("OPENAI_BASE_URL=http://127.0.0.1:8012/v1 gbrain stats 2>/dev/null", env={"OPENAI_BASE_URL": "http://127.0.0.1:8012/v1"})
    print(f"gbrain:\n{out}\n" if out else "gbrain: not available\n")

    # Cognee
    try:
        req = urllib.request.Request(f"{COGNEE_BASE}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read())
        print(f"Cognee: {health.get('status', 'unknown')}")
    except Exception:
        print("Cognee: not available")


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "gbrain2cognee":
        gbrain2cognee()
    elif cmd == "cognee2gbrain":
        cognee2gbrain()
    elif cmd == "both":
        gbrain2cognee()
        cognee2gbrain()
    elif cmd == "status":
        show_status()
    else:
        print(__doc__)
        sys.exit(1)
