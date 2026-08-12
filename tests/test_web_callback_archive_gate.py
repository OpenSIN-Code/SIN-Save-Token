from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sin_orca.web_callbacks import (
    acknowledge_callback,
    callback_path,
    callback_status,
    mark_archive_verified,
)


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def done_record(repo: Path) -> tuple[str, str, str]:
    token = "gptwcb_" + "a" * 32
    delivery_id = "gptwcd_" + "b" * 32
    url = "https://chatgpt.com/c/archivegate1234"
    path = callback_path(repo, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token": token,
                "status": "sent",
                "callback_status": "done",
                "task_id": "T-0140",
                "round": 4,
                "repository_root": str(repo.resolve()),
                "delivery_id": delivery_id,
                "expires_at": "2999-01-01T00:00:00+00:00",
                "conversation": {"url": url, "page_id": "page-1", "profile": "OpenSIN"},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return token, delivery_id, url


def test_done_receipt_fails_closed_until_exact_archive_proof(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    token, delivery_id, _url = done_record(repo)

    with pytest.raises(RuntimeError, match="archive-and-close gate is not verified"):
        acknowledge_callback(repository=repo, token=token, delivery_id=delivery_id)

    assert callback_status(repository=repo, token=token)["status"] == "sent"


def test_exact_archive_proof_allows_done_receipt(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    token, delivery_id, url = done_record(repo)

    proof = mark_archive_verified(
        repository=repo,
        token=token,
        delivery_id=delivery_id,
        conversation_url=url,
        closed_tab_count=2,
    )
    result = acknowledge_callback(repository=repo, token=token, delivery_id=delivery_id)

    assert proof["status"] == "callback-archive-verified"
    assert result["status"] == "callback-acknowledged"
    saved = json.loads(callback_path(repo, token).read_text(encoding="utf-8"))
    assert saved["archive_gate"]["conversation_url"] == url
    assert saved["archive_gate"]["closed_tab_count"] == 2


def test_archive_proof_rejects_wrong_identity_or_zero_closed_tabs(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    token, delivery_id, url = done_record(repo)

    with pytest.raises(RuntimeError, match="conversation does not match"):
        mark_archive_verified(
            repository=repo,
            token=token,
            delivery_id=delivery_id,
            conversation_url="https://chatgpt.com/c/otherchat1234",
            closed_tab_count=1,
        )
    with pytest.raises(RuntimeError, match="at least one closed"):
        mark_archive_verified(
            repository=repo,
            token=token,
            delivery_id=delivery_id,
            conversation_url=url,
            closed_tab_count=0,
        )
