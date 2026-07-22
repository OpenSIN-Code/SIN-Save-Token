#!/usr/bin/env python3
"""Bidirectional sync between gbrain (Global-Brain) and Cognee.

Usage:
  bin/brain-sync.sh gbrain2cognee   # Push gbrain pages → Cognee
  bin/brain-sync.sh cognee2gbrain   # Push Cognee items → gbrain
  bin/brain-sync.sh both            # Bidirectional sync
  bin/brain-sync.sh status          # Show both systems' state

Requires:
  - gbrain CLI on PATH (or ~/.bun/bin/gbrain)
  - Cognee API on :8011 (bin/cognee-fleet-up.sh)
  - NIM embed proxy on :8012
"""
import json
import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path

COGNEE_BASE = "http://127.0.0.1:8011"
COGNEE_DATASET = "sin-fleet"
SYNC_TAG = "brain-sync"


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


def run(cmd, timeout=30):
    """Run a shell command and return stdout."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", 1


def content_hash(text):
    """SHA-256 hash of content for dedup."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── gbrain → Cognee ──────────────────────────────────────────────────────

def gbrain_list_pages():
    """List all gbrain pages via CLI."""
    out, rc = run("gbrain list --json 2>/dev/null")
    if rc != 0:
        # Fallback: use gbrain search with broad query
        out, rc = run('gbrain search "" --json 2>/dev/null || gbrain list 2>/dev/null')
    if rc != 0:
        print(f"  ERROR: gbrain list failed", file=sys.stderr)
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Parse text output
        pages = []
        for line in out.split("\n"):
            if line.strip():
                pages.append({"slug": line.strip()})
        return pages


def gbrain_get_page(slug):
    """Get a gbrain page by slug."""
    out, rc = run(f'gbrain get {slug} 2>/dev/null')
    if rc != 0:
        return None
    return out


def cognee_remember(text, tags=None):
    """Ingest text into Cognee via multipart/form-data API."""
    import uuid
    api_key = get_cognee_api_key()

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
        f'Content-Disposition: form-data; name="data"; filename="sync-note.txt"\r\n'
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["X-Api-Key"] = api_key

    req = urllib.request.Request(
        f"{COGNEE_BASE}/api/v1/remember",
        data=body,
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR cognee_remember: {e}", file=sys.stderr)
        return None


def gbrain2cognee():
    """Push gbrain pages to Cognee."""
    print("=== gbrain → Cognee ===")
    pages = gbrain_list_pages()
    if not pages:
        print("  No pages found in gbrain")
        return 0

    synced = 0
    for page in pages:
        slug = page.get("slug") or page.get("id") or page
        if isinstance(slug, dict):
            slug = slug.get("slug", str(slug))
        content = gbrain_get_page(slug)
        if not content:
            print(f"  SKIP {slug}: could not read")
            continue

        # Add sync tag to avoid re-syncing
        tagged = f"[{SYNC_TAG}] {content}"
        result = cognee_remember(tagged, tags=["gbrain-sync", "rule"])
        if result:
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
        data=data,
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR cognee_recall: {e}", file=sys.stderr)
        return []


def gbrain_put_page(slug, content):
    """Write a page to gbrain via CLI."""
    out, rc = run(f"echo '{content}' | gbrain put {slug} 2>/dev/null")
    return rc == 0


def cognee2gbrain():
    """Push Cognee items to gbrain."""
    print("=== Cognee → gbrain ===")
    results = cognee_recall()
    items = results if isinstance(results, list) else results.get("results", [])
    if not items:
        print("  No items found in Cognee")
        return 0

    synced = 0
    for item in items:
        text = item.get("text") or item.get("content") or str(item)
        if f"[{SYNC_TAG}]" in text:
            continue  # Skip our own synced items

        slug = f"cognee-{content_hash(text)}"
        frontmatter = f"---\ntitle: \"[cognee] {slug}\"\ntags: [cognee, synced]\ntype: fact\nscope: fleet\n---\n\n"
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
    out, rc = run("OPENAI_BASE_URL=http://127.0.0.1:8012/v1 gbrain stats 2>/dev/null")
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
