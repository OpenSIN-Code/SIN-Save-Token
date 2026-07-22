#!/usr/bin/env python3
"""Fail CI on duplicate definitions and obvious unreachable statements."""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str
    scope: str


def python_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        yield path


def statement_fingerprint(statement: ast.stmt) -> str:
    return ast.dump(statement, include_attributes=False)


def scan_block(
    *, body: list[ast.stmt], path: Path, scope: str,
    findings: list[Finding], function_scope: bool,
) -> None:
    definitions: dict[str, ast.AST] = {}
    previous_fingerprint: str | None = None
    terminated = False

    for statement in body:
        line = getattr(statement, "lineno", 0)

        if terminated:
            findings.append(Finding(path=str(path), line=line, code="UNREACHABLE_STATEMENT", message="Statement appears after unconditional return/raise/break/continue.", scope=scope))

        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            previous = definitions.get(statement.name)
            if previous is not None:
                findings.append(Finding(path=str(path), line=line, code="DUPLICATE_DEFINITION", message=f"{statement.name!r} already defined at line {getattr(previous, 'lineno', 0)}.", scope=scope))
            else:
                definitions[statement.name] = statement

        fingerprint = statement_fingerprint(statement)
        harmless = isinstance(statement, ast.Pass) or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str))

        if not harmless and fingerprint == previous_fingerprint:
            findings.append(Finding(path=str(path), line=line, code="DUPLICATE_ADJACENT_STATEMENT", message="Adjacent statement is structurally identical to its predecessor.", scope=scope))

        previous_fingerprint = fingerprint

        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan_block(body=statement.body, path=path, scope=f"{scope}.{statement.name}", findings=findings, function_scope=True)
        elif isinstance(statement, ast.ClassDef):
            scan_block(body=statement.body, path=path, scope=f"{scope}.{statement.name}", findings=findings, function_scope=False)
        elif isinstance(statement, ast.If):
            scan_block(body=statement.body, path=path, scope=f"{scope}.if", findings=findings, function_scope=function_scope)
            scan_block(body=statement.orelse, path=path, scope=f"{scope}.else", findings=findings, function_scope=function_scope)
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            scan_block(body=statement.body, path=path, scope=f"{scope}.loop", findings=findings, function_scope=function_scope)
        elif isinstance(statement, ast.Try):
            scan_block(body=statement.body, path=path, scope=f"{scope}.try", findings=findings, function_scope=function_scope)
            for index, handler in enumerate(statement.handlers):
                scan_block(body=handler.body, path=path, scope=f"{scope}.except[{index}]", findings=findings, function_scope=function_scope)
            scan_block(body=statement.finalbody, path=path, scope=f"{scope}.finally", findings=findings, function_scope=function_scope)

        terminated = function_scope and isinstance(statement, TERMINATORS)


def scan_file(path: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        return [Finding(path=str(path), line=getattr(error, "lineno", 0) or 0, code="PARSE_ERROR", message=str(error), scope="<module>")]
    findings: list[Finding] = []
    scan_block(body=tree.body, path=path, scope="<module>", findings=findings, function_scope=False)
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "lib").resolve()
    findings = [finding for path in python_files(root) for finding in scan_file(path)]
    print(json.dumps({"ok": not findings, "root": str(root), "finding_count": len(findings), "findings": [asdict(item) for item in findings]}, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
