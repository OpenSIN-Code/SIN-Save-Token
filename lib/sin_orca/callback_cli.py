"""Operator CLI and service management for the SIN callback broker."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

from .callback_broker import BROKER_SCHEMA_VERSION, CallbackBrokerStore
from .callback_broker_service import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    api_token,
    sanitize_delivery,
    serve,
    token_path,
)

SERVICE_LABEL = "com.sin-orca.callback-broker"
SYSTEMD_UNIT = "sin-callback-broker.service"


def _print(value: object, *, exit_code: int = 0) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def _health_url() -> str:
    return os.getenv(
        "SIN_CALLBACK_BROKER_URL",
        f"http://127.0.0.1:{DEFAULT_PORT}",
    ).rstrip("/")


def _api(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    if method != "GET":
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        _health_url() + path,
        data=data,
        headers={
            "Authorization": f"Bearer {api_token()}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
    with opener.open(req, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("callback broker API returned a non-object payload")
    return payload


def _mode(path: Path) -> str:
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError:
        return "missing"


def doctor_report(store: CallbackBrokerStore) -> dict[str, Any]:
    issues: list[str] = []
    integrity = "error"
    schema = None

    # Repair private local broker permissions before evaluating health. These
    # paths contain transport metadata and the local control-plane bearer token.
    try:
        store.path.parent.chmod(0o700)
    except OSError:
        pass
    try:
        store.path.chmod(0o600)
    except OSError:
        pass
    token = token_path()
    try:
        token.chmod(0o600)
    except OSError:
        pass
    try:
        with store._connect() as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            row = db.execute(
                "SELECT value FROM broker_meta WHERE key='schema_version'"
            ).fetchone()
            schema = int(row[0]) if row is not None else None
    except Exception:
        issues.append("db-unavailable")
    if integrity != "ok":
        issues.append("db-integrity")
    if schema != BROKER_SCHEMA_VERSION:
        issues.append("schema-mismatch")

    permissions = {
        "db": _mode(store.path),
        "token": _mode(token),
        "state_dir": _mode(store.path.parent),
    }
    if permissions["db"] != "0600":
        issues.append("db-permissions")
    if permissions["token"] != "0600":
        issues.append("token-permissions")
    if permissions["state_dir"] != "0700":
        issues.append("state-dir-permissions")

    try:
        health = _api("GET", "/health")
    except Exception:
        health = {"ok": False, "status": "offline"}
        issues.append("api-offline")
    else:
        if health.get("ok") is not True:
            issues.append("api-unhealthy")
        if health.get("schema") != BROKER_SCHEMA_VERSION:
            issues.append("api-schema-mismatch")

    return {
        "ok": not issues,
        "service": "sin-callback-broker",
        "schema": schema,
        "db": str(store.path),
        "integrity": integrity,
        "permissions": permissions,
        "api": health,
        "issues": list(dict.fromkeys(issues)),
    }


def _service_executable() -> Path:
    return Path(__file__).resolve().parents[2] / "bin" / "sin-callback"


def _service_path() -> str:
    current = os.environ.get("PATH", "").strip()
    required = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    values = [part for part in current.split(os.pathsep) if part]
    for part in required:
        if part not in values:
            values.append(part)
    return os.pathsep.join(values)


def _macos_launch_agent_payload(executable: Path) -> dict[str, Any]:
    state_dir = Path.home() / ".local" / "state" / "sin-orca"
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(executable),
            "serve",
            "--host",
            DEFAULT_HOST,
            "--port",
            str(DEFAULT_PORT),
        ],
        "EnvironmentVariables": {"PATH": _service_path()},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(state_dir / "callback-broker.out.log"),
        "StandardErrorPath": str(state_dir / "callback-broker.err.log"),
    }


def _linux_systemd_unit(executable: Path) -> str:
    path_value = _service_path().replace("%", "%%")
    return "\n".join(
        [
            "[Unit]",
            "Description=SIN durable callback broker",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=PATH={path_value}",
            f"ExecStart={sys.executable} {executable} serve --host {DEFAULT_HOST} --port {DEFAULT_PORT}",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def install_service() -> dict[str, Any]:
    executable = _service_executable()
    if not executable.is_file():
        return {"ok": False, "installed": False, "error": "broker-executable-missing"}

    if sys.platform == "darwin":
        state_dir = Path.home() / ".local" / "state" / "sin-orca"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plist = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps(_macos_launch_agent_payload(executable), fmt=plistlib.FMT_XML))
        plist.chmod(0o600)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            check=False,
            capture_output=True,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "ok": result.returncode == 0,
            "installed": result.returncode == 0,
            "manager": "launchd",
            "label": SERVICE_LABEL,
            "service_file": str(plist),
        }

    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(_linux_systemd_unit(executable), encoding="utf-8")
        unit.chmod(0o600)
        reload_result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
            text=True,
        )
        enable_result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT],
            check=False,
            capture_output=True,
            text=True,
        )
        ok = reload_result.returncode == 0 and enable_result.returncode == 0
        return {
            "ok": ok,
            "installed": ok,
            "manager": "systemd-user",
            "label": SYSTEMD_UNIT,
            "service_file": str(unit),
        }

    return {"ok": False, "installed": False, "error": "service-manager-unsupported"}


def uninstall_service() -> dict[str, Any]:
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            check=False,
            capture_output=True,
        )
        plist.unlink(missing_ok=True)
        return {"ok": True, "installed": False, "manager": "launchd", "label": SERVICE_LABEL}

    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT],
            check=False,
            capture_output=True,
        )
        unit.unlink(missing_ok=True)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
        )
        return {"ok": True, "installed": False, "manager": "systemd-user", "label": SYSTEMD_UNIT}

    return {"ok": False, "installed": False, "error": "service-manager-unsupported"}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sin-callback",
        description="SIN durable callback delivery broker",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")
    p = sub.add_parser("list")
    p.add_argument("--state")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("inspect")
    p.add_argument("delivery_id")
    p = sub.add_parser("reconcile")
    p.add_argument("delivery_id")
    p = sub.add_parser("drain")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("sync")
    p.add_argument("--repo", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--interval", type=float, default=5.0)
    sub.add_parser("install")
    sub.add_parser("uninstall")
    args = parser.parse_args()
    store = CallbackBrokerStore()

    if args.command == "doctor":
        report = doctor_report(store)
        return _print(report, exit_code=0 if report["ok"] else 1)
    if args.command == "status":
        return _print(_api("GET", "/status"))
    if args.command == "list":
        rows = [sanitize_delivery(row) for row in store.list(state=args.state, limit=args.limit)]
        return _print({"ok": True, "callbacks": rows})
    if args.command == "inspect":
        row = store.get(args.delivery_id)
        return _print({"ok": row is not None, "callback": sanitize_delivery(row) if row else None})
    if args.command == "reconcile":
        return _print(_api("POST", f"/callbacks/{args.delivery_id}/reconcile"))
    if args.command == "drain":
        return _print(_api("POST", "/callbacks/drain", {"limit": args.limit}))
    if args.command == "sync":
        repository = str(Path(args.repo).expanduser().resolve())
        return _print(_api("POST", "/repositories/sync", {"repository": repository}))
    if args.command == "serve":
        serve(host=args.host, port=args.port, interval_seconds=args.interval)
        return 0
    if args.command == "install":
        result = install_service()
        return _print(result, exit_code=0 if result.get("ok") else 1)
    if args.command == "uninstall":
        result = uninstall_service()
        return _print(result, exit_code=0 if result.get("ok") else 1)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
