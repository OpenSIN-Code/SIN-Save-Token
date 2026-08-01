#!/usr/bin/env python3
"""Managed adapters for Ponytail, Caveman, pxpipe, and Gigatoken.

The default path is conservative: policy integration is always available, while
file rewriting and visual context compression require an explicit command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "token-optimizer-stack.json"


class StackError(RuntimeError):
    """Expected user-facing failure."""


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def managed_home(config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    override = os.environ.get("SIN_TOKEN_STACK_HOME", "").strip()
    raw = override or config["managed_home"]
    return Path(raw).expanduser().resolve()


def run(argv: Sequence[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_head(path: Path, *, short: bool = True) -> str | None:
    if not (path / ".git").exists():
        return None
    argv = ["git", "rev-parse"]
    if short:
        argv.append("--short")
    argv.append("HEAD")
    result = run(argv, cwd=path, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def normalize_git_url(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


def verify_origin(target: Path, expected_url: str) -> None:
    result = run(["git", "remote", "get-url", "origin"], cwd=target, check=False)
    if result.returncode != 0:
        raise StackError(f"{target}: origin fehlt")
    actual = normalize_git_url(result.stdout.strip())
    expected = normalize_git_url(expected_url)
    if actual != expected:
        raise StackError(f"{target}: unerwartetes origin {actual!r}; erwartet {expected!r}")


def ensure_clean_checkout(target: Path) -> None:
    result = run(["git", "status", "--porcelain"], cwd=target, check=False)
    if result.returncode != 0:
        raise StackError(f"{target}: Git-Status nicht lesbar")
    if result.stdout.strip():
        raise StackError(f"{target}: Worktree ist verändert; geprüften Checkout nicht überschreiben")


def ensure_assessed_source(name: str, config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    spec = config["upstreams"][name]
    target = managed_home(config) / name
    expected = spec["assessed_commit"].lower()
    actual = (git_head(target, short=False) or "").lower()
    if actual != expected:
        raise StackError(
            f"{name} ist nicht auf geprüftem Commit {expected[:7]}; "
            f"ausführen: sin-token-stack sync --source {name}"
        )
    verify_origin(target, spec["url"])
    ensure_clean_checkout(target)
    return target


def sync_one(name: str, spec: dict[str, Any], home: Path) -> dict[str, str]:
    if shutil.which("git") is None:
        raise StackError("git fehlt")
    target = home / name
    expected = spec["assessed_commit"].lower()
    home.mkdir(parents=True, exist_ok=True)
    created = False
    if not target.exists():
        result = run(["git", "clone", "--filter=blob:none", spec["url"], str(target)], check=False)
        if result.returncode != 0:
            raise StackError(f"clone {name} fehlgeschlagen: {result.stderr.strip()}")
        created = True
    elif not (target / ".git").exists():
        raise StackError(f"{target} existiert, ist aber kein Git-Checkout")

    verify_origin(target, spec["url"])
    ensure_clean_checkout(target)
    actual = (git_head(target, short=False) or "").lower()
    if actual != expected:
        result = run(["git", "fetch", "--depth", "1", "origin", expected], cwd=target, check=False)
        if result.returncode != 0:
            raise StackError(f"fetch {name}@{expected[:7]} fehlgeschlagen: {result.stderr.strip()}")
        result = run(["git", "checkout", "--detach", expected], cwd=target, check=False)
        if result.returncode != 0:
            raise StackError(f"checkout {name}@{expected[:7]} fehlgeschlagen: {result.stderr.strip()}")
        action = "cloned+pinned" if created else "pinned"
    else:
        action = "cloned+verified" if created else "verified"
    head = git_head(target) or "unknown"
    return {"name": name, "action": action, "path": str(target), "commit": head}


def pxpipe_policy(model: str, accept_lossy: bool, config: dict[str, Any] | None = None) -> tuple[bool, str]:
    config = config or load_config()
    policy = config["policy"]["pxpipe"]
    normalized = model.strip().lower()
    safe = {item.lower() for item in policy["validated_default_models"]}
    lossy = {item.lower() for item in policy["lossy_opt_in_models"]}
    if normalized in safe:
        return True, "validated-default"
    if normalized in lossy:
        if accept_lossy:
            return True, "lossy-opt-in"
        return False, "model requires --accept-lossy"
    return False, "model is not allowlisted"


def resolve_pxpipe_route(model: str, requested: str = "auto") -> str:
    """Resolve the upstream route without conflating it with PXPIPE_MODELS.

    PXPIPE_MODELS controls image compression only. OPENAI_MODELS and
    CLOUDFLARE_MODELS control provider routing inside pxpipe.
    """
    if requested != "auto":
        return requested
    normalized = model.strip().lower()
    if normalized.startswith("gpt-") or normalized.startswith(("o1", "o3", "o4")):
        return "openai"
    if "kimi" in normalized or normalized.startswith("moonshotai/"):
        return "cloudflare"
    return "default"


def configure_pxpipe_routing(env: dict[str, str], model: str, route: str) -> None:
    if route == "openai":
        env["OPENAI_MODELS"] = model
    elif route == "cloudflare":
        env["CLOUDFLARE_MODELS"] = model


def wait_for_port(host: str, port: int, process: subprocess.Popen[str], timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StackError(f"pxpipe exited before startup with code {process.returncode}")
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise StackError(f"pxpipe did not open {host}:{port}")


def pxpipe_argv(config: dict[str, Any]) -> list[str]:
    installed = shutil.which("pxpipe")
    if installed:
        return [installed]
    if shutil.which("npx") is None:
        raise StackError("weder pxpipe noch npx gefunden; Node.js 18+ installieren")
    package = config["upstreams"]["pxpipe"]["npm_package"]
    return [shutil.which("npx") or "npx", "-y", "--package", package, "pxpipe"]



def planned_chunks(token_count: int, chunk_size: int | None, overlap: int = 0) -> int | None:
    """Return the number of fixed-token windows needed without changing text."""
    if chunk_size is None:
        return None
    if chunk_size <= 0:
        raise StackError("--chunk-size muss größer als 0 sein")
    if overlap < 0:
        raise StackError("--chunk-overlap darf nicht negativ sein")
    if overlap >= chunk_size:
        raise StackError("--chunk-overlap muss kleiner als --chunk-size sein")
    if token_count <= 0:
        return 0
    if token_count <= chunk_size:
        return 1
    stride = chunk_size - overlap
    return 1 + (token_count - chunk_size + stride - 1) // stride


def assessed_gigatoken_version(source: Path) -> str:
    cargo = source / "Cargo.toml"
    try:
        text = cargo.read_text(encoding="utf-8")
    except OSError as exc:
        raise StackError(f"Gigatoken-Version nicht lesbar: {cargo}") from exc
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if match is None:
        raise StackError(f"Gigatoken-Version fehlt in {cargo}")
    return match.group(1)


def gigatoken_runtime_argv(
    config: dict[str, Any] | None = None, *, validation: bool = False
) -> list[str]:
    """Return the locked wheel runtime validated against reviewed source."""
    config = config or load_config()
    spec = config["upstreams"]["gigatoken"]
    source = ensure_assessed_source("gigatoken", config)
    package = spec["python_package"]
    source_version = assessed_gigatoken_version(source)
    expected_package = f"gigatoken=={source_version}"
    if package != expected_package:
        raise StackError(
            f"Gigatoken-Runtime {package!r} passt nicht zum geprüften Quellstand {expected_package!r}"
        )

    runtime_project = (ROOT / spec["runtime_project"]).resolve()
    pyproject = runtime_project / "pyproject.toml"
    lock = runtime_project / "uv.lock"
    if not pyproject.is_file() or not lock.is_file():
        raise StackError(f"gesperrte Gigatoken-Runtime fehlt: {runtime_project}")
    runtime_text = pyproject.read_text(encoding="utf-8")
    if package not in runtime_text:
        raise StackError(f"{pyproject}: {package} fehlt")
    if validation and spec["validation_package"] not in runtime_text:
        raise StackError(f"{pyproject}: {spec['validation_package']} fehlt")

    uv = shutil.which("uv")
    if uv is None:
        raise StackError("uv fehlt; Gigatoken wird absichtlich nicht global installiert")
    argv = [uv, "run", "--quiet", "--frozen", "--project", str(runtime_project)]
    if validation:
        argv.extend(["--group", spec["validation_group"]])
    return argv


_GIGATOKEN_COUNT_SCRIPT = r"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

import awkward as ak
import gigatoken as gt


def chunks(token_count: int, size: int | None, overlap: int) -> int | None:
    if size is None:
        return None
    if token_count <= 0:
        return 0
    if token_count <= size:
        return 1
    stride = size - overlap
    return 1 + (token_count - size + stride - 1) // stride


parser = argparse.ArgumentParser()
parser.add_argument("--tokenizer", required=True)
parser.add_argument("--stdin", action="store_true")
parser.add_argument("--doc-separator")
parser.add_argument("--chunk-size", type=int)
parser.add_argument("--chunk-overlap", type=int, default=0)
parser.add_argument("--json", action="store_true")
parser.add_argument("files", nargs="*")
args = parser.parse_args()

t0 = time.perf_counter()
tokenizer = gt.Tokenizer(args.tokenizer)
load_seconds = time.perf_counter() - t0
rows = []
encode_seconds = 0.0

if args.stdin:
    data = sys.stdin.buffer.read()
    started = time.perf_counter()
    count = int(len(tokenizer.encode(data)))
    encode_seconds += time.perf_counter() - started
    rows.append({"path": "<stdin>", "bytes": len(data), "documents": 1, "tokens": count})
else:
    for raw in args.files:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"not a readable file: {path}")
        source = gt.TextFileSource([path], separator=args.doc_separator) if args.doc_separator is not None else path
        started = time.perf_counter()
        encoded = tokenizer.encode_files(source)
        encode_seconds += time.perf_counter() - started
        rows.append({
            "path": str(path),
            "bytes": path.stat().st_size,
            "documents": len(encoded),
            "tokens": int(ak.count(encoded)),
        })

for row in rows:
    row["planned_chunks"] = chunks(row["tokens"], args.chunk_size, args.chunk_overlap)

total_tokens = sum(row["tokens"] for row in rows)
total_bytes = sum(row["bytes"] for row in rows)
total_docs = sum(row["documents"] for row in rows)
payload = {
    "engine": "gigatoken",
    "tokenizer": args.tokenizer,
    "bytes": total_bytes,
    "documents": total_docs,
    "tokens": total_tokens,
    "chunk_size": args.chunk_size,
    "chunk_overlap": args.chunk_overlap if args.chunk_size is not None else None,
    "planned_chunks": (
        sum(int(row["planned_chunks"]) for row in rows)
        if args.chunk_size is not None
        else None
    ),
    "chunk_plan_mode": "per-input" if args.chunk_size is not None else None,
    "load_seconds": load_seconds,
    "encode_seconds": encode_seconds,
    "files": rows,
}
if args.json:
    print(json.dumps(payload, indent=2, sort_keys=True))
else:
    print(f"tokenizer: {args.tokenizer}")
    print(f"tokens: {total_tokens} | documents: {total_docs} | bytes: {total_bytes}")
    if args.chunk_size is not None:
        print(f"planned chunks: {payload['planned_chunks']} (size={args.chunk_size}, overlap={args.chunk_overlap})")
    print(f"load: {load_seconds:.4f}s | encode: {encode_seconds:.4f}s")
    if len(rows) > 1:
        for row in rows:
            suffix = f" | chunks={row['planned_chunks']}" if row["planned_chunks"] is not None else ""
            print(f"{row['path']}: {row['tokens']} tokens | {row['documents']} docs | {row['bytes']} bytes{suffix}")
"""


