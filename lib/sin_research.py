"""
sin_research – Strukturierte Research-Pipeline (DeepTutor-Prinzipien).

Phasen:
1. Hauptfrage präzisieren
2. In beweisbare Teilfragen zerlegen
3. Teilfragen parallel bearbeiten
4. Evidenz sammeln mit Citation Manager
5. Widersprüche und offene Fragen sichtbar machen
6. Strukturierte Synthese für Codex
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sin_citation import CitationManager, sha256_text, utc_now


class ResearchDecomposer:
    """Zerlegt eine Hauptfrage in beweisbare Teilfragen."""

    def decompose(
        self,
        main_question: str,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        subquestions = self._generate_subquestions(main_question, context)

        return {
            "schema_version": 1,
            "main_question": main_question,
            "subquestions": [
                {
                    "id": f"sq-{i + 1:02d}",
                    "question": sq,
                    "status": "pending",
                    "evidence": [],
                    "synthesis": None,
                }
                for i, sq in enumerate(subquestions)
            ],
            "open_questions": [f"sq-{i + 1:02d}" for i in range(len(subquestions))],
            "created_at": utc_now(),
        }

    def _generate_subquestions(
        self,
        question: str,
        context: Optional[dict[str, Any]],
    ) -> list[str]:
        q_lower = question.lower()

        subqs = []

        if any(w in q_lower for w in ["how", "wie", "flow", "ablauf"]):
            subqs.append(f"Where does {self._extract_subject(question)} start?")
            subqs.append(f"What are the main steps in {self._extract_subject(question)}?")
            subqs.append(f"What are the failure modes in {self._extract_subject(question)}?")

        if any(w in q_lower for w in ["where", "wo", "find", "finde"]):
            subqs.append(f"Which files contain {self._extract_subject(question)}?")
            subqs.append(f"What are the entry points for {self._extract_subject(question)}?")

        if any(w in q_lower for w in ["why", "warum", "reason", "grund"]):
            subqs.append(f"What problem does {self._extract_subject(question)} solve?")
            subqs.append(f"What are the alternatives to {self._extract_subject(question)}?")

        if any(w in q_lower for w in ["security", "sicherheit", "vulnerability", "auth"]):
            subqs.append(f"What are the security assumptions for {self._extract_subject(question)}?")
            subqs.append(f"What could go wrong with {self._extract_subject(question)}?")

        if not subqs:
            subqs = [
                f"What is {self._extract_subject(question)}?",
                f"How does {self._extract_subject(question)} work?",
                f"What are the implications of {self._extract_subject(question)}?",
            ]

        return subqs

    def _extract_subject(self, question: str) -> str:
        words = question.split()
        stop = {"how", "what", "where", "why", "when", "which", "does", "is", "the", "a", "an", "to", "in", "of", "for", "wie", "wo", "was", "warum", "wird", "ist", "die", "der", "das"}
        subject_words = [w for w in words if w.lower().strip("?!.") not in stop]
        return " ".join(subject_words[:5]) if subject_words else question[:50]


class ResearchPipeline:
    """Führt eine Research-Durchführung durch."""

    def __init__(self, citation_manager: Optional[CitationManager] = None):
        self.decomposer = ResearchDecomposer()
        self.citations = citation_manager or CitationManager()

    def start_research(
        self,
        main_question: str,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        plan = self.decomposer.decompose(main_question, context)
        plan["status"] = "in_progress"
        plan["citations"] = self.citations.to_dict()
        return plan

    def answer_subquestion(
        self,
        plan: dict[str, Any],
        subquestion_id: str,
        answer: str,
        evidence: list[dict[str, Any]],
        can_add_subquestions: bool = True,
    ) -> dict[str, Any]:
        for sq in plan["subquestions"]:
            if sq["id"] == subquestion_id:
                sq["status"] = "answered"
                sq["synthesis"] = answer
                sq["evidence"] = evidence

                for ev in evidence:
                    self.citations.add_source(
                        source_id=f"{subquestion_id}-ev-{len(sq['evidence'])}",
                        path=ev.get("path", ""),
                        content_sha256=ev.get("content_sha256", ""),
                        lines=ev.get("lines"),
                    )

                self.citations.add_claim(
                    claim_id=subquestion_id,
                    text=answer,
                    source_ids=[f"{subquestion_id}-ev-{i}" for i in range(len(evidence))],
                    confidence="stated",
                )
                break

        plan["citations"] = self.citations.to_dict()
        plan["contradictions"] = self.citations.detect_contradictions()
        plan["open_questions"] = [
            sq["id"] for sq in plan["subquestions"] if sq["status"] == "pending"
        ]

        return plan

    def add_dynamic_subquestion(
        self,
        plan: dict[str, Any],
        question: str,
        parent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        new_id = f"sq-{len(plan['subquestions']) + 1:02d}"
        new_sq = {
            "id": new_id,
            "question": question,
            "status": "pending",
            "evidence": [],
            "synthesis": None,
            "parent": parent_id,
            "dynamic": True,
        }
        plan["subquestions"].append(new_sq)
        plan["open_questions"].append(new_id)
        return plan

    def synthesize(self, plan: dict[str, Any]) -> dict[str, Any]:
        answered = [sq for sq in plan["subquestions"] if sq["status"] == "answered"]
        pending = [sq for sq in plan["subquestions"] if sq["status"] == "pending"]

        synthesis_parts = []
        for sq in answered:
            synthesis_parts.append(f"**{sq['question']}**\n{sq['synthesis']}")

        synthesis = "\n\n".join(synthesis_parts) if synthesis_parts else "No answers yet."

        contradictions = self.citations.detect_contradictions()

        result = {
            "schema_version": 1,
            "main_question": plan["main_question"],
            "synthesis": synthesis,
            "answered_count": len(answered),
            "pending_count": len(pending),
            "contradictions": contradictions,
            "citations": self.citations.to_dict(),
            "subquestions": plan["subquestions"],
        }

        plan["status"] = "completed" if not pending else "partial"
        plan["synthesis_result"] = result

        return result

    def to_checkpoint(self, plan: dict[str, Any]) -> dict[str, Any]:
        answered = sum(1 for sq in plan["subquestions"] if sq["status"] == "answered")
        total = len(plan["subquestions"])

        return {
            "checkpoint": "research-progress",
            "main_question": plan["main_question"],
            "progress": f"{answered}/{total}",
            "open_questions": plan.get("open_questions", []),
            "contradictions": plan.get("contradictions", []),
        }
