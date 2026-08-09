#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_context.deeptutor_adapter import build_deeptutor_context  # noqa: E402


def test_deeptutor_adapter_keeps_dynamic_questions_and_citations() -> None:
    packet = build_deeptutor_context(
        "How does auth work?",
        dynamic_subquestions=["What evidence proves token expiry behavior?"],
        answers=[
            {
                "id": "sq-01",
                "answer": "The flow starts in the authentication handler.",
                "evidence": [
                    {
                        "path": "src/auth.py",
                        "content_sha256": "abc123",
                        "lines": "10-25",
                    }
                ],
            }
        ],
    )

    assert packet["adapter"] == "deeptutor"
    assert packet["allows_dynamic_subquestions"] is True
    assert packet["subquestions"][-1]["dynamic"] is True
    assert "sq-01" not in packet["open_questions"]
    assert packet["citations"]["entries"][0]["path"] == "src/auth.py"
    assert packet["citations"]["claims"][0]["source_ids"] == ["sq-01-ev-0"]


def test_deeptutor_adapter_rejects_unknown_answer_id() -> None:
    with pytest.raises(ValueError, match="unknown subquestion"):
        build_deeptutor_context(
            "How does auth work?",
            answers=[{"id": "sq-99", "answer": "Nope", "evidence": []}],
        )


class StubReviewBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]] | None]] = []

    def build_review_context(
        self,
        base_sha: str,
        graphify_paths: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((base_sha, graphify_paths))
        return {
            "base_sha": base_sha,
            "changed_files": [{"path": "src/auth.py", "change_type": "modified"}],
            "changed_symbols": [{"name": "validate_token", "file": "src/auth.py"}],
            "affected_flows": [
                {
                    "flow": "authentication",
                    "functions": ["validate_token"],
                    "criticality": "high",
                }
            ],
            "test_gaps": [
                {
                    "function": "validate_token",
                    "has_direct_test": False,
                    "risk": "medium",
                }
            ],
            "risk_signals": [
                {
                    "type": "security_keyword",
                    "symbol": "validate_token",
                    "score": 0.2,
                }
            ],
            "recommended_review_order": ["validate_token"],
            "total_risk_score": 0.2,
            "uncertainties": [],
            "crg_advisory": {
                "ok": True,
                "provider": "code-review-graph",
                "authoritative": False,
            },
            "diff_hash": "abc",
        }


def test_review_context_adapter_emits_signals_and_blind_packet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = runpy.run_path(str(ROOT / "bin" / "sin-review-context"))
    command_adapter = namespace["command_adapter"]
    builder = StubReviewBuilder()

    def builder_factory(worktree: Path) -> StubReviewBuilder:
        assert worktree == tmp_path.resolve()
        return builder

    args = argparse.Namespace(
        worktree=str(tmp_path),
        base_sha="HEAD",
        objective="Review current authentication changes",
    )
    with patch.dict(
        command_adapter.__globals__,
        {
            "ReviewContextBuilder": builder_factory,
            "bounded_diff": lambda **_: {
                "text": "diff --git a/src/auth.py b/src/auth.py\n+change\n"
            },
        },
    ):
        assert command_adapter(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert builder.calls == [("HEAD", [])]
    assert payload["adapter"] == "crg"
    assert payload["changed_symbols"][0]["name"] == "validate_token"
    assert payload["affected_flows"][0]["flow"] == "authentication"
    assert payload["test_gaps"][0]["has_direct_test"] is False
    assert payload["risk_signals"][0]["score"] == 0.2
    blind = payload["blind_review_packet"]
    assert blind["crg_authoritative"] is False
    assert blind["original_task"]["allowed_paths"] == ["src/auth.py"]
    assert "UNTRUSTED_EVIDENCE_BEGIN" in blind["bounded_diff"]
