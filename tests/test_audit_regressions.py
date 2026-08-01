#!/usr/bin/env python3

import fcntl
import json
import runpy
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sin_cache import SinCache
from sin_capability import capability_prompt, load_capabilities
from sin_citation import CitationManager
from sin_context.provider_runtime import ProviderRuntime
from sin_memory import MemoryStore
from sin_orca import state
from sin_orca.cli import _load_config
from sin_orca.dispatch import select_created_terminal
from sin_orca.lease import ControllerLease
from sin_research import ResearchPipeline
from sin_review_context import ReviewContextBuilder


def test_repository_root_reports_git_failure() -> None:
    completed = subprocess.CompletedProcess(
        ["git"], 128, stdout="", stderr="fatal: not a git repository"
    )
    with patch("sin_orca.state.subprocess.run", return_value=completed):
        with pytest.raises(RuntimeError, match="not a git repository"):
            state.repository_root()


def test_read_events_waits_for_writer_lock(tmp_path: Path) -> None:
    with patch("sin_orca.state.state_root", lambda *a, **k: tmp_path):
        task_id = "locked-read"
        state.append_event(
            task_id,
            "task.created",
            {"task_hash": "sha256:test", "base_sha": "a" * 40},
            actor="controller",
        )
        lock_path = state.task_dir(task_id) / ".events.lock"
        result: list[dict] = []
        started = threading.Event()

        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            def reader() -> None:
                started.set()
                result.extend(state.read_events(task_id))

            thread = threading.Thread(target=reader)
            thread.start()
            assert started.wait(timeout=1)
            time.sleep(0.05)
            assert thread.is_alive()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(result) == 1


def test_terminal_selection_uses_expected_title_not_last_candidate() -> None:
    listing = {
        "result": {
            "terminals": [
                {"handle": "term-other", "title": "another concurrent task"},
                {"handle": "term-wanted", "title": "task-123"},
            ]
        }
    }
    assert select_created_terminal(
        listing,
        existing_handles={"term-old"},
        expected_title="task-123",
    ) == "term-wanted"


def test_terminal_selection_refuses_ambiguous_unlabelled_candidates() -> None:
    listing = {"terminals": [{"handle": "a"}, {"handle": "b"}]}
    assert select_created_terminal(
        listing, existing_handles=set(), expected_title="task-123"
    ) is None


def test_review_context_detects_async_methods_and_exact_test_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "@decorator\nclass Service:\n"
        "    async def refresh_token(self):\n        return True\n",
        encoding="utf-8",
    )
    (tmp_path / "test_service.py").write_text(
        "def test_refresh_token_extra():\n    pass\n", encoding="utf-8"
    )
    builder = ReviewContextBuilder(tmp_path)
    symbols = builder._extract_changed_symbols(
        [{"path": "service.py", "change_type": "modified"}]
    )
    names = {item["name"] for item in symbols}
    assert {"Service", "refresh_token"} <= names
    gap = next(
        item for item in builder._detect_test_gaps(symbols)
        if item["function"] == "refresh_token"
    )
    assert gap["has_direct_test"] is False


def test_review_context_preserves_rename_source_and_destination(tmp_path: Path) -> None:
    builder = ReviewContextBuilder(tmp_path)
    responses = [
        subprocess.CompletedProcess(
            ["git"], 0, stdout="R100\told.py\tnew.py\n", stderr=""
        ),
        subprocess.CompletedProcess(["git"], 0, stdout="", stderr=""),
    ]
    with patch("sin_review_context.run_command", side_effect=responses):
        files = builder._get_changed_files("a" * 40)
    assert files == [{
        "path": "new.py",
        "previous_path": "old.py",
        "change_type": "renamed",
        "lines_added": 0,
        "lines_removed": 0,
    }]


def test_malformed_orchestrator_config_fails_explicitly(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "orca-orchestrator.json").write_text("{broken", encoding="utf-8")
    with patch("sin_orca.cli.repository_root", return_value=tmp_path):
        with pytest.raises(RuntimeError, match="invalid orchestrator config"):
            _load_config()


