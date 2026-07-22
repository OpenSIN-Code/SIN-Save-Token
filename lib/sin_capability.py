"""
sin_capability – Capability-Loader für sin-orca Agent-Loop.

DeepTutor-Prinzip: Ein Loop, mehrere Fähigkeiten.
Tools werden nach Bedarf geladen und nicht konfigurierte entfernt.
"""

import json
from pathlib import Path
from typing import Any, Optional

CAPABILITIES_PATH = Path(__file__).resolve().parent.parent / "config" / "capabilities.json"


def load_capabilities(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or CAPABILITIES_PATH
    if not path.exists():
        return {"schema_version": 1, "capabilities": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_capability(name: str, path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    caps = load_capabilities(path)
    return caps.get("capabilities", {}).get(name)


def list_capabilities(path: Optional[Path] = None) -> list[str]:
    caps = load_capabilities(path)
    return list(caps.get("capabilities", {}).keys())


def build_tool_list(
    capability_name: str,
    available_tools: set[str],
    path: Optional[Path] = None,
) -> list[str]:
    cap = get_capability(capability_name, path)
    if cap is None:
        return []

    required = set(cap.get("tools", []))
    return sorted(required & available_tools)


def build_prompt_context(
    capability_name: str,
    task: dict[str, Any],
    path: Optional[Path] = None,
) -> dict[str, Any]:
    cap = get_capability(capability_name, path)
    if cap is None:
        return {"error": f"unknown capability: {capability_name}"}

    return {
        "capability": capability_name,
        "description": cap.get("description", ""),
        "objective": task.get("objective", ""),
        "steps": task.get("steps", []),
        "allowed_paths": task.get("allowed_paths", []),
        "forbidden_paths": task.get("forbidden_paths", []),
        "acceptance": task.get("acceptance", []),
        "non_goals": task.get("non_goals", []),
        "constraints": task.get("constraints", ""),
        "allows_dynamic_subquestions": cap.get("allows_dynamic_subquestions", False),
        "requires_approval": cap.get("requires_approval", False),
    }


def capability_prompt(capability_name: str, path: Optional[Path] = None) -> str:
    cap = get_capability(capability_name, path)
    if cap is None:
        return ""

    template_name = cap.get("prompt_template", "")
    if not template_name:
        return cap.get("description", "")

    template_path = Path(__file__).resolve().parent.parent / "config" / "prompts" / template_name
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    return cap.get("description", "")