def cmd_token_count(args: argparse.Namespace) -> int:
    if args.stdin and args.files:
        raise StackError("--stdin kann nicht zusammen mit Dateien verwendet werden")
    if not args.stdin and not args.files:
        raise StackError("mindestens eine Datei oder --stdin angeben")
    planned_chunks(0, args.chunk_size, args.chunk_overlap)
    config = load_config()
    argv = gigatoken_runtime_argv(config) + [
        "python",
        "-c",
        _GIGATOKEN_COUNT_SCRIPT,
        "--tokenizer",
        args.tokenizer,
    ]
    if args.stdin:
        argv.append("--stdin")
    if args.doc_separator is not None:
        argv.extend(["--doc-separator", args.doc_separator])
    if args.chunk_size is not None:
        argv.extend(["--chunk-size", str(args.chunk_size), "--chunk-overlap", str(args.chunk_overlap)])
    if args.json:
        argv.append("--json")
    argv.extend(str(Path(item).expanduser().resolve()) for item in args.files)
    return subprocess.run(argv).returncode


def cmd_token_bench(args: argparse.Namespace) -> int:
    if not args.files:
        raise StackError("mindestens eine Benchmark-Datei angeben")
    config = load_config()
    argv = gigatoken_runtime_argv(config, validation=args.validate_hf) + [
        "gigatoken",
        "bench",
        args.tokenizer,
    ]
    argv.extend(str(Path(item).expanduser().resolve()) for item in args.files)
    if args.stream_from_disk:
        argv.append("--stream-from-disk")
    if args.validate_hf:
        argv.append("--validate")
    if args.comparison_limit is not None:
        argv.extend(["--comparison-limit", args.comparison_limit])
    if args.doc_separator is not None:
        argv.extend(["--doc-separator", args.doc_separator])
    return subprocess.run(argv).returncode


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    home = managed_home(config)
    rows: list[dict[str, Any]] = []
    for name, spec in config["upstreams"].items():
        target = home / name
        rows.append(
            {
                "name": name,
                "role": spec["role"],
                "integration": spec["integration"],
                "path": str(target),
                "installed": target.is_dir(),
                "commit": git_head(target),
                "assessed_commit": spec["assessed_commit"][:7],
                "assessed": git_head(target, short=False) == spec["assessed_commit"],
                "license": spec["license"],
            }
        )
    payload = {
        "managed_home": str(home),
        "policy_cli": str(ROOT / "bin" / "sin-token-stack"),
        "pxpipe_binary": shutil.which("pxpipe"),
        "node": shutil.which("node"),
        "npx": shutil.which("npx"),
        "uv": shutil.which("uv"),
        "gigatoken_package": config["upstreams"]["gigatoken"].get("python_package"),
        "gigatoken_runtime_project": str(
            (ROOT / config["upstreams"]["gigatoken"]["runtime_project"]).resolve()
        ),
        "sources": rows,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"managed home: {home}")
        for row in rows:
            state = row["commit"] or "not synced"
            pin = "assessed" if row["assessed"] else f"expected {row['assessed_commit']}"
            print(f"{row['name']}: {state} ({pin}) | {row['integration']} | {row['license']}")
        print(f"pxpipe runtime: {payload['pxpipe_binary'] or payload['npx'] or 'missing'}")
        print(
            f"gigatoken runtime: {payload['gigatoken_package']} via locked "
            f"{payload['gigatoken_runtime_project']} and {payload['uv'] or 'missing uv'} "
            "(explicit only)"
        )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()
    home = managed_home(config)
    names = list(config["upstreams"]) if args.source == "all" else [args.source]
    results = [sync_one(name, config["upstreams"][name], home) for name in names]
    state_path = home / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    sources_raw = state.get("sources")
    sources: dict = sources_raw if isinstance(sources_raw, dict) else {}
    sources.update({row["name"]: row for row in results})
    state = {"updated_at": int(time.time()), "sources": sources}
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for row in results:
        print(f"{row['name']}: {row['action']} {row['commit']} at {row['path']}")
    return 0


