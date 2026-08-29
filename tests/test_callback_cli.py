from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

from sin_orca import callback_cli
from sin_orca.callback_broker import CallbackBrokerStore, DeliveryRef


def _ref(tmp_path: Path) -> DeliveryRef:
    return DeliveryRef(
        delivery_id="gptwcd_" + "a" * 32,
        relay_id="gptwcr_" + "b" * 32,
        repository_root=str(tmp_path),
        callback_status="done",
        transport="opencode",
        target_id="ses_EXACT123",
        message_sha256="c" * 64,
        expires_at="2035-01-01T00:00:00+00:00",
    )


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SIN_CALLBACK_BROKER_STATE_DIR", str(state))
    monkeypatch.setenv("SIN_CALLBACK_BROKER_DB", str(state / "callback-broker.sqlite3"))
    monkeypatch.setenv("SIN_CALLBACK_BROKER_TOKEN_FILE", str(state / "callback-broker.token"))
    return state


def test_cli_list_redacts_internal_lease_fields(monkeypatch, tmp_path: Path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    store = CallbackBrokerStore()
    store.enqueue(_ref(tmp_path))
    claimed = store.claim_due(limit=1)
    assert claimed and claimed[0]["lease_token"]

    monkeypatch.setattr(sys, "argv", ["sin-callback", "list"])
    assert callback_cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    assert payload["ok"] is True
    assert "lease_token" not in rendered
    assert "lease_expires_at" not in rendered


def test_cli_inspect_redacts_internal_lease_fields(monkeypatch, tmp_path: Path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    store = CallbackBrokerStore()
    store.enqueue(_ref(tmp_path))
    claimed = store.claim_due(limit=1)
    assert claimed

    monkeypatch.setattr(sys, "argv", ["sin-callback", "inspect", _ref(tmp_path).delivery_id])
    assert callback_cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert "lease_token" not in json.dumps(payload)


def test_cli_status_uses_broker_control_plane(monkeypatch, tmp_path: Path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    seen: list[tuple[str, str]] = []

    def fake_api(method: str, path: str, body=None):
        seen.append((method, path))
        return {"ok": True, "schema": 2, "repositories": 3, "states": {"queued": 2}}

    monkeypatch.setattr(callback_cli, "_api", fake_api)
    monkeypatch.setattr(sys, "argv", ["sin-callback", "status"])
    assert callback_cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["states"]["queued"] == 2
    assert seen == [("GET", "/status")]


def test_doctor_fails_closed_when_api_is_offline(monkeypatch, tmp_path: Path, capsys) -> None:
    state = _isolate(monkeypatch, tmp_path)
    CallbackBrokerStore()
    token = state / "callback-broker.token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("test-token\n", encoding="utf-8")
    token.chmod(0o600)

    monkeypatch.setattr(callback_cli, "_api", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(sys, "argv", ["sin-callback", "doctor"])
    assert callback_cli.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["api"]["status"] == "offline"


def test_doctor_repairs_private_state_permissions(monkeypatch, tmp_path: Path, capsys) -> None:
    state = _isolate(monkeypatch, tmp_path)
    store = CallbackBrokerStore()
    token = state / "callback-broker.token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("test-token\n", encoding="utf-8")
    token.chmod(0o644)
    state.chmod(0o755)
    store.path.chmod(0o644)

    monkeypatch.setattr(callback_cli, "_api", lambda *args, **kwargs: {"ok": True, "schema": 2})
    monkeypatch.setattr(sys, "argv", ["sin-callback", "doctor"])
    assert callback_cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["permissions"]["db"] == "0600"
    assert payload["permissions"]["token"] == "0600"
    assert payload["permissions"]["state_dir"] == "0700"


def test_linux_systemd_unit_is_user_scoped_and_contains_no_secret_material(tmp_path: Path) -> None:
    executable = tmp_path / "sin-callback"
    unit = callback_cli._linux_systemd_unit(executable)
    assert "ExecStart=" in unit
    assert str(executable) in unit
    assert "WantedBy=default.target" in unit
    assert "User=" not in unit
    lowered = unit.casefold()
    assert "password" not in lowered
    assert "token=" not in lowered
    assert "gptwcb_" not in lowered
    assert "session-" not in lowered


def test_sst_installer_publishes_callback_cli() -> None:
    install = (Path(__file__).resolve().parents[1] / "bin" / "install.sh").read_text(encoding="utf-8")
    assert 'ln -sfn "$REPO_DIR/bin/sin-callback" "$BIN_DEST/sin-callback"' in install


def test_service_installation_is_global_not_per_delivery(monkeypatch, tmp_path: Path) -> None:
    state = _isolate(monkeypatch, tmp_path)
    executable = tmp_path / "sin-callback"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    with patch.object(callback_cli.Path, "home", return_value=tmp_path):
        mac = callback_cli._macos_launch_agent_payload(executable)
    assert mac["Label"] == "com.sin-orca.callback-broker"
    args = mac["ProgramArguments"]
    assert args[0] == sys.executable
    assert args[1] == str(executable)
    assert args[2] == "serve"
    assert not any("gptwcd_" in str(value) for value in args)
    assert not any("session-" in str(value) for value in args)


def test_ci_compiles_and_checks_callback_entrypoint() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "context-discipline.yml").read_text(encoding="utf-8")
    compile_block = workflow.split("- name: Compile all Python sources", 1)[1].split("- name: Structural audit", 1)[0]
    executable_block = workflow.split("- name: Check executable entrypoints", 1)[1].split("- name: Enforce orchestration invariants", 1)[0]
    assert "bin/sin-callback" in compile_block
    assert "test -x bin/sin-callback" in executable_block


def test_readme_repo_layout_lists_callback_cli_and_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    layout = readme.split("## Repo-Layout", 1)[1]
    assert "sin-callback" in layout
    assert "callback broker" in layout.casefold()
