"""
sin_review_context – CRG-Adapter als Review-Sensor.

Verwendet code-review-graph ausschließlich für:
- Zuordnung geänderter Zeilen zu Funktionen
- Blast Radius / betroffene Flows
- Testlücken
- Review-Prioritäten und Risikosignale

CRG darf niemals allein über Annahme/Ablehnung entscheiden.
Git-Diff, Tests, Typecheck, Linter, Blind Reviewer und Codex bleiben maßgeblich.
"""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Optional


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_command(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )


class ReviewContextBuilder:
    """Baut den Review-Kontext aus Git-Diff, CRG und Graphify auf."""

    def __init__(self, worktree: Path):
        self.worktree = worktree

    def build_review_context(
        self,
        base_sha: str,
        graphify_paths: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        changed_files = self._get_changed_files(base_sha)
        changed_symbols = self._extract_changed_symbols(changed_files)
        diff_content = self._get_diff(base_sha)

        affected_flows = self._detect_affected_flows(changed_symbols)
        test_gaps = self._detect_test_gaps(changed_symbols)
        risk_signals = self._calculate_risk_signals(changed_symbols, affected_flows)
        uncertainties = self._identify_uncertainties(changed_files)
        review_order = self._recommend_review_order(changed_symbols, risk_signals)

        total_risk = sum(r.get("score", 0) for r in risk_signals)

        return {
            "schema_version": 1,
            "base_sha": base_sha,
            "worktree": str(self.worktree),
            "changed_files": changed_files,
            "changed_symbols": changed_symbols,
            "affected_flows": affected_flows,
            "test_gaps": test_gaps,
            "risk_signals": risk_signals,
            "graphify_paths": graphify_paths or [],
            "uncertainties": uncertainties,
            "recommended_review_order": review_order,
            "total_risk_score": round(min(total_risk, 1.0), 2),
            "diff_hash": sha256_text(diff_content),
            "diff_length": len(diff_content),
        }

    def _get_changed_files(self, base_sha: str) -> list[dict[str, Any]]:
        result = run_command(
            ["git", "diff", "--name-status", "--no-renames", base_sha, "--"],
            cwd=self.worktree,
        )

        files = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status, path = parts
                files.append({
                    "path": path,
                    "change_type": self._map_status(status),
                    "lines_added": 0,
                    "lines_removed": 0,
                })

        untracked = run_command(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=self.worktree,
        )
        for line in untracked.stdout.strip().splitlines():
            if line.strip() and not line.strip().startswith(".sin-worker/"):
                files.append({
                    "path": line.strip(),
                    "change_type": "added",
                    "lines_added": 0,
                    "lines_removed": 0,
                })

        return files

    def _map_status(self, status: str) -> str:
        return {
            "A": "added",
            "M": "modified",
            "D": "deleted",
            "R": "renamed",
        }.get(status[0], "modified")

    def _get_diff(self, base_sha: str) -> str:
        result = run_command(
            ["git", "diff", "--no-color", base_sha, "--"],
            cwd=self.worktree,
        )
        return result.stdout

    def _extract_changed_symbols(
        self,
        changed_files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        symbols = []
        for f in changed_files:
            file_path = self.worktree / f["path"]
            if not file_path.is_file():
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, IOError):
                continue

            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("function "):
                    name = stripped.split("(")[0].split()[-1]
                    symbols.append({
                        "name": name,
                        "file": f["path"],
                        "start_line": i,
                        "end_line": i,
                        "type": "function",
                    })
                elif stripped.startswith("class "):
                    rest = stripped[6:]
                    name = rest.split("(")[0].split(":")[0].split()[0].rstrip(":")
                    symbols.append({
                        "name": name,
                        "file": f["path"],
                        "start_line": i,
                        "end_line": i,
                        "type": "class",
                    })

        return symbols

    def _detect_affected_flows(
        self,
        symbols: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        flows = []
        auth_symbols = {"auth", "token", "session", "login", "password", "refresh", "jwt"}
        security_symbols = {"crypto", "hash", "sign", "verify", "encrypt", "decrypt"}

        for sym in symbols:
            name_lower = sym["name"].lower()

            if any(a in name_lower for a in auth_symbols):
                flows.append({
                    "flow": "authentication",
                    "functions": [sym["name"]],
                    "criticality": "high",
                })

            if any(s in name_lower for s in security_symbols):
                flows.append({
                    "flow": "security",
                    "functions": [sym["name"]],
                    "criticality": "high",
                })

        seen = set()
        unique_flows = []
        for f in flows:
            key = f["flow"]
            if key not in seen:
                seen.add(key)
                unique_flows.append(f)
            else:
                for uf in unique_flows:
                    if uf["flow"] == key:
                        uf["functions"].extend(f["functions"])
                        break

        return unique_flows

    def _detect_test_gaps(
        self,
        symbols: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        gaps = []

        test_files = list(self.worktree.rglob("test_*.py")) + list(
            self.worktree.rglob("*.test.ts")
        ) + list(self.worktree.rglob("*.test.js"))

        test_content = ""
        for tf in test_files:
            try:
                test_content += tf.read_text(encoding="utf-8", errors="replace")
            except (OSError, IOError):
                continue

        for sym in symbols:
            has_test = sym["name"] in test_content
            gaps.append({
                "function": sym["name"],
                "has_direct_test": has_test,
                "coverage_type": "direct" if has_test else "unknown",
                "risk": "low" if has_test else "medium",
            })

        return gaps

    def _calculate_risk_signals(
        self,
        symbols: list[dict[str, Any]],
        flows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        signals = []
        security_keywords = {"auth", "token", "session", "password", "secret", "key", "crypto"}

        flow_functions = set()
        for f in flows:
            flow_functions.update(f.get("functions", []))

        for sym in symbols:
            name_lower = sym["name"].lower()

            if any(k in name_lower for k in security_keywords):
                signals.append({
                    "type": "security_keyword",
                    "symbol": sym["name"],
                    "score": 0.20,
                })

            if sym["name"] in flow_functions:
                flow_name = next(
                    f["flow"] for f in flows if sym["name"] in f.get("functions", [])
                )
                signals.append({
                    "type": "flow_participation",
                    "symbol": sym["name"],
                    "flow": flow_name,
                    "score": 0.25,
                })

        return signals

    def _identify_uncertainties(
        self,
        changed_files: list[dict[str, Any]],
    ) -> list[str]:
        uncertainties = []

        for f in changed_files:
            if f["change_type"] == "deleted":
                uncertainties.append(f"Deleted file {f['path']} may have had dependents")

        config_files = [f for f in changed_files if "config" in f["path"].lower()]
        if config_files:
            uncertainties.append("Configuration changes may have runtime effects")

        return uncertainties

    def _recommend_review_order(
        self,
        symbols: list[dict[str, Any]],
        risk_signals: list[dict[str, Any]],
    ) -> list[str]:
        risk_by_symbol: dict[str, float] = {}
        for sig in risk_signals:
            sym = sig["symbol"]
            risk_by_symbol[sym] = risk_by_symbol.get(sym, 0) + sig.get("score", 0)

        sorted_symbols = sorted(
            symbols,
            key=lambda s: risk_by_symbol.get(s["name"], 0),
            reverse=True,
        )

        return [s["name"] for s in sorted_symbols]


def build_blind_review_packet(
    task: dict[str, Any],
    review_context: dict[str, Any],
    diff_content: str,
) -> dict[str, Any]:
    """Erstellt das Paket für den Blinden Reviewer.
    
    Enthält NICHT: Worker-Report, Worker-Begründungen, Worker-Selbstbewertung.
    """
    return {
        "original_task": {
            "objective": task.get("objective", ""),
            "steps": task.get("steps", []),
            "allowed_paths": task.get("allowed_paths", []),
            "forbidden_paths": task.get("forbidden_paths", []),
            "non_goals": task.get("non_goals", []),
            "constraints": task.get("constraints", ""),
        },
        "base_sha": review_context.get("base_sha", ""),
        "changed_files": review_context.get("changed_files", []),
        "changed_symbols": review_context.get("changed_symbols", []),
        "bounded_diff": diff_content[:60000],
        "affected_flows": review_context.get("affected_flows", []),
        "test_gaps": review_context.get("test_gaps", []),
        "risk_signals": review_context.get("risk_signals", []),
        "acceptance_criteria": task.get("acceptance", []),
    }