def cmd_memory_compress(args: argparse.Namespace) -> int:
    config = load_config()
    target = Path(args.file).expanduser().resolve()
    if not target.is_file():
        raise StackError(f"Datei nicht gefunden: {target}")
    caveman = ensure_assessed_source("caveman", config)
    scripts = caveman / "skills" / "caveman-compress"
    if not scripts.is_dir():
        raise StackError("Caveman nicht synchronisiert; zuerst: sin-token-stack sync --source caveman")
    if not args.yes:
        raise StackError("Datei-Rewrite erfordert --yes; das Upstream-Tool legt ein .original.md-Backup an")
    proc = subprocess.run([sys.executable, "-m", "scripts", str(target)], cwd=str(scripts))
    return int(proc.returncode)


def cmd_pxpipe_export(args: argparse.Namespace) -> int:
    config = load_config()
    argv = pxpipe_argv(config) + ["export"]
    if args.git:
        argv.append("--git")
    elif args.stdin:
        argv.append("--stdin")
    else:
        argv.append(args.path)
    return subprocess.run(argv).returncode


def cmd_pxpipe_run(args: argparse.Namespace) -> int:
    config = load_config()
    allowed, reason = pxpipe_policy(args.model, args.accept_lossy, config)
    if not allowed:
        raise StackError(f"pxpipe blockiert: {reason}")
    command = list(args.exec_argv)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise StackError("Befehl nach -- fehlt")
    host = "127.0.0.1"
    port = int(args.port)
    route = resolve_pxpipe_route(args.model, args.route)
    env = os.environ.copy()
    env["PXPIPE_MODELS"] = args.model
    configure_pxpipe_routing(env, args.model, route)
    env["PORT"] = str(port)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as exc:
        raise StackError(f"Port {host}:{port} ist bereits belegt: {exc}") from exc

    proxy_cmd = pxpipe_argv(config)
    log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    try:
        process = subprocess.Popen(
            proxy_cmd,
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
        )
    except OSError:
        log.close()
        raise
    try:
        try:
            wait_for_port(host, port, process)
        except StackError:
            log.flush()
            log.seek(0)
            detail = log.read().strip()
            raise StackError(f"pxpipe start fehlgeschlagen: {detail or 'kein Fehlertext'}")
        child_env = env.copy()
        child_env["ANTHROPIC_BASE_URL"] = f"http://{host}:{port}"
        child_env["OPENAI_BASE_URL"] = f"http://{host}:{port}/v1"
        child_env.setdefault("ANTHROPIC_AUTH_TOKEN", "local-pxpipe")
        if route == "openai" and not env.get("OPENAI_API_KEY"):
            print("warning: OPENAI_API_KEY fehlt; echte OpenAI-Anfragen werden scheitern", file=sys.stderr)
        if route == "cloudflare" and not (env.get("CLOUDFLARE_ACCOUNT_ID") and env.get("CLOUDFLARE_API_TOKEN")):
            print("warning: Cloudflare-Zugangsdaten fehlen; echte Cloudflare-Anfragen werden scheitern", file=sys.stderr)
        print(
            f"pxpipe active for {args.model} ({reason}, route={route}); dashboard http://{host}:{port}/",
            file=sys.stderr,
        )
        return subprocess.run(command, env=child_env).returncode
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sin-token-stack")
    sub = parser.add_subparsers(dest="action", required=True)

    status = sub.add_parser("status", help="show managed source and runtime state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    sync = sub.add_parser("sync", help="clone/update reviewed upstream repositories")
    sync.add_argument("--source", choices=["all", "ponytail", "caveman", "pxpipe", "gigatoken"], default="all")
    sync.set_defaults(func=cmd_sync)

    count = sub.add_parser("token-count", help="exact model-bound token count via Gigatoken")
    count.add_argument("--tokenizer", required=True, help="HF repo/path, tokenizer.json, .tiktoken, or .model")
    count.add_argument("--stdin", action="store_true", help="read one document from stdin")
    count.add_argument("--doc-separator", help="split each input file into documents on this exact separator")
    count.add_argument("--chunk-size", type=int, help="also calculate fixed-token chunk count")
    count.add_argument("--chunk-overlap", type=int, default=0)
    count.add_argument("--json", action="store_true")
    count.add_argument("files", nargs="*")
    count.set_defaults(func=cmd_token_count)

    bench = sub.add_parser("token-bench", help="benchmark Gigatoken and optionally validate HuggingFace parity")
    bench.add_argument("--tokenizer", required=True, help="HF repo/path, tokenizer.json, .tiktoken, or .model")
    bench.add_argument("--validate-hf", action="store_true")
    bench.add_argument("--stream-from-disk", action="store_true")
    bench.add_argument("--comparison-limit", default="100MB")
    bench.add_argument("--doc-separator")
    bench.add_argument("files", nargs="+")
    bench.set_defaults(func=cmd_token_bench)

    compress = sub.add_parser("memory-compress", help="explicit Caveman memory-file rewrite")
    compress.add_argument("file")
    compress.add_argument("--yes", action="store_true", help="confirm rewrite with upstream backup")
    compress.set_defaults(func=cmd_memory_compress)

    export = sub.add_parser("pxpipe-export", help="render dense context to PNG without proxying")
    group = export.add_mutually_exclusive_group()
    group.add_argument("--git", action="store_true")
    group.add_argument("--stdin", action="store_true")
    export.add_argument("path", nargs="?", default=".")
    export.set_defaults(func=cmd_pxpipe_export)

    pxrun = sub.add_parser("pxpipe-run", help="run one command through an isolated pxpipe proxy")
    pxrun.add_argument("--model", required=True)
    pxrun.add_argument("--accept-lossy", action="store_true")
    pxrun.add_argument(
        "--route",
        choices=["auto", "default", "openai", "cloudflare"],
        default="auto",
        help="provider route; auto sends GPT/o-series to OpenAI",
    )
    pxrun.add_argument("--port", type=int, default=47821)
    pxrun.add_argument("exec_argv", nargs=argparse.REMAINDER)
    pxrun.set_defaults(func=cmd_pxpipe_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (StackError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
