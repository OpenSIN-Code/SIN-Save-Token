"""
sin_citation – Evidenzgebundene Citation-Verwaltung.

Jede Aussage bekommt eine Quellenreferenz mit Content-Hash.
Widersprüche werden sichtbar gemacht.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CitationManager:
    """Verwaltet Quellenangaben für Forschungsergebnisse."""

    def __init__(self):
        self.entries: list[dict[str, Any]] = []
        self.claims: list[dict[str, Any]] = []

    def add_source(
        self,
        source_id: str,
        path: str,
        content_sha256: str,
        lines: Optional[str] = None,
        url: Optional[str] = None,
        access_date: Optional[str] = None,
    ) -> dict[str, Any]:
        entry = {
            "source_id": source_id,
            "path": path,
            "content_sha256": content_sha256,
            "lines": lines,
            "url": url,
            "access_date": access_date or utc_now(),
        }
        self.entries.append(entry)
        return entry

    def add_file_source(
        self,
        source_id: str,
        file_path: Path,
        lines: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not file_path.is_file():
            return None

        content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        return self.add_source(
            source_id=source_id,
            path=str(file_path),
            content_sha256=content_hash,
            lines=lines,
        )

    def add_claim(
        self,
        claim_id: str,
        text: str,
        source_ids: list[str],
        confidence: str = "stated",
    ) -> dict[str, Any]:
        claim = {
            "claim_id": claim_id,
            "text": text,
            "source_ids": source_ids,
            "confidence": confidence,
            "created_at": utc_now(),
        }
        self.claims.append(claim)
        return claim

    def add_verified_claim(
        self,
        claim_id: str,
        text: str,
        source_ids: list[str],
    ) -> dict[str, Any]:
        return self.add_claim(claim_id, text, source_ids, confidence="verified")

    def detect_contradictions(self) -> list[dict[str, Any]]:
        contradictions = []
        for i, c1 in enumerate(self.claims):
            for c2 in self.claims[i + 1:]:
                if self._might_contradict(c1, c2):
                    contradictions.append({
                        "claim_a": c1["claim_id"],
                        "claim_b": c2["claim_id"],
                        "text_a": c1["text"],
                        "text_b": c2["text"],
                        "reason": "opposite_or_incompatible",
                    })
        return contradictions

    def _might_contradict(self, c1: dict, c2: dict) -> bool:
        negations = {"not", "no", "never", "neither", "nor", "cannot", "false"}
        words1 = set(c1["text"].lower().split())
        words2 = set(c2["text"].lower().split())

        if words1 & negations and not (words2 & negations):
            overlap = len(words1 - negations & words2) / max(len(words1 | words2), 1)
            return overlap > 0.5

        if words2 & negations and not (words1 & negations):
            overlap = len(words1 & words2 - negations) / max(len(words1 | words2), 1)
            return overlap > 0.5

        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "claims": self.claims,
            "contradictions": self.detect_contradictions(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CitationManager":
        mgr = cls()
        mgr.entries = data.get("entries", [])
        mgr.claims = data.get("claims", [])
        return mgr

    def evidence_refs_for_claim(self, claim_id: str) -> list[dict[str, Any]]:
        claim = next((c for c in self.claims if c["claim_id"] == claim_id), None)
        if not claim:
            return []

        refs = []
        for sid in claim.get("source_ids", []):
            source = next((s for s in self.entries if s["source_id"] == sid), None)
            if source:
                refs.append(source)
        return refs