def test_capability_prompt_rejects_path_traversal(tmp_path: Path) -> None:
    config = tmp_path / "capabilities.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "capabilities": {
            "escape": {
                "description": "fallback",
                "prompt_template": "../../outside.txt",
            }
        },
    }), encoding="utf-8")
    assert load_capabilities(config)["capabilities"]["escape"]
    with pytest.raises(ValueError, match="escapes config/prompts"):
        capability_prompt("escape", config)


def test_dynamic_subquestion_id_uses_max_existing_number() -> None:
    pipeline = ResearchPipeline()
    plan = {
        "subquestions": [
            {"id": "sq-01"},
            {"id": "sq-03"},
            {"id": "custom"},
        ],
        "open_questions": [],
    }
    pipeline.add_dynamic_subquestion(plan, "What changed?")
    assert plan["subquestions"][-1]["id"] == "sq-04"


def test_citation_import_rejects_unknown_source_reference() -> None:
    with pytest.raises(ValueError, match="unknown sources"):
        CitationManager.from_dict({
            "entries": [],
            "claims": [{
                "claim_id": "c1", "text": "claim", "source_ids": ["missing"]
            }],
        })


def test_memory_context_bounds_disk_reads(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    for index in range(205):
        store.write_l2_summary(f"topic-{index:03d}", f"summary {index}")
    context = store.context_for_task({"objective": "summary 204"})
    assert context["total_l2"] == 205
    assert context["scan_limited"] is True
    assert len(context["l2_summaries"]) == 10
    assert context["l2_summaries"][0]["topic"] == "topic-204"


def test_provider_process_stays_in_callers_session(tmp_path: Path) -> None:
    captured = {}
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    with patch(
        "sin_context.provider_runtime.subprocess.Popen",
        side_effect=recording_popen,
    ):
        result = ProviderRuntime._run_bounded(
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            timeout_seconds=5,
            maximum_output_chars=100,
        )
    assert result.returncode == 0
    assert captured["start_new_session"] is False


def test_cache_ttl_expires_even_frequently_used_entries(tmp_path: Path) -> None:
    cache = SinCache(tmp_path / "cache.db", ttl_seconds=10)
    try:
        key = cache.put("route", "provider", "query", "repo", "answer")
        cache.conn.execute(
            "UPDATE cache_entries SET created_at = ?, hit_count = 99 WHERE cache_key = ?",
            (0, key),
        )
        cache.conn.commit()
        assert cache.get("route", "provider", "query", "repo") is None
        assert cache.conn.execute(
            "SELECT COUNT(*) FROM cache_entries WHERE cache_key = ?", (key,)
        ).fetchone()[0] == 0
    finally:
        cache.close()


def test_lease_release_removes_lock_artifact(tmp_path: Path) -> None:
    manager = ControllerLease(tmp_path, owner="controller")
    lease = manager.acquire(ttl_seconds=30)
    manager.release(lease.token)
    assert not manager.lease_path.exists()
    assert not manager.lock_path.exists()


def run_node_hook(path: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_agent_grep_nudge_treats_makefile_as_file() -> None:
    result = run_node_hook(
        ROOT / "hooks" / "agent-grep-nudge.js",
        {"tool_name": "Grep", "tool_input": {"pattern": "target", "path": "Makefile"}},
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_git_classifier_handles_config_option() -> None:
    script = (
        "const g=require('./hooks/lib/git-cmd.js');"
        "if(!g.isGitSubcommand('git -c foo.bar=baz commit -m \\\"x\\\"','commit'))"
        "process.exit(1);"
    )
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_session_digest_rejects_unknown_format(tmp_path: Path) -> None:
    target = tmp_path / "unknown.txt"
    target.write_text("not json\n", encoding="utf-8")
    namespace = runpy.run_path(str(ROOT / "bin" / "session-digest"))
    assert namespace["_detect"](str(target)) is None


def test_agent_grep_no_match_message_is_stderr(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    result = subprocess.run(
        [str(ROOT / "bin" / "agent-grep"), "definitely-no-match", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no matches" in result.stderr


def test_dream_loads_session_digest_without_sourcefileloader() -> None:
    namespace = runpy.run_path(str(ROOT / "bin" / "dream"))
    digest = namespace["_load_digest"]()
    assert callable(digest._detect)
