"""Safe provider execution with persistent health state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    argv: list[str]
    timeout_seconds: int = 120
    maximum_output_chars: int = 16000
    failure_threshold: int = 3
    cooldown_seconds: int = 300


class ProviderRuntime:
    def __init__(
        self,
        *,
        state_path: Path | None = None,
    ) -> None:
        self.state_path = (
            state_path
            or (
                Path.home()
                / ".cache"
                / "sin"
                / "provider-health.sqlite3"
            )
        ).resolve()

        self.state_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.state_path,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            "PRAGMA journal_mode=WAL"
        )
        connection.execute(
            "PRAGMA busy_timeout=10000"
        )
        return connection

    def _initialize(self) -> None:
        connection = self._connect()

        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_health (
                    provider TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    opened_until INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_success_at INTEGER,
                    last_failure_at INTEGER
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _health(
        self,
        provider: str,
    ) -> dict[str, Any]:
        connection = self._connect()

        try:
            row = connection.execute(
                """
                SELECT *
                FROM provider_health
                WHERE provider = ?
                """,
                (provider,),
            ).fetchone()

            if row is None:
                return {
                    "provider": provider,
                    "consecutive_failures": 0,
                    "opened_until": 0,
                }

            return dict(row)
        finally:
            connection.close()

    def _record_success(
        self,
        provider: str,
    ) -> None:
        connection = self._connect()

        try:
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider,
                    consecutive_failures,
                    opened_until,
                    last_error,
                    last_success_at,
                    last_failure_at
                )
                VALUES (?, 0, 0, NULL, ?, NULL)
                ON CONFLICT(provider)
                DO UPDATE SET
                    consecutive_failures = 0,
                    opened_until = 0,
                    last_error = NULL,
                    last_success_at = excluded.last_success_at
                """,
                (provider, int(time.time())),
            )
            connection.commit()
        finally:
            connection.close()

    def _record_failure(
        self,
        spec: ProviderSpec,
        error: str,
    ) -> dict[str, Any]:
        current = self._health(spec.name)
        failures = (
            int(current.get("consecutive_failures", 0))
            + 1
        )

        opened_until = 0

        if failures >= spec.failure_threshold:
            opened_until = (
                int(time.time())
                + spec.cooldown_seconds
            )

        connection = self._connect()

        try:
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider,
                    consecutive_failures,
                    opened_until,
                    last_error,
                    last_success_at,
                    last_failure_at
                )
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(provider)
                DO UPDATE SET
                    consecutive_failures =
                        excluded.consecutive_failures,
                    opened_until =
                        excluded.opened_until,
                    last_error =
                        excluded.last_error,
                    last_failure_at =
                        excluded.last_failure_at
                """,
                (
                    spec.name,
                    failures,
                    opened_until,
                    error[:2000],
                    int(time.time()),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        return {
            "consecutive_failures": failures,
            "opened_until": opened_until,
        }

    @staticmethod
    def _render_argv(
        argv: list[str],
        variables: dict[str, str],
    ) -> list[str]:
        rendered: list[str] = []

        for item in argv:
            value = item

            for key, replacement in variables.items():
                value = value.replace(
                    "{" + key + "}",
                    replacement,
                )

            if "{" in value or "}" in value:
                raise ValueError(
                    f"unresolved provider argument: {value}"
                )

            rendered.append(value)

        return rendered

    def call(
        self,
        spec: ProviderSpec,
        *,
        cwd: Path,
        variables: dict[str, str],
    ) -> dict[str, Any]:
        health = self._health(spec.name)
        now = int(time.time())
        opened_until = int(
            health.get("opened_until", 0)
        )

        if opened_until > now:
            return {
                "ok": False,
                "provider": spec.name,
                "status": "circuit-open",
                "retry_after_seconds": opened_until - now,
                "error": health.get("last_error"),
            }

        argv = self._render_argv(
            spec.argv,
            variables,
        )

        executable = argv[0]

        if os.path.isabs(executable):
            available = (
                os.path.isfile(executable)
                and os.access(executable, os.X_OK)
            )
        else:
            available = shutil.which(executable) is not None

        if not available:
            state = self._record_failure(
                spec,
                f"executable unavailable: {executable}",
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "unavailable",
                "error": f"executable unavailable: {executable}",
                **state,
            }

        started = time.monotonic()

        try:
            process = subprocess.run(
                argv,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=spec.timeout_seconds,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            state = self._record_failure(
                spec,
                "provider timed out",
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "timeout",
                "error": "provider timed out",
                **state,
            }
        except OSError as error:
            state = self._record_failure(
                spec,
                str(error),
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "execution-error",
                "error": str(error),
                **state,
            }

        duration_ms = int(
            (time.monotonic() - started) * 1000
        )

        full_output = (
            process.stdout
            + "\n"
            + process.stderr
        ).strip()

        output_sha256 = hashlib.sha256(
            full_output.encode("utf-8")
        ).hexdigest()

        if process.returncode != 0:
            state = self._record_failure(
                spec,
                full_output[-2000:]
                or f"exit code {process.returncode}",
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "failed",
                "argv": argv,
                "exit_code": process.returncode,
                "duration_ms": duration_ms,
                "output_tail": full_output[
                    -spec.maximum_output_chars:
                ],
                "output_sha256": output_sha256,
                **state,
            }

        self._record_success(spec.name)

        truncated = (
            len(full_output)
            > spec.maximum_output_chars
        )

        return {
            "ok": True,
            "provider": spec.name,
            "status": "completed",
            "argv": argv,
            "exit_code": 0,
            "duration_ms": duration_ms,
            "output": full_output[
                :spec.maximum_output_chars
            ],
            "output_sha256": output_sha256,
            "output_chars": len(full_output),
            "truncated": truncated,
        }

    def call_first_available(
        self,
        specs: list[ProviderSpec],
        *,
        cwd: Path,
        variables: dict[str, str],
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []

        for spec in specs:
            result = self.call(
                spec,
                cwd=cwd,
                variables=variables,
            )

            attempts.append(
                {
                    key: value
                    for key, value in result.items()
                    if key != "output"
                }
            )

            if result.get("ok") is True:
                return {
                    **result,
                    "attempts": attempts,
                }

        return {
            "ok": False,
            "status": "all-providers-failed",
            "attempts": attempts,
        }


def load_provider_specs(
    path: Path,
) -> dict[str, ProviderSpec]:
    raw = json.loads(
        path.read_text(encoding="utf-8")
    )

    providers = raw.get("providers")

    if not isinstance(providers, dict):
        raise ValueError(
            "provider configuration requires providers object"
        )

    result: dict[str, ProviderSpec] = {}

    for name, config in providers.items():
        if not isinstance(config, dict):
            continue

        result[name] = ProviderSpec(
            name=name,
            argv=list(config["argv"]),
            timeout_seconds=int(
                config.get("timeout_seconds", 120)
            ),
            maximum_output_chars=int(
                config.get(
                    "maximum_output_chars",
                    16000,
                )
            ),
            failure_threshold=int(
                config.get("failure_threshold", 3)
            ),
            cooldown_seconds=int(
                config.get("cooldown_seconds", 300)
            ),
        )

    return result
