"""
sin_memory – L1/L2/L3 Memory-Layer.

L1: rohe Trace-Ereignisse (events.jsonl)
L2: verdichtete Zusammenfassungen pro Task/Topic
L3: dauerhafte, übergreifende Synthese (verifizierte Entscheidungen)
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ZERO_HASH = "0" * 64


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_epoch() -> int:
    return int(time.time())


class MemoryStore:
    """Zustandslose Memory-Fassade. Alle Schreibwege laufen durch sie."""

    def __init__(self, state_root: Path):
        self.state_root = state_root
        self.l1_dir = state_root / "L1"
        self.l2_dir = state_root / "L2"
        self.l3_dir = state_root / "L3"

        for d in [self.l1_dir, self.l2_dir, self.l3_dir]:
            d.mkdir(parents=True, exist_ok=True)

    # ─── L1: Raw Events ─────────────────────────────────────────────────

    def append_l1_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        events_file = self.l1_dir / f"{task_id}.jsonl"

        existing = []
        if events_file.exists():
            with open(events_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        existing.append(json.loads(line))

        sequence = len(existing) + 1
        previous_hash = existing[-1]["event_hash"] if existing else ZERO_HASH

        material = {
            "sequence": sequence,
            "type": event_type,
            "timestamp": utc_now(),
            "payload": payload,
            "previous_hash": previous_hash,
        }

        event = {
            **material,
            "event_hash": sha256_text(json.dumps(material, sort_keys=True, separators=(",", ":"))),
        }

        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

        return event

    def read_l1_events(self, task_id: str) -> list[dict[str, Any]]:
        events_file = self.l1_dir / f"{task_id}.jsonl"
        if not events_file.exists():
            return []
        events = []
        with open(events_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    # ─── L2: Compressed Summaries ───────────────────────────────────────

    def write_l2_summary(
        self,
        topic: str,
        content: str,
        evidence_refs: Optional[list[dict[str, Any]]] = None,
        source_tasks: Optional[list[str]] = None,
        confidence: str = "draft",
    ) -> dict[str, Any]:
        entry = {
            "schema_version": 1,
            "level": "L2",
            "topic": topic,
            "content": content,
            "evidence_refs": evidence_refs or [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "confidence": confidence,
            "source_tasks": source_tasks or [],
        }

        entry_file = self.l2_dir / f"{topic}.json"
        with open(entry_file, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2, ensure_ascii=False)

        return entry

    def read_l2_summary(self, topic: str) -> Optional[dict[str, Any]]:
        entry_file = self.l2_dir / f"{topic}.json"
        if not entry_file.exists():
            return None
        with open(entry_file, encoding="utf-8") as f:
            return json.load(f)

    def list_l2_topics(self) -> list[str]:
        return [f.stem for f in self.l2_dir.glob("*.json")]

    def search_l2(self, query: str) -> list[dict[str, Any]]:
        query_lower = query.lower()
        results = []
        for topic in self.list_l2_topics():
            entry = self.read_l2_summary(topic)
            if entry and query_lower in entry.get("content", "").lower():
                results.append(entry)
        return results

    # ─── L3: Verified Decisions ─────────────────────────────────────────

    def write_l3_decision(
        self,
        decision_id: str,
        decision: str,
        rationale: str,
        evidence: Optional[list[str]] = None,
        source_tasks: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        entry = {
            "schema_version": 1,
            "level": "L3",
            "decision_id": decision_id,
            "decision": decision,
            "rationale": rationale,
            "evidence": evidence or [],
            "created_at": utc_now(),
            "status": "accepted",
            "source_tasks": source_tasks or [],
        }

        entry_file = self.l3_dir / f"{decision_id}.json"
        with open(entry_file, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2, ensure_ascii=False)

        return entry

    def read_l3_decision(self, decision_id: str) -> Optional[dict[str, Any]]:
        entry_file = self.l3_dir / f"{decision_id}.json"
        if not entry_file.exists():
            return None
        with open(entry_file, encoding="utf-8") as f:
            return json.load(f)

    def list_l3_decisions(self) -> list[str]:
        return [f.stem for f in self.l3_dir.glob("*.json")]

    def search_l3(self, query: str) -> list[dict[str, Any]]:
        query_lower = query.lower()
        results = []
        for did in self.list_l3_decisions():
            entry = self.read_l3_decision(did)
            if entry and (
                query_lower in entry.get("decision", "").lower()
                or query_lower in entry.get("rationale", "").lower()
            ):
                results.append(entry)
        return results

    # ─── Promote: L1 → L2 ──────────────────────────────────────────────

    def promote_to_l2(
        self,
        task_id: str,
        topic: str,
        summary: str,
        confidence: str = "draft",
    ) -> dict[str, Any]:
        events = self.read_l1_events(task_id)
        evidence_refs = [
            {"source": "events.jsonl", "sequence": e["sequence"]}
            for e in events[-10:]
        ]

        return self.write_l2_summary(
            topic=topic,
            content=summary,
            evidence_refs=evidence_refs,
            source_tasks=[task_id],
            confidence=confidence,
        )

    # ─── Promote: L2 → L3 ──────────────────────────────────────────────

    def promote_to_l3(
        self,
        topic: str,
        decision: str,
        rationale: str,
    ) -> dict[str, Any]:
        l2 = self.read_l2_summary(topic)
        source_tasks = l2.get("source_tasks", []) if l2 else []
        evidence = [
            ref.get("source", "") for ref in (l2.get("evidence_refs", []) if l2 else [])
        ]

        decision_id = f"DEC-{sha256_text(topic + decision)[:8].upper()}"

        return self.write_l3_decision(
            decision_id=decision_id,
            decision=decision,
            rationale=rationale,
            evidence=evidence,
            source_tasks=source_tasks,
        )

    # ─── Context for Codex ──────────────────────────────────────────────

    def context_for_task(self, task: dict[str, Any]) -> dict[str, Any]:
        relevant_l3 = []
        for did in self.list_l3_decisions():
            entry = self.read_l3_decision(did)
            if entry and entry.get("status") == "accepted":
                relevant_l3.append(entry)

        relevant_l2 = []
        for topic in self.list_l2_topics():
            entry = self.read_l2_summary(topic)
            if entry:
                relevant_l2.append(entry)

        return {
            "l3_decisions": relevant_l3[-20:],
            "l2_summaries": relevant_l2[-10:],
            "total_l3": len(relevant_l3),
            "total_l2": len(relevant_l2),
        }
