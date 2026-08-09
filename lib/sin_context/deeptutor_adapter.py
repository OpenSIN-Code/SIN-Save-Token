"""DeepTutor-style research adapter for the canonical context broker."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sin_research import ResearchPipeline

MAX_QUESTION_CHARS = 8_000
MAX_DYNAMIC_SUBQUESTIONS = 32
MAX_ANSWERS = 64


def build_deeptutor_context(
    question: str,
    *,
    dynamic_subquestions: Iterable[str] = (),
    answers: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a serializable research-decomposition context packet.

    Research semantics stay in ``ResearchPipeline`` so decomposition, dynamic
    subquestions, citation bookkeeping, and contradiction handling have one
    canonical implementation.
    """
    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("research question must not be empty")
    if len(normalized_question) > MAX_QUESTION_CHARS:
        raise ValueError("research question exceeds adapter limit")

    pipeline = ResearchPipeline()
    plan = pipeline.start_research(normalized_question)

    for index, candidate in enumerate(dynamic_subquestions):
        if index >= MAX_DYNAMIC_SUBQUESTIONS:
            raise ValueError("too many dynamic subquestions")
        subquestion = candidate.strip()
        if not subquestion:
            continue
        if len(subquestion) > MAX_QUESTION_CHARS:
            raise ValueError("dynamic subquestion exceeds adapter limit")
        pipeline.add_dynamic_subquestion(plan, subquestion)

    known_ids = {item["id"] for item in plan["subquestions"]}
    for index, item in enumerate(answers):
        if index >= MAX_ANSWERS:
            raise ValueError("too many research answers")
        subquestion_id = str(item.get("id", "")).strip()
        answer = str(item.get("answer", "")).strip()
        evidence = item.get("evidence", [])
        if subquestion_id not in known_ids:
            raise ValueError("research answer references unknown subquestion")
        if not answer:
            raise ValueError("research answer must not be empty")
        if not isinstance(evidence, list) or not all(
            isinstance(entry, dict) for entry in evidence
        ):
            raise ValueError("research evidence must be a list of objects")
        pipeline.answer_subquestion(
            plan,
            subquestion_id,
            answer,
            evidence,
        )

    return {
        "schema_version": 1,
        "adapter": "deeptutor",
        "kind": "research-decomposition",
        "main_question": plan["main_question"],
        "status": plan["status"],
        "created_at": plan["created_at"],
        "allows_dynamic_subquestions": True,
        "subquestions": plan["subquestions"],
        "open_questions": plan["open_questions"],
        "citations": plan["citations"],
        "contradictions": plan.get("contradictions", []),
    }
