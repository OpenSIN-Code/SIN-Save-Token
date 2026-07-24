"""
sin_capability – Capability-Loader für sin-orca Agent-Loop.

DeepTutor-Prinzip: Ein Loop, mehrere Fähigkeiten.
Tools werden nach Bedarf geladen und nicht konfigurierte entfernt.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

CAPABILITIES_PATH = Path(__file__).resolve().parent.parent / "config" / "capabilities.json"


@lru_cache(maxsize=16)
def _load_capabilities_cached(
    resolved_path: str,
    modified_ns: int,
    size: int,
) -> dict[str, Any]:
    del modified_ns, size
    with open(resolved_path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("capabilities config root must be an object")
    capabilities = value.get("capabilities", {})
    if not isinstance(capabilities, dict):
        raise ValueError("capabilities config must contain an object")
    return value


def load_capabilities(path: Optional[Path] = None) -> dict[str, Any]:
    target = (path or CAPABILITIES_PATH).expanduser().resolve()
    if not target.exists():
        return {"schema_version": 1, "capabilities": {}}
    metadata = target.stat()
    return _load_capabilities_cached(
        str(target), metadata.st_mtime_ns, metadata.st_size
    )


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

    if not isinstance(template_name, str):
        raise ValueError("prompt_template must be a string")
    prompts_root = (
        Path(__file__).resolve().parent.parent / "config" / "prompts"
    ).resolve()
    candidate = Path(template_name)
    if candidate.is_absolute():
        raise ValueError("prompt_template must be relative to config/prompts")
    template_path = (prompts_root / candidate).resolve()
    try:
        template_path.relative_to(prompts_root)
    except ValueError as error:
        raise ValueError("prompt_template escapes config/prompts") from error
    if template_path.is_file():
        return template_path.read_text(encoding="utf-8")

    return cap.get("description", "")
