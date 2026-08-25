"""Hermetic unit tests for the fail-closed SIN memory gateway contract.

No network access, no live OpenViking writes, no filesystem access: the backend is
always the in-memory fake.
"""

from __future__ import annotations

import pytest

from sin_memory_gateway import (
    BackendFailureError,
    CommitResult,
    InMemoryBackend,
    PersistenceReceipt,
    ReceiptStatus,
    RecordStatus,
    RejectedRecordError,
    SinMemoryGateway,
    build_canonical_record,
    validate_content,
    validate_provenance,
)

EVIDENCE = "a" * 64


def make_provenance() -> dict[str, str]:
    return {"source": "unit-test", "evidence_sha256": EVIDENCE, "actor": "tester"}


def make_gateway(fail_commits: bool = False) -> SinMemoryGateway:
    return SinMemoryGateway(InMemoryBackend(fail_commits=fail_commits))


class TestValidRecordAcceptance:
    def test_valid_record_is_accepted_with_committed_receipt(self):
        gateway = make_gateway()
        result = gateway.commit_record(
            record_id="rec-001",
            content="Deployed sinchat via cloudflared tunnel on 2026-08-23.",
            provenance=make_provenance(),
        )
        assert isinstance(result, CommitResult)
        assert result.accepted is True
        assert result.reason is None
        assert isinstance(result.receipt, PersistenceReceipt)
        assert result.receipt.status is ReceiptStatus.COMMITTED
        assert result.receipt.ok is True

    def test_build_canonical_record_normalizes_and_hashes(self):
        record = build_canonical_record(
            record_id="rec-002",
            content="  OpenViking is the memory backend.  ",
            provenance=make_provenance(),
        )
        assert record.content == "OpenViking is the memory backend."
        assert record.status is RecordStatus.ACTIVE
        assert len(record.content_hash()) == 64

    def test_receipt_binds_record_identity(self):
        gateway = make_gateway()
        receipt = gateway.commit_record(
            record_id="rec-003",
            content="Writer reservation is exclusive per task.",
            provenance=make_provenance(),
        ).receipt
        assert receipt.record_id == "rec-003"
        assert len(receipt.receipt_id) > 0


