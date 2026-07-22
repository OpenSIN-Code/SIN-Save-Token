#!/usr/bin/env python3
"""Context packet builder for SIN agents.

Produces a strictly typed context packet from broker results.
Each packet contains: answer, evidence, files, uncertainty, next_read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class Evidence:
    source: str
    snippet: str
    relevance: float


@dataclass
class ContextPacket:
    answer: str
    evidence: list[Evidence] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    uncertainty: str = ""
    next_read: list[str] = field(default_factory=list)
    approx_tokens: int = 0
    provider: str = ""
    route: str = ""
    novelty_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "evidence": [asdict(e) for e in self.evidence],
            "files": self.files,
            "uncertainty": self.uncertainty,
            "next_read": self.next_read,
            "approx_tokens": self.approx_tokens,
            "provider": self.provider,
            "route": self.route,
            "novelty_score": self.novelty_score,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def summary(self) -> str:
        lines = [self.answer]
        if self.uncertainty:
            lines.append(f"\n⚠ {self.uncertainty}")
        if self.files:
            lines.append(f"\nFiles: {', '.join(self.files[:5])}")
        if self.next_read:
            lines.append(f"\nNext: {self.next_read[0]}")
        return "\n".join(lines)


FILE_PATTERN = re.compile(r"[/\\]?([\w./-]+\.\w{1,10})")
SYMBOL_PATTERN = re.compile(r"\b([A-Z][a-zA-Z0-9_]{2,})\b")


def extract_files(text: str, max_files: int = 10) -> list[str]:
    candidates = FILE_PATTERN.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for match in candidates:
        if match not in seen and len(result) < max_files:
            seen.add(match)
            result.append(match)
    return result


def extract_symbols(text: str, max_symbols: int = 5) -> list[str]:
    candidates = SYMBOL_PATTERN.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for match in candidates:
        if match not in seen and len(result) < max_symbols:
            seen.add(match)
            result.append(match)
    return result


def estimate_novelty(answer: str, prior_context: str) -> float:
    """Estimate how much new information the answer contains compared to prior context."""
    if not prior_context:
        return 1.0

    answer_words = set(answer.lower().split())
    prior_words = set(prior_context.lower().split())

    if not answer_words:
        return 0.0

    new_words = answer_words - prior_words
    return len(new_words) / len(answer_words)


def detect_uncertainty(text: str) -> str:
    """Detect uncertainty markers in the result."""
    uncertainty_markers = [
        (r"\bnot sure\b", "Result expresses uncertainty"),
        (r"\bunclear\b", "Result is unclear"),
        (r"\bpossibly\b", "Result contains speculation"),
        (r"\bmight be\b", "Result contains speculation"),
        (r"\berror\b", "Result may contain errors"),
        (r"\bfailed\b", "Provider may have failed"),
    ]
    for pattern, message in uncertainty_markers:
        if re.search(pattern, text, re.IGNORECASE):
            return message
    return ""


def suggest_next_read(answer: str, files: list[str]) -> list[str]:
    """Suggest files to read next based on the answer content."""
    suggestions: list[str] = []
    if files:
        suggestions.extend(files[:3])

    if "definition" in answer.lower() or "declared" in answer.lower():
        suggestions.append("(check definition site)")
    if "called by" in answer.lower() or "caller" in answer.lower():
        suggestions.append("(check caller sites)")

    return suggestions[:3]


def build_packet(
    text: str,
    provider: str,
    route: str,
    approx_tokens: int,
    prior_context: str = "",
) -> ContextPacket:
    """Build a structured context packet from broker output."""
    files = extract_files(text)
    uncertainty = detect_uncertainty(text)
    novelty = estimate_novelty(text, prior_context)
    next_read = suggest_next_read(text, files)

    answer = text.strip()
    if len(answer) > 2000:
        answer = answer[:2000] + "\n[… truncated]"

    return ContextPacket(
        answer=answer,
        files=files,
        uncertainty=uncertainty,
        next_read=next_read,
        approx_tokens=approx_tokens,
        provider=provider,
        route=route,
        novelty_score=novelty,
    )
