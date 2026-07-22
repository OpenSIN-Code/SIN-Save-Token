#!/usr/bin/env python3
"""Context packet builder for SIN agents.

Produces a strictly typed, honest context packet from broker results.
Only fields that are actually populated are included.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


FILE_PATTERN = re.compile(r"[/\\]?([\w./-]+\.\w{1,10})")


@dataclass
class ContextPacket:
    answer: str
    files: list[str] = field(default_factory=list)
    uncertainty: str = ""
    next_read: list[str] = field(default_factory=list)
    approx_tokens: int = 0
    provider: str = ""
    route: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "answer": self.answer,
            "approx_tokens": self.approx_tokens,
            "provider": self.provider,
            "route": self.route,
        }
        if self.files:
            result["files"] = self.files
        if self.uncertainty:
            result["uncertainty"] = self.uncertainty
        if self.next_read:
            result["next_read"] = self.next_read
        return result

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


def extract_files(text: str, max_files: int = 10) -> list[str]:
    candidates = FILE_PATTERN.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for match in candidates:
        if match not in seen and len(result) < max_files:
            seen.add(match)
            result.append(match)
    return result


def detect_uncertainty(text: str) -> str:
    markers = [
        (r"\bnot sure\b", "Result expresses uncertainty"),
        (r"\bunclear\b", "Result is unclear"),
        (r"\berror\b", "Result may contain errors"),
        (r"\bfailed\b", "Provider may have failed"),
    ]
    for pattern, message in markers:
        if re.search(pattern, text, re.IGNORECASE):
            return message
    return ""


def suggest_next_read(answer: str, files: list[str]) -> list[str]:
    suggestions: list[str] = files[:3] if files else []
    if "definition" in answer.lower() or "declared" in answer.lower():
        suggestions.append("(check definition site)")
    return suggestions[:3]


def build_packet(
    text: str,
    provider: str,
    route: str,
    approx_tokens: int,
) -> ContextPacket:
    files = extract_files(text)
    uncertainty = detect_uncertainty(text)
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
    )