class TestRejectedInput:
    @pytest.mark.parametrize(
        "content",
        [
            "I think the deploy failed yesterday.",
            "Maybe the token rotates every hour.",
            "This is just a guess about the schema.",
            "Unverified claim from an unknown chat.",
            "Probably fine to skip verification.",
            "",
        ],
    )
    def test_speculative_or_empty_content_is_rejected(self, content):
        with pytest.raises(RejectedRecordError):
            validate_content(content)

    @pytest.mark.parametrize(
        "content",
        [
            "api_key = sk-abcdefghijklmnopqrstuvwx",
            "password=hunter2supersecret",
            "bearer: BearerTokenValue1234567890",
            "github token ghp_abcdefghijklmnopqrstuvwxyz123456",
            "aws key AKIAIOSFODNN7EXAMPLE here",
            "slack webhook xoxb-123456789012-abcdef",
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
    )
    def test_secret_shaped_content_is_rejected(self, content):
        with pytest.raises(RejectedRecordError):
            validate_content(content)

    def test_non_string_content_is_rejected(self):
        with pytest.raises(RejectedRecordError):
            validate_content({"note": "dict content not allowed"})

    def test_missing_provenance_field_is_rejected(self):
        bad = {"source": "x", "actor": "y"}
        with pytest.raises(RejectedRecordError):
            validate_provenance(bad)

    def test_bad_evidence_hash_is_rejected(self):
        bad = make_provenance()
        bad["evidence_sha256"] = "not-a-hash"
        with pytest.raises(RejectedRecordError):
            validate_provenance(bad)

    def test_unsafe_identifier_is_rejected(self):
        with pytest.raises(RejectedRecordError):
            build_canonical_record(
                record_id="../escape",
                content="Valid factual statement.",
                provenance=make_provenance(),
            )

    def test_rejected_records_never_reach_the_backend(self):
        backend = InMemoryBackend()
        gateway = SinMemoryGateway(backend)
        result = gateway.commit_record(
            record_id="rec-bad",
            content="maybe this works",
            provenance=make_provenance(),
        )
        assert result.accepted is False
        assert result.receipt is None
        assert backend.records == {}


class TestReceiptContract:
    def test_success_requires_committed_receipt(self):
        gateway = make_gateway()
        result = gateway.commit_record(
            record_id="rec-010",
            content="Factual: tests run hermetically.",
            provenance=make_provenance(),
        )
        assert result.accepted is (result.receipt is not None and result.receipt.ok)

    def test_failed_receipt_status_blocks_acceptance(self):
        gateway = make_gateway()
        failing = InMemoryBackend()
        failing.fail_commits = True
        result = SinMemoryGateway(failing).commit_record(
            record_id="rec-011",
            content="Factual statement.",
            provenance=make_provenance(),
        )
        assert result.accepted is False


class TestBackendFailure:
    def test_backend_failure_yields_no_success_receipt(self):
        gateway = make_gateway(fail_commits=True)
        result = gateway.commit_record(
            record_id="rec-020",
            content="Factual statement about outage handling.",
            provenance=make_provenance(),
        )
        assert result.accepted is False
        assert result.receipt is None
        assert result.reason and "backend failure" in result.reason

    def test_backend_failure_never_persists_the_record(self):
        backend = InMemoryBackend(fail_commits=True)
        gateway = SinMemoryGateway(backend)
        gateway.commit_record(
            record_id="rec-021",
            content="Another factual statement.",
            provenance=make_provenance(),
        )
        assert backend.records == {}

    def test_recall_failure_raises_fail_closed_error(self):
        class ExplodingBackend(InMemoryBackend):
            def recall(self, query, statuses=(RecordStatus.ACTIVE,)):
                raise BackendFailureError("recall outage")

        gateway = SinMemoryGateway(ExplodingBackend())
        with pytest.raises(BackendFailureError):
            gateway.recall_records("anything")


class TestRecallFiltering:
    def test_superseded_records_are_excluded_from_recall(self):
        gateway = make_gateway()
        gateway.commit_record(
            record_id="rec-030",
            content="Tunnel endpoint is sinchat.delqhi.com.",
            provenance=make_provenance(),
        )
        result = gateway.supersede_record(
            record_id="rec-030",
            new_record_id="rec-031",
            new_content="Tunnel endpoint moved to sinchat2.delqhi.com.",
            provenance=make_provenance(),
        )
        assert result.accepted is True
        active = gateway.recall_records("tunnel endpoint")
        ids = {r.record_id for r in active}
        assert "rec-030" not in ids
        assert "rec-031" in ids

    def test_inactive_records_are_excluded_from_recall(self):
        backend = InMemoryBackend()
        gateway = SinMemoryGateway(backend)
        gateway.commit_record(
            record_id="rec-040",
            content="Cache vacuum procedure documented.",
            provenance=make_provenance(),
        )
        stale = backend.records["rec-040"]
        backend.records["rec-040"] = type(stale)(
            record_id=stale.record_id,
            content=stale.content,
            provenance=stale.provenance,
            created_at=stale.created_at,
            status=RecordStatus.INACTIVE,
            supersedes=None,
        )
        assert gateway.recall_records("cache vacuum") == []

    def test_default_recall_only_returns_active(self):
        backend = InMemoryBackend()
        gateway = SinMemoryGateway(backend)
        gateway.commit_record(
            record_id="rec-050",
            content="First fact about the fleet.",
            provenance=make_provenance(),
        )
        gateway.commit_record(
            record_id="rec-051",
            content="Second fact about the fleet.",
            provenance=make_provenance(),
        )
        all_statuses = tuple(RecordStatus)
        recalled = backend.recall("fleet", statuses=all_statuses)
        assert len(recalled) == 2
        active_only = gateway.recall_records("fleet")
        assert {r.status for r in active_only} == {RecordStatus.ACTIVE}
