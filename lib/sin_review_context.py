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

from sin_context.evidence_firewall import render_for_model, wrap_evidence
from sin_context.provider_runtime import ProviderRuntime, load_provider_specs
from sin_orca.verification import bounded_diff, controller_environment

MAX_CHANGED_FILES = 5_000
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_LINES = 20_000
MAX_SYMBOLS = 5_000
MAX_TEST_FILES = 5_000
MAX_TEST_BYTES = 8 * 1024 * 1024
TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".swift",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".sh", ".bash", ".zsh",
}
IGNORED_SCAN_PARTS = {
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "__pycache__", ".mimocode", ".sin-worker",
}

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROVIDER_CONFIG = ROOT / "config" / "provider-runtime.json"
DEFAULT_PROVIDER_HEALTH = (
    Path.home() / ".cache" / "sin" / "provider-health.sqlite3"
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_command(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
        env=controller_environment(),
    )


class ReviewContextBuilder:
    """Baut den Review-Kontext aus Git-Diff, CRG und Graphify auf."""

    def __init__(
        self,
        worktree: Path,
        *,
        provider_config: Path = DEFAULT_PROVIDER_CONFIG,
        provider_health: Path = DEFAULT_PROVIDER_HEALTH,
    ):
        self.worktree = worktree.resolve()
        self.provider_config = provider_config.expanduser().resolve()
        self.provider_health = provider_health.expanduser().resolve()
        self.scan_uncertainties: list[str] = []

    def build_review_context(
        self,
        base_sha: str,
        graphify_paths: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        self.scan_uncertainties = []
        changed_files = self._get_changed_files(base_sha)
        changed_symbols = self._extract_changed_symbols(changed_files)
        diff = self._get_diff(base_sha)
        crg_advisory = self._collect_crg_advisory(base_sha)

        affected_flows = self._detect_affected_flows(changed_symbols)
        test_gaps = self._detect_test_gaps(changed_symbols)
        risk_signals = self._calculate_risk_signals(changed_symbols, affected_flows)
        review_order = self._recommend_review_order(changed_symbols, risk_signals)
        if graphify_paths is None:
            graphify_advisory = self._collect_graphify_top_risk(
                review_order[:3]
            )
        else:
            graphify_advisory = {
                "ok": True,
                "provider": "graphify",
                "status": "caller-supplied",
                "authoritative": False,
                "paths": graphify_paths,
            }

        uncertainties = self._identify_uncertainties(changed_files)
        uncertainties.extend(self.scan_uncertainties)
        if crg_advisory.get("ok") is not True:
            uncertainties.append(
                "code-review-graph advisory sensor unavailable: "
                + str(crg_advisory.get("status", "unknown"))
            )
        elif crg_advisory.get("truncated") is True:
            uncertainties.append(
                "code-review-graph advisory output was truncated"
            )
        if graphify_advisory.get("ok") is not True and review_order:
            uncertainties.append(
                "Graphify top-risk enrichment unavailable: "
                + str(graphify_advisory.get("status", "unknown"))
            )
        elif graphify_advisory.get("truncated") is True:
            uncertainties.append("Graphify advisory output was truncated")

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
            "crg_advisory": crg_advisory,
            "crg_authoritative": False,
            "graphify_advisory": graphify_advisory,
            "graphify_authoritative": False,
            "graphify_paths": graphify_paths or [],
            "uncertainties": list(dict.fromkeys(uncertainties)),
            "recommended_review_order": review_order,
            "total_risk_score": round(min(total_risk, 1.0), 2),
            "diff_hash": diff["full_sha256"],
            "diff_length": diff["full_chars"],
            "diff_truncated": diff["truncated"],
        }

    def _collect_crg_advisory(self, base_sha: str) -> dict[str, Any]:
        try:
            specs = load_provider_specs(self.provider_config)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {
                "ok": False,
                "provider": "code-review-graph",
                "status": "configuration-error",
                "error_type": type(error).__name__,
                "authoritative": False,
            }

        spec = specs.get("code-review-graph")
        if spec is None:
            return {
                "ok": False,
                "provider": "code-review-graph",
                "status": "unconfigured",
                "authoritative": False,
            }

        runtime = ProviderRuntime(state_path=self.provider_health)
        try:
            result = runtime.call(
                spec,
                cwd=self.worktree,
                variables={"base": base_sha},
            )
        except (OSError, ValueError) as error:
            return {
                "ok": False,
                "provider": "code-review-graph",
                "status": "execution-error",
                "error_type": type(error).__name__,
                "authoritative": False,
            }
        if result.get("ok") is not True:
            return {
                key: value
                for key, value in {
                    "ok": False,
                    "provider": "code-review-graph",
                    "status": result.get("status", "failed"),
                    "exit_code": result.get("exit_code"),
                    "duration_ms": result.get("duration_ms"),
                    "authoritative": False,
                }.items()
                if value is not None
            }

        output = str(result.get("output", ""))
        envelope = wrap_evidence(
            source="code-review-graph",
            source_type="review-sensor-output",
            content=output,
            trust_level="external-untrusted",
            metadata={
                "base_sha": base_sha,
                "worktree": str(self.worktree),
            },
        )
        rendered = render_for_model(
            envelope,
            maximum_chars=24_000,
        )
        structured_shape: dict[str, Any] | None = None
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                structured_shape = {
                    "type": "object",
                    "top_level_keys": sorted(
                        str(key)[:100] for key in parsed.keys()
                    )[:100],
                    "field_count": len(parsed),
                }
            elif isinstance(parsed, list):
                structured_shape = {
                    "type": "array",
                    "item_count": len(parsed),
                }
        except json.JSONDecodeError:
            structured_shape = None

        return {
            "ok": True,
            "provider": "code-review-graph",
            "status": "completed",
            "authoritative": False,
            "duration_ms": result.get("duration_ms"),
            "truncated": bool(result.get("truncated")),
            "evidence": rendered,
            "evidence_sha256": envelope.sha256,
            "suspicious_instruction_spans": len(envelope.suspicious),
            "structured_shape": structured_shape,
        }

    def _collect_graphify_top_risk(
        self,
        symbols: list[str],
    ) -> dict[str, Any]:
        if not symbols:
            return {
                "ok": True,
                "provider": "graphify",
                "status": "not-needed",
                "authoritative": False,
                "symbols": [],
            }

        try:
            specs = load_provider_specs(self.provider_config)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {
                "ok": False,
                "provider": "graphify",
                "status": "configuration-error",
                "error_type": type(error).__name__,
                "authoritative": False,
                "symbols": symbols,
            }
        spec = specs.get("graphify")
        if spec is None:
            return {
                "ok": False,
                "provider": "graphify",
                "status": "unconfigured",
                "authoritative": False,
                "symbols": symbols,
            }

        query = (
            "Explain only the dependency and execution paths affected by these "
            "top-risk changed symbols: " + ", ".join(symbols)
        )
        runtime = ProviderRuntime(state_path=self.provider_health)
        try:
            result = runtime.call(
                spec,
                cwd=self.worktree,
                variables={"query": query},
            )
        except (OSError, ValueError) as error:
            return {
                "ok": False,
                "provider": "graphify",
                "status": "execution-error",
                "error_type": type(error).__name__,
                "authoritative": False,
                "symbols": symbols,
            }
        if result.get("ok") is not True:
            return {
                key: value
                for key, value in {
                    "ok": False,
                    "provider": "graphify",
                    "status": result.get("status", "failed"),
                    "exit_code": result.get("exit_code"),
                    "duration_ms": result.get("duration_ms"),
                    "authoritative": False,
                    "symbols": symbols,
                }.items()
                if value is not None
            }

        output = str(result.get("output", ""))
        envelope = wrap_evidence(
            source="graphify:top-risk",
            source_type="review-sensor-output",
            content=output,
            trust_level="external-untrusted",
            metadata={
                "symbols": symbols,
                "worktree": str(self.worktree),
            },
        )
        return {
            "ok": True,
            "provider": "graphify",
            "status": "completed",
            "authoritative": False,
            "symbols": symbols,
            "duration_ms": result.get("duration_ms"),
            "truncated": bool(result.get("truncated")),
            "evidence": render_for_model(envelope, maximum_chars=24_000),
            "evidence_sha256": envelope.sha256,
            "suspicious_instruction_spans": len(envelope.suspicious),
        }

    def _get_changed_files(self, base_sha: str) -> list[dict[str, Any]]:
        result = run_command(
            ["git", "diff", "--name-status", "--no-renames", base_sha, "--"],
            cwd=self.worktree,
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "git diff --name-status failed"
            )

        files = []
        for line in result.stdout.strip().splitlines():
            if len(files) >= MAX_CHANGED_FILES:
                self.scan_uncertainties.append(
                    f"changed-file list truncated at {MAX_CHANGED_FILES} entries"
                )
                break
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
        if untracked.returncode != 0:
            raise RuntimeError(
                untracked.stderr.strip() or "git ls-files failed"
            )
        for line in untracked.stdout.strip().splitlines():
            if len(files) >= MAX_CHANGED_FILES:
                self.scan_uncertainties.append(
                    f"changed-file list truncated at {MAX_CHANGED_FILES} entries"
                )
                break
            if line.strip() and not line.strip().startswith(".sin-worker/"):
                files.append({
                    "path": line.strip(),
                    "change_type": "added",
                    "lines_added": 0,
                    "lines_removed": 0,
                })

        unique: dict[str, dict[str, Any]] = {}
        for item in files:
            path = str(item.get("path", ""))
            if path:
                unique[path] = item
        return [unique[path] for path in sorted(unique)]

    def _map_status(self, status: str) -> str:
        return {
            "A": "added",
            "M": "modified",
            "D": "deleted",
            "R": "renamed",
        }.get(status[0], "modified")

    def _get_diff(self, base_sha: str) -> dict[str, Any]:
        return bounded_diff(
            worktree=self.worktree,
            base_sha=base_sha,
            maximum_chars=60_000,
        )

    def _note_uncertainty(self, message: str) -> None:
        if message not in self.scan_uncertainties:
            self.scan_uncertainties.append(message)

    def _bounded_text_file(
        self,
        path: Path,
        *,
        maximum_bytes: int,
    ) -> str | None:
        try:
            resolved = path.resolve()
            resolved.relative_to(self.worktree)
            metadata = resolved.stat()
        except (OSError, ValueError):
            return None
        if not resolved.is_file() or metadata.st_size > maximum_bytes:
            if metadata.st_size > maximum_bytes:
                self._note_uncertainty(
                    f"skipped oversized review file: {resolved.name}"
                )
            return None
        try:
            raw = resolved.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw:
            return None
        return raw.decode("utf-8", errors="replace")

    def _extract_changed_symbols(
        self,
        changed_files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        for item in changed_files:
            if len(symbols) >= MAX_SYMBOLS:
                self._note_uncertainty(
                    f"changed-symbol scan truncated at {MAX_SYMBOLS} symbols"
                )
                break
            relative = str(item.get("path", ""))
            file_path = self.worktree / relative
            if file_path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            content = self._bounded_text_file(
                file_path,
                maximum_bytes=MAX_SOURCE_FILE_BYTES,
            )
            if content is None:
                continue

            lines = content.splitlines()
            if len(lines) > MAX_SOURCE_LINES:
                self._note_uncertainty(
                    f"source scan truncated at {MAX_SOURCE_LINES} lines: {relative}"
                )
                lines = lines[:MAX_SOURCE_LINES]

            for line_number, line in enumerate(lines, 1):
                if len(symbols) >= MAX_SYMBOLS:
                    self._note_uncertainty(
                        f"changed-symbol scan truncated at {MAX_SYMBOLS} symbols"
                    )
                    break
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("function "):
                    name = stripped.split("(")[0].split()[-1]
                    symbols.append({
                        "name": name,
                        "file": relative,
                        "start_line": line_number,
                        "end_line": line_number,
                        "type": "function",
                    })
                elif stripped.startswith("class "):
                    rest = stripped[6:]
                    name = rest.split("(")[0].split(":")[0].split()[0].rstrip(":")
                    symbols.append({
                        "name": name,
                        "file": relative,
                        "start_line": line_number,
                        "end_line": line_number,
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
        test_content_parts: list[str] = []
        seen: set[Path] = set()
        total_bytes = 0
        scanned_files = 0

        for pattern in ("test_*.py", "*.test.ts", "*.test.js", "*.spec.ts", "*.spec.js"):
            for test_file in self.worktree.rglob(pattern):
                if scanned_files >= MAX_TEST_FILES or total_bytes >= MAX_TEST_BYTES:
                    self._note_uncertainty(
                        "test-gap scan truncated by repository-size limits"
                    )
                    break
                try:
                    relative = test_file.resolve().relative_to(self.worktree)
                except (OSError, ValueError):
                    continue
                if any(part in IGNORED_SCAN_PARTS for part in relative.parts):
                    continue
                if test_file in seen:
                    continue
                seen.add(test_file)
                remaining = MAX_TEST_BYTES - total_bytes
                content = self._bounded_text_file(
                    test_file,
                    maximum_bytes=min(MAX_SOURCE_FILE_BYTES, remaining),
                )
                if content is None:
                    continue
                encoded_size = len(content.encode("utf-8"))
                total_bytes += encoded_size
                scanned_files += 1
                test_content_parts.append(content)
            if scanned_files >= MAX_TEST_FILES or total_bytes >= MAX_TEST_BYTES:
                break

        test_content = "\n".join(test_content_parts)
        gaps: list[dict[str, Any]] = []
        for symbol in symbols:
            name = str(symbol.get("name", ""))
            has_test = bool(name) and name in test_content
            gaps.append({
                "function": name,
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
    diff_envelope = wrap_evidence(
        source=f"git-diff:{review_context.get('base_sha', '')}",
        source_type="repository-diff",
        content=diff_content,
        trust_level="repository-untrusted",
        metadata={
            "diff_hash": review_context.get("diff_hash", ""),
        },
    )
    safe_diff = render_for_model(
        diff_envelope,
        maximum_chars=60000,
    )

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
        "bounded_diff": safe_diff,
        "diff_evidence": {
            "trust_level": diff_envelope.trust_level,
            "sha256": diff_envelope.sha256,
            "suspicious_instruction_spans": len(
                diff_envelope.suspicious
            ),
        },
        "affected_flows": review_context.get("affected_flows", []),
        "test_gaps": review_context.get("test_gaps", []),
        "risk_signals": review_context.get("risk_signals", []),
        "crg_advisory": review_context.get("crg_advisory", {
            "ok": False,
            "provider": "code-review-graph",
            "status": "not-collected",
            "authoritative": False,
        }),
        "crg_authoritative": False,
        "graphify_advisory": review_context.get("graphify_advisory", {
            "ok": False,
            "provider": "graphify",
            "status": "not-collected",
            "authoritative": False,
        }),
        "graphify_authoritative": False,
        "acceptance_criteria": task.get(
            "acceptance_criteria",
            task.get("acceptance", []),
        ),
    }
