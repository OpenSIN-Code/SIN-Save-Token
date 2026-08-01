#!/usr/bin/env python3
"""Managed adapters for Ponytail, Caveman, pxpipe, and Gigatoken.

The default path is conservative: policy integration is always available, while
file rewriting and visual context compression require an explicit command.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
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
from typing import Any, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "token-optimizer-stack.json"


class StackError(RuntimeError):
    """Expected user-facing failure."""


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StackError(f"{label} muss ein JSON-Objekt sein")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StackError(f"{label} muss eine nichtleere Zeichenkette sein")
    return value.strip()


def _safe_project_path(raw: Any, label: str) -> Path:
    value = _require_string(raw, label)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise StackError(f"{label} muss relativ innerhalb des Repositorys liegen")
    resolved = (ROOT / relative).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise StackError(f"{label} verlässt das Repository") from exc
    return resolved


def _parse_npm_pin(value: Any, label: str) -> tuple[str, str]:
    pin = _require_string(value, label)
    if "@" not in pin:
        raise StackError(f"{label} muss exakt als package@version gepinnt sein")
    name, version = pin.rsplit("@", 1)
    if not name or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise StackError(f"{label} muss eine exakte SemVer-Version verwenden")
    return name, version


def _parse_python_pin(value: Any, label: str) -> tuple[str, str]:
    pin = _require_string(value, label)
    if "==" not in pin:
        raise StackError(f"{label} muss exakt als package==version gepinnt sein")
    name, version = pin.split("==", 1)
    if not name or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise StackError(f"{label} muss eine exakte Version verwenden")
    return name, version


def validate_config(config: Any) -> dict[str, Any]:
    data = _require_mapping(config, "Konfiguration")
    if data.get("version") != 1:
        raise StackError("token-optimizer-stack.json: nur version 1 wird unterstützt")
    _require_string(data.get("managed_home"), "managed_home")

    upstreams = _require_mapping(data.get("upstreams"), "upstreams")
    expected_sources = {"ponytail", "caveman", "pxpipe", "gigatoken"}
    if set(upstreams) != expected_sources:
        raise StackError(f"upstreams muss exakt {sorted(expected_sources)} enthalten")
    for name, raw_spec in upstreams.items():
        spec = _require_mapping(raw_spec, f"upstreams.{name}")
        url = _require_string(spec.get("url"), f"upstreams.{name}.url")
        if not url.startswith("https://github.com/") or not url.endswith(".git"):
            raise StackError(f"upstreams.{name}.url muss eine HTTPS-GitHub-URL sein")
        if spec.get("license") != "MIT":
            raise StackError(f"upstreams.{name}.license muss MIT sein")
        _require_string(spec.get("role"), f"upstreams.{name}.role")
        _require_string(spec.get("integration"), f"upstreams.{name}.integration")
        commit = _require_string(spec.get("assessed_commit"), f"upstreams.{name}.assessed_commit")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise StackError(f"upstreams.{name}.assessed_commit muss ein vollständiger SHA-1 sein")

    pxpipe = _require_mapping(upstreams["pxpipe"], "upstreams.pxpipe")
    _parse_npm_pin(pxpipe.get("npm_package"), "upstreams.pxpipe.npm_package")
    _safe_project_path(pxpipe.get("runtime_project"), "upstreams.pxpipe.runtime_project")
    _require_string(pxpipe.get("npm_integrity"), "upstreams.pxpipe.npm_integrity")
    transitive = _require_mapping(pxpipe.get("transitive_integrity"), "upstreams.pxpipe.transitive_integrity")
    if set(transitive) != {"gpt-tokenizer@3.4.0"}:
        raise StackError("upstreams.pxpipe.transitive_integrity muss gpt-tokenizer@3.4.0 enthalten")
    _require_string(transitive["gpt-tokenizer@3.4.0"], "gpt-tokenizer integrity")

    gigatoken = _require_mapping(upstreams["gigatoken"], "upstreams.gigatoken")
    _parse_python_pin(gigatoken.get("python_package"), "upstreams.gigatoken.python_package")
    _parse_python_pin(gigatoken.get("validation_package"), "upstreams.gigatoken.validation_package")
    _safe_project_path(gigatoken.get("runtime_project"), "upstreams.gigatoken.runtime_project")
    _require_string(gigatoken.get("validation_group"), "upstreams.gigatoken.validation_group")

    policy = _require_mapping(data.get("policy"), "policy")
    for key in ("always_on", "explicit_only"):
        values = policy.get(key)
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
            raise StackError(f"policy.{key} muss eine nichtleere String-Liste sein")
    px_policy = _require_mapping(policy.get("pxpipe"), "policy.pxpipe")
    if px_policy.get("default_mode") != "off":
        raise StackError("policy.pxpipe.default_mode muss off sein")
    for key in ("validated_default_models", "lossy_opt_in_models", "never_image"):
        values = px_policy.get(key)
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise StackError(f"policy.pxpipe.{key} muss eine String-Liste sein")
    giga_policy = _require_mapping(policy.get("gigatoken"), "policy.gigatoken")
    if giga_policy.get("default_mode") != "off":
        raise StackError("policy.gigatoken.default_mode muss off sein")
    if giga_policy.get("require_explicit_tokenizer") is not True:
        raise StackError("policy.gigatoken.require_explicit_tokenizer muss true sein")
    if giga_policy.get("never_assume_provider_parity") is not True:
        raise StackError("policy.gigatoken.never_assume_provider_parity muss true sein")
    return data


def load_config() -> dict[str, Any]:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StackError(f"Konfiguration nicht lesbar: {CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise StackError(f"Konfiguration ist kein gültiges JSON: {exc}") from exc
    return validate_config(raw)


def managed_home(config: Mapping[str, Any] | None = None) -> Path:
    config = config or load_config()
    override = os.environ.get("SIN_TOKEN_STACK_HOME", "").strip()
    raw = override or str(config["managed_home"])
    path = Path(raw).expanduser().resolve()
    if path == Path(path.anchor):
        raise StackError("SIN_TOKEN_STACK_HOME darf nicht das Dateisystem-Root sein")
    return path


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def git_head(path: Path, *, short: bool = True) -> str | None:
    if not (path / ".git").exists():
        return None
    argv = ["git", "rev-parse"]
    if short:
        argv.append("--short")
    argv.append("HEAD")
    result = run(argv, cwd=path, check=False, timeout=15)
    return result.stdout.strip() if result.returncode == 0 else None


def normalize_git_url(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


def _origin_matches(target: Path, expected_url: str) -> bool:
    result = run(["git", "remote", "get-url", "origin"], cwd=target, check=False, timeout=15)
    return result.returncode == 0 and normalize_git_url(result.stdout.strip()) == normalize_git_url(expected_url)


def verify_origin(target: Path, expected_url: str) -> None:
    if _origin_matches(target, expected_url):
        return
    result = run(["git", "remote", "get-url", "origin"], cwd=target, check=False, timeout=15)
    actual = result.stdout.strip() if result.returncode == 0 else "<missing>"
    raise StackError(f"{target}: unerwartetes origin {actual!r}; erwartet {expected_url!r}")


def _checkout_is_clean(target: Path) -> bool:
    result = run(["git", "status", "--porcelain"], cwd=target, check=False, timeout=15)
    return result.returncode == 0 and not result.stdout.strip()


def ensure_clean_checkout(target: Path) -> None:
    if not _checkout_is_clean(target):
        raise StackError(f"{target}: Worktree ist verändert oder nicht lesbar")


def source_state(name: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    spec = config["upstreams"][name]
    target = managed_home(config) / name
    present = target.exists() or target.is_symlink()
    installed = target.is_dir() and (target / ".git").exists() and not target.is_symlink()
    full_commit = git_head(target, short=False) if installed else None
    origin_valid = _origin_matches(target, str(spec["url"])) if installed else False
    clean = _checkout_is_clean(target) if installed else False
    assessed = full_commit == spec["assessed_commit"]
    return {
        "name": name,
        "role": spec["role"],
        "integration": spec["integration"],
        "path": str(target),
        "present": present,
        "installed": installed,
        "commit": full_commit,
        "assessed_commit": spec["assessed_commit"],
        "assessed": assessed,
        "origin_valid": origin_valid,
        "clean": clean,
        "ready": installed and assessed and origin_valid and clean,
        "license": spec["license"],
    }


def ensure_assessed_source(name: str, config: Mapping[str, Any] | None = None) -> Path:
    config = config or load_config()
    state = source_state(name, config)
    if not state["ready"]:
        expected = str(state["assessed_commit"])
        raise StackError(
            f"{name} ist nicht als sauberer geprüfter Checkout auf {expected[:7]} verfügbar; "
            f"ausführen: sin-token-stack sync --source {name}"
        )
    return Path(str(state["path"]))


@contextlib.contextmanager
def sync_lock(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / ".sync.lock"
    if lock_path.is_symlink():
        raise StackError(f"Symlink als Sync-Lock ist nicht erlaubt: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StackError("ein anderer sin-token-stack sync läuft bereits") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sync_one(name: str, spec: Mapping[str, Any], home: Path) -> dict[str, str]:
    git = shutil.which("git")
    if git is None:
        raise StackError("git fehlt")
    target = home / name
    expected = str(spec["assessed_commit"]).lower()
    home.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise StackError(f"{target}: Symlink als verwalteter Checkout ist nicht erlaubt")

    created = False
    if not target.exists():
        temp_target = home / f".{name}.clone-{os.getpid()}-{time.time_ns()}"
        result = run([git, "clone", "--filter=blob:none", str(spec["url"]), str(temp_target)], check=False)
        if result.returncode != 0:
            shutil.rmtree(temp_target, ignore_errors=True)
            raise StackError(f"clone {name} fehlgeschlagen: {result.stderr.strip()}")
        try:
            os.replace(temp_target, target)
        except OSError:
            shutil.rmtree(temp_target, ignore_errors=True)
            raise
        created = True
    elif not (target / ".git").exists():
        raise StackError(f"{target} existiert, ist aber kein Git-Checkout")

    verify_origin(target, str(spec["url"]))
    ensure_clean_checkout(target)
    actual = (git_head(target, short=False) or "").lower()
    if actual != expected:
        result = run([git, "fetch", "--depth", "1", "origin", expected], cwd=target, check=False)
        if result.returncode != 0:
            raise StackError(f"fetch {name}@{expected[:7]} fehlgeschlagen: {result.stderr.strip()}")
        result = run([git, "checkout", "--detach", expected], cwd=target, check=False)
        if result.returncode != 0:
            raise StackError(f"checkout {name}@{expected[:7]} fehlgeschlagen: {result.stderr.strip()}")
        action = "cloned+pinned" if created else "pinned"
    else:
        action = "cloned+verified" if created else "verified"

    final = (git_head(target, short=False) or "").lower()
    if final != expected:
        raise StackError(f"{name}: Checkout endete auf {final or '<unknown>'}, erwartet {expected}")
    verify_origin(target, str(spec["url"]))
    ensure_clean_checkout(target)
    return {"name": name, "action": action, "path": str(target), "commit": final[:7]}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StackError(f"Symlink als State-Datei ist nicht erlaubt: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


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


def assessed_pxpipe_version(source: Path) -> str:
    package_path = source / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StackError(f"pxpipe-Paketmetadaten nicht lesbar: {package_path}") from exc
    if package.get("name") != "pxpipe-proxy":
        raise StackError(f"{package_path}: unerwarteter Paketname")
    return _require_string(package.get("version"), f"{package_path}: version")


def validate_pxpipe_runtime_project(
    config: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    config = config or load_config()
    spec = config["upstreams"]["pxpipe"]
    package_name, expected_version = _parse_npm_pin(spec["npm_package"], "npm_package")
    runtime = _safe_project_path(spec["runtime_project"], "pxpipe.runtime_project")
    package_path = runtime / "package.json"
    lock_path = runtime / "package-lock.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StackError(f"gesperrte pxpipe-Runtime fehlt: {runtime}") from exc
    except json.JSONDecodeError as exc:
        raise StackError(f"pxpipe-Runtime enthält ungültiges JSON: {exc}") from exc

    dependencies = package.get("dependencies")
    if not isinstance(dependencies, dict) or dependencies.get(package_name) != expected_version:
        raise StackError(f"{package_path}: {package_name} muss exakt {expected_version} sein")
    if lock.get("lockfileVersion") != 3:
        raise StackError(f"{lock_path}: lockfileVersion 3 erforderlich")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise StackError(f"{lock_path}: packages fehlt")

    root = packages.get("")
    if not isinstance(root, dict) or root.get("dependencies") != {package_name: expected_version}:
        raise StackError(f"{lock_path}: Root-Abhängigkeiten driften")
    px_entry = packages.get(f"node_modules/{package_name}")
    expected_integrity = str(spec["npm_integrity"])
    if (
        not isinstance(px_entry, dict)
        or px_entry.get("version") != expected_version
        or px_entry.get("integrity") != expected_integrity
    ):
        raise StackError(f"{lock_path}: {package_name}@{expected_version} oder SRI fehlt")

    transitive = spec["transitive_integrity"]
    for pin, integrity in transitive.items():
        dependency, version = _parse_npm_pin(pin, f"transitive_integrity.{pin}")
        entry = packages.get(f"node_modules/{dependency}")
        if not isinstance(entry, dict) or entry.get("version") != version or entry.get("integrity") != integrity:
            raise StackError(f"{lock_path}: {pin} oder SRI fehlt")
    return runtime, expected_version


def _node_major(executable: Path) -> int | None:
    result = run([str(executable), "--version"], check=False, timeout=10)
    match = re.match(r"^v(\d+)", result.stdout.strip())
    if result.returncode != 0 or match is None:
        return None
    return int(match.group(1))


def compatible_node() -> Path:
    candidates: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if raw:
            candidates.append(Path(raw) / "node")
    candidates.extend([Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node")])
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        seen.add(resolved)
        major = _node_major(resolved)
        if major is not None and major >= 18:
            return resolved
    raise StackError("Node.js 18+ fehlt")


def npm_for_node(node: Path) -> Path:
    sibling = node.parent / "npm"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling.resolve()
    npm = shutil.which("npm")
    if npm is None:
        raise StackError("npm fehlt")
    return Path(npm).resolve()


def npm_command(node: Path) -> list[str]:
    npm = npm_for_node(node)
    try:
        first_line = npm.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        first_line = ""
    if npm.suffix == ".js" or "node" in first_line.lower():
        return [str(node), str(npm)]
    return [str(npm)]


def managed_pxpipe_runtime(config: Mapping[str, Any] | None = None) -> Path:
    config = config or load_config()
    return managed_home(config) / ".runtime" / "pxpipe"


def pxpipe_runtime_state(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    managed = managed_pxpipe_runtime(config)
    expected_project, expected_version = validate_pxpipe_runtime_project(config)
    binary = managed / "node_modules" / "pxpipe-proxy" / "bin" / "cli.js"
    package_path = managed / "node_modules" / "pxpipe-proxy" / "package.json"
    installed_version: str | None = None
    if package_path.is_file():
        try:
            value = json.loads(package_path.read_text(encoding="utf-8")).get("version")
            installed_version = value if isinstance(value, str) else None
        except (OSError, json.JSONDecodeError):
            installed_version = None
    manifests_match = False
    try:
        manifests_match = (
            (managed / "package.json").read_bytes() == (expected_project / "package.json").read_bytes()
            and (managed / "package-lock.json").read_bytes()
            == (expected_project / "package-lock.json").read_bytes()
        )
    except OSError:
        pass
    installed = managed.is_dir() and not managed.is_symlink()
    ready = (
        installed
        and binary.is_file()
        and installed_version == expected_version
        and manifests_match
    )
    return {
        "path": str(managed),
        "installed": installed,
        "binary": str(binary),
        "version": installed_version,
        "expected_version": expected_version,
        "manifests_match": manifests_match,
        "ready": ready,
    }


def install_pxpipe_runtime(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    config = config or load_config()
    node = compatible_node()

    source = ensure_assessed_source("pxpipe", config)
    _, expected_version = validate_pxpipe_runtime_project(config)
    source_version = assessed_pxpipe_version(source)
    if source_version != expected_version:
        raise StackError(
            f"pxpipe-Runtime {expected_version} passt nicht zum geprüften Quellstand {source_version}"
        )
    current = pxpipe_runtime_state(config)
    if current["ready"]:
        return {"action": "verified", "path": str(current["path"]), "version": expected_version}

    project, _ = validate_pxpipe_runtime_project(config)
    target = managed_pxpipe_runtime(config)
    if target.is_symlink():
        raise StackError(f"{target}: Symlink als Runtime ist nicht erlaubt")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".pxpipe-install-", dir=parent))
    backup: Path | None = None
    try:
        shutil.copy2(project / "package.json", temp / "package.json")
        shutil.copy2(project / "package-lock.json", temp / "package-lock.json")
        result = run(
            npm_command(node) + ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=temp,
            check=False,
            timeout=180,
        )
        if result.returncode != 0:
            raise StackError(f"npm ci für pxpipe fehlgeschlagen: {result.stderr.strip()}")
        binary = temp / "node_modules" / "pxpipe-proxy" / "bin" / "cli.js"
        installed_package = temp / "node_modules" / "pxpipe-proxy" / "package.json"
        if not binary.is_file() or not installed_package.is_file():
            raise StackError("pxpipe-Installation enthält kein ausführbares CLI")
        installed_version = json.loads(installed_package.read_text(encoding="utf-8")).get("version")
        if installed_version != expected_version:
            raise StackError(
                f"pxpipe installierte Version {installed_version!r}, erwartet {expected_version!r}"
            )
        if target.exists():
            backup = parent / f".pxpipe-backup-{os.getpid()}-{time.time_ns()}"
            os.replace(target, backup)
        os.replace(temp, target)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return {"action": "installed", "path": str(target), "version": expected_version}
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def pxpipe_argv(config: Mapping[str, Any] | None = None) -> list[str]:
    state = pxpipe_runtime_state(config)
    if not state["ready"]:
        raise StackError(
            "pxpipe-Runtime fehlt oder driftet; ausführen: sin-token-stack sync --source pxpipe"
        )
    node = compatible_node()
    return [str(node), str(state["binary"])]


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


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StackError(f"{label} nicht lesbar: {path}") from exc


def _locked_python_packages(lock_text: str) -> set[tuple[str, str]]:
    packages: set[tuple[str, str]] = set()
    for block in lock_text.split("[[package]]")[1:]:
        name_match = re.search(r'^name\s*=\s*"([^"]+)"', block, flags=re.MULTILINE)
        version_match = re.search(r'^version\s*=\s*"([^"]+)"', block, flags=re.MULTILINE)
        if name_match and version_match:
            packages.add((name_match.group(1), version_match.group(1)))
    return packages


def validate_gigatoken_runtime_project(
    config: Mapping[str, Any] | None = None,
    *,
    validation: bool = False,
) -> tuple[Path, str]:
    config = config or load_config()
    spec = config["upstreams"]["gigatoken"]
    package_name, expected_version = _parse_python_pin(spec["python_package"], "python_package")
    validation_name, validation_version = _parse_python_pin(
        spec["validation_package"], "validation_package"
    )
    runtime = _safe_project_path(spec["runtime_project"], "gigatoken.runtime_project")
    pyproject_path = runtime / "pyproject.toml"
    lock_path = runtime / "uv.lock"
    pyproject_text = _read_text(pyproject_path, "Gigatoken pyproject")
    lock_text = _read_text(lock_path, "Gigatoken uv.lock")
    if f'"{spec["python_package"]}"' not in pyproject_text:
        raise StackError(f"{pyproject_path}: {spec['python_package']} fehlt")
    if validation and f'"{spec["validation_package"]}"' not in pyproject_text:
        raise StackError(f"{pyproject_path}: Validierungsgruppe ist nicht exakt gepinnt")
    locked = _locked_python_packages(lock_text)
    if (package_name, expected_version) not in locked:
        raise StackError(f"{lock_path}: {package_name}=={expected_version} fehlt")
    if validation and (validation_name, validation_version) not in locked:
        raise StackError(f"{lock_path}: {validation_name}=={validation_version} fehlt")
    return runtime, expected_version


def gigatoken_runtime_argv(
    config: Mapping[str, Any] | None = None,
    *,
    validation: bool = False,
) -> list[str]:
    config = config or load_config()
    source = ensure_assessed_source("gigatoken", config)
    runtime, expected_version = validate_gigatoken_runtime_project(config, validation=validation)
    source_version = assessed_gigatoken_version(source)
    if source_version != expected_version:
        raise StackError(
            f"Gigatoken-Runtime {expected_version!r} passt nicht zum geprüften Quellstand {source_version!r}"
        )
    uv = shutil.which("uv")
    if uv is None:
        raise StackError("uv fehlt; Gigatoken wird absichtlich nicht global installiert")
    argv = [uv, "run", "--quiet", "--frozen", "--project", str(runtime)]
    if validation:
        argv.extend(["--group", str(config["upstreams"]["gigatoken"]["validation_group"])])
    return argv


_GIGATOKEN_COUNT_SCRIPT = r"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
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


def _resolved_input_files(raw_files: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_files:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise StackError(f"Datei nicht lesbar: {path}")
        paths.append(path)
    return paths


def cmd_token_count(args: argparse.Namespace) -> int:
    if args.stdin and args.files:
        raise StackError("--stdin kann nicht zusammen mit Dateien verwendet werden")
    if not args.stdin and not args.files:
        raise StackError("mindestens eine Datei oder --stdin angeben")
    planned_chunks(0, args.chunk_size, args.chunk_overlap)
    files = _resolved_input_files(args.files)
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
    argv.extend(str(path) for path in files)
    return subprocess.run(argv).returncode


def cmd_token_bench(args: argparse.Namespace) -> int:
    files = _resolved_input_files(args.files)
    if not files:
        raise StackError("mindestens eine Benchmark-Datei angeben")
    config = load_config()
    argv = gigatoken_runtime_argv(config, validation=args.validate_hf) + [
        "gigatoken",
        "bench",
        args.tokenizer,
    ]
    argv.extend(str(path) for path in files)
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
    rows = [source_state(name, config) for name in config["upstreams"]]
    problems: list[str] = []
    for row in rows:
        if row["present"] and not row["ready"]:
            problems.append(f"source {row['name']} is present but not a clean assessed checkout")

    try:
        px_runtime = pxpipe_runtime_state(config)
        px_runtime["manifest_valid"] = True
    except StackError as exc:
        px_runtime = {
            "ready": False,
            "installed": False,
            "manifest_valid": False,
            "error": str(exc),
        }
        problems.append(f"pxpipe runtime manifest invalid: {exc}")
    px_source = next(row for row in rows if row["name"] == "pxpipe")
    if px_source["installed"] and not px_runtime.get("ready"):
        problems.append("pxpipe source is synced but locked runtime is not ready")
    if px_runtime.get("installed") and not px_source["ready"]:
        problems.append("pxpipe runtime is installed without a ready assessed source")
    try:
        giga_runtime, giga_version = validate_gigatoken_runtime_project(config, validation=True)
        giga_state: dict[str, Any] = {
            "path": str(giga_runtime),
            "expected_version": giga_version,
            "manifest_valid": True,
            "uv": shutil.which("uv"),
        }
    except StackError as exc:
        giga_state = {"manifest_valid": False, "error": str(exc), "uv": shutil.which("uv")}
        problems.append(f"gigatoken runtime manifest invalid: {exc}")
    giga_source = next(row for row in rows if row["name"] == "gigatoken")
    if giga_source["installed"] and not giga_state.get("uv"):
        problems.append("gigatoken source is synced but uv is missing")

    try:
        selected_node = compatible_node()
        selected_npm = npm_for_node(selected_node)
    except StackError:
        selected_node = None
        selected_npm = None
    if px_source["installed"] and selected_node is None:
        problems.append("pxpipe source is synced but Node.js 18+ is missing")

    payload = {
        "manifest_valid": True,
        "managed_home": str(managed_home(config)),
        "policy_cli": str(ROOT / "bin" / "sin-token-stack"),
        "node": str(selected_node) if selected_node else None,
        "npm": str(selected_npm) if selected_npm else None,
        "uv": shutil.which("uv"),
        "sources": rows,
        "runtimes": {"pxpipe": px_runtime, "gigatoken": giga_state},
        "check_ok": not problems,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"managed home: {payload['managed_home']}")
        for row in rows:
            if not row["present"]:
                state = "not synced (optional)"
            elif row["ready"]:
                state = f"{str(row['commit'])[:7]} assessed+clean"
            else:
                state = f"drifted (expected {str(row['assessed_commit'])[:7]})"
            print(f"{row['name']}: {state} | {row['integration']} | {row['license']}")
        print(
            f"pxpipe runtime: {'ready' if px_runtime.get('ready') else 'not installed'} "
            f"(expected {px_runtime.get('expected_version', 'unknown')})"
        )
        print(
            "gigatoken runtime manifest: "
            f"{'valid' if giga_state.get('manifest_valid') else 'invalid'}; "
            f"uv={giga_state.get('uv') or 'missing'}"
        )
        for problem in problems:
            print(f"problem: {problem}", file=sys.stderr)
    return 0 if not args.check or not problems else 2


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()
    home = managed_home(config)
    names = list(config["upstreams"]) if args.source == "all" else [args.source]
    with sync_lock(home):
        results = [sync_one(name, config["upstreams"][name], home) for name in names]
        runtime_results: dict[str, Any] = {}
        if "pxpipe" in names:
            runtime_results["pxpipe"] = install_pxpipe_runtime(config)
        state_path = home / "state.json"
        if state_path.is_symlink():
            raise StackError(f"Symlink als State-Datei ist nicht erlaubt: {state_path}")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        sources_raw = state.get("sources") if isinstance(state, dict) else None
        sources: dict[str, Any] = sources_raw if isinstance(sources_raw, dict) else {}
        sources.update({row["name"]: row for row in results})
        runtimes_raw = state.get("runtimes") if isinstance(state, dict) else None
        runtimes: dict[str, Any] = runtimes_raw if isinstance(runtimes_raw, dict) else {}
        runtimes.update(runtime_results)
        state = {"updated_at": int(time.time()), "sources": sources, "runtimes": runtimes}
        _atomic_write_json(state_path, state)
    for row in results:
        print(f"{row['name']}: {row['action']} {row['commit']} at {row['path']}")
    for name, row in runtime_results.items():
        print(f"{name} runtime: {row['action']} {row['version']} at {row['path']}")
    return 0


def caveman_backup_path(target: Path) -> Path:
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "caveman-compress" / "backups" / target.parent.name / f"{target.stem}.original.md"


def _validate_caveman_target(raw: str) -> Path:
    raw_target = Path(raw).expanduser()
    if raw_target.is_symlink():
        raise StackError("Symlink-Ziele werden nicht komprimiert")
    target = raw_target.resolve()
    if not target.is_file():
        raise StackError(f"Datei nicht gefunden: {target}")
    if target.stat().st_size > 500_000:
        raise StackError("Caveman-Datei ist größer als 500 KB")
    sensitive_components = {".ssh", ".aws", ".gnupg", ".kube", ".docker"}
    if sensitive_components & {part.lower() for part in target.parts}:
        raise StackError("Caveman-Datei liegt in einem sensiblen Konfigurationsverzeichnis")
    normalized_name = re.sub(r"[_\-\s.]", "", target.name.lower())
    if any(
        token in normalized_name
        for token in (
            "secret",
            "credential",
            "password",
            "passwd",
            "apikey",
            "accesskey",
            "privatekey",
        )
    ):
        raise StackError("Caveman-Dateiname wirkt sensibel; Upload wird verweigert")
    return target


def terminate_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def cmd_memory_compress(args: argparse.Namespace) -> int:
    if not args.allow_third_party_upload:
        raise StackError(
            "Caveman sendet den Dateiinhalt an Claude/Anthropic; "
            "explizit bestätigen mit --allow-third-party-upload"
        )
    if not args.yes:
        raise StackError("Datei-Rewrite erfordert --yes")
    if args.timeout <= 0:
        raise StackError("--timeout muss größer als 0 sein")
    target = _validate_caveman_target(args.file)
    try:
        original_text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise StackError("Caveman-Datei muss gültiges UTF-8 sein") from exc
    backup_path = caveman_backup_path(target)
    if backup_path.exists():
        raise StackError(f"Caveman-Backup existiert bereits: {backup_path}")

    config = load_config()
    caveman = ensure_assessed_source("caveman", config)
    scripts = caveman / "skills" / "caveman-compress"
    if not scripts.is_dir():
        raise StackError("Caveman nicht synchronisiert; zuerst: sin-token-stack sync --source caveman")
    print(
        "warning: Caveman überträgt diese Datei an Claude/Anthropic; "
        "Backup liegt außerhalb des Quellverzeichnisses im Caveman-Datenordner.",
        file=sys.stderr,
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts", str(target)],
        cwd=str(scripts),
        start_new_session=(os.name == "posix"),
    )
    try:
        returncode = int(process.wait(timeout=args.timeout))
        if returncode != 0:
            raise StackError(f"Caveman beendete sich mit Exit-Code {returncode}")
        if not backup_path.is_file():
            raise StackError("Caveman meldete Erfolg, aber das Original-Backup fehlt")
        try:
            backup_text = backup_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StackError(f"Caveman-Backup ist nicht lesbar: {backup_path}") from exc
        if backup_text != original_text:
            raise StackError("Caveman-Backup stimmt nicht mit dem Original überein")
        return 0
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        raise StackError(f"Caveman überschritt das Zeitlimit von {args.timeout:g}s") from exc
    except KeyboardInterrupt:
        terminate_process_group(process)
        return 130


def cmd_pxpipe_export(args: argparse.Namespace) -> int:
    config = load_config()
    argv = pxpipe_argv(config) + ["export"]
    if (args.git or args.stdin) and args.path != ".":
        raise StackError("Pfad kann nicht mit --git oder --stdin kombiniert werden")
    if args.git:
        argv.append("--git")
    elif args.stdin:
        argv.append("--stdin")
    else:
        path = Path(args.path).expanduser().resolve()
        if not path.exists():
            raise StackError(f"Pfad nicht gefunden: {path}")
        argv.append(str(path))
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
    if not 1 <= port <= 65535:
        raise StackError("--port muss zwischen 1 und 65535 liegen")
    if args.startup_timeout <= 0:
        raise StackError("--startup-timeout muss größer als 0 sein")
    route = resolve_pxpipe_route(args.model, args.route)
    env = os.environ.copy()
    env["PXPIPE_MODELS"] = args.model
    env["HOST"] = host
    env["PORT"] = str(port)
    configure_pxpipe_routing(env, args.model, route)
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
            start_new_session=(os.name == "posix"),
        )
    except OSError:
        log.close()
        raise
    try:
        try:
            wait_for_port(host, port, process, timeout=args.startup_timeout)
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
        if route == "cloudflare" and not (
            env.get("CLOUDFLARE_ACCOUNT_ID") and env.get("CLOUDFLARE_API_TOKEN")
        ):
            print(
                "warning: Cloudflare-Zugangsdaten fehlen; echte Cloudflare-Anfragen werden scheitern",
                file=sys.stderr,
            )
        print(
            f"pxpipe active for {args.model} ({reason}, route={route}); dashboard http://{host}:{port}/",
            file=sys.stderr,
        )
        return subprocess.run(command, env=child_env).returncode
    finally:
        terminate_process_group(process)
        log.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sin-token-stack")
    sub = parser.add_subparsers(dest="action", required=True)

    status = sub.add_parser("status", help="show managed source and runtime state")
    status.add_argument("--json", action="store_true")
    status.add_argument("--check", action="store_true", help="fail on drifted installed sources/runtimes")
    status.set_defaults(func=cmd_status)

    sync = sub.add_parser("sync", help="sync reviewed sources and locked runtimes")
    sync.add_argument(
        "--source",
        choices=["all", "ponytail", "caveman", "pxpipe", "gigatoken"],
        default="all",
    )
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

    compress = sub.add_parser("memory-compress", help="explicit Caveman memory-file rewrite via Claude")
    compress.add_argument("file")
    compress.add_argument("--yes", action="store_true", help="confirm local file rewrite")
    compress.add_argument(
        "--allow-third-party-upload",
        action="store_true",
        help="confirm that file contents may be sent to Claude/Anthropic",
    )
    compress.add_argument("--timeout", type=float, default=300.0, help="maximum Caveman runtime in seconds")
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
    pxrun.add_argument("--startup-timeout", type=float, default=45.0)
    pxrun.add_argument("exec_argv", nargs=argparse.REMAINDER)
    pxrun.set_defaults(func=cmd_pxpipe_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (StackError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
