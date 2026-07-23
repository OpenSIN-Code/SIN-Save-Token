"""Safe provider execution with persistent health state."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
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


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool


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
            # Older builds persisted raw provider tails in last_error. Clear
            # those legacy values before exposing health snapshots again.
            connection.execute(
                "UPDATE provider_health SET last_error = NULL "
                "WHERE last_error IS NOT NULL"
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

    def health(self, provider: str) -> dict[str, Any]:
        """Return a read-only provider health snapshot for diagnostics."""
        return self._health(provider)

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

        for index, item in enumerate(argv):
            value = item

            for key, replacement in variables.items():
                value = value.replace(
                    "{" + key + "}",
                    replacement,
                )

            if "{" in value or "}" in value:
                raise ValueError(
                    f"unresolved provider argument at index {index}"
                )

            rendered.append(value)

        return rendered

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    @classmethod
    def _run_bounded(
        cls,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        maximum_output_chars: int,
    ) -> BoundedProcessResult:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            cls._terminate_process_group(process)
            raise OSError("provider pipes unavailable")

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        retained_stdout = bytearray()
        stdout_bytes = 0
        stderr_bytes = 0
        maximum_stdout_bytes = max(1, maximum_output_chars * 4)
        deadline = time.monotonic() + timeout_seconds

        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cls._terminate_process_group(process)
                    raise subprocess.TimeoutExpired(argv[0], timeout_seconds)

                events = selector.select(timeout=min(0.2, remaining))
                if not events and process.poll() is not None:
                    events = [
                        (key, selectors.EVENT_READ)
                        for key in list(selector.get_map().values())
                    ]

                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        stdout_bytes += len(chunk)
                        remaining_capacity = (
                            maximum_stdout_bytes - len(retained_stdout)
                        )
                        if remaining_capacity > 0:
                            retained_stdout.extend(chunk[:remaining_capacity])
                    else:
                        stderr_bytes += len(chunk)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cls._terminate_process_group(process)
                raise subprocess.TimeoutExpired(argv[0], timeout_seconds)
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                cls._terminate_process_group(process)
                raise subprocess.TimeoutExpired(argv[0], timeout_seconds) from None
        except BaseException:
            cls._terminate_process_group(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

        decoded = retained_stdout.decode("utf-8", errors="replace")
        bounded_stdout = decoded[:maximum_output_chars]
        return BoundedProcessResult(
            returncode=returncode,
            stdout=bounded_stdout,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=(
                stdout_bytes > len(retained_stdout)
                or len(decoded) > maximum_output_chars
            ),
        )

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
                "provider executable unavailable",
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "unavailable",
                "error": "provider executable unavailable",
                **state,
            }

        started = time.monotonic()

        try:
            process = self._run_bounded(
                argv,
                cwd=cwd,
                timeout_seconds=spec.timeout_seconds,
                maximum_output_chars=spec.maximum_output_chars,
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
            diagnostic = f"provider execution error: {type(error).__name__}"
            state = self._record_failure(
                spec,
                diagnostic,
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "execution-error",
                "error": diagnostic,
                **state,
            }

        duration_ms = int(
            (time.monotonic() - started) * 1000
        )

        stdout = process.stdout.strip()

        if process.returncode != 0:
            diagnostic = f"provider exited with code {process.returncode}"
            state = self._record_failure(
                spec,
                diagnostic,
            )

            return {
                "ok": False,
                "provider": spec.name,
                "status": "failed",
                "exit_code": process.returncode,
                "duration_ms": duration_ms,
                "stdout_bytes": process.stdout_bytes,
                "stderr_bytes": process.stderr_bytes,
                **state,
            }

        self._record_success(spec.name)

        return {
            "ok": True,
            "provider": spec.name,
            "status": "completed",
            "exit_code": 0,
            "duration_ms": duration_ms,
            "output": stdout,
            "output_chars": len(stdout),
            "stdout_bytes": process.stdout_bytes,
            "stderr_bytes": process.stderr_bytes,
            "truncated": process.stdout_truncated,
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

        argv = config.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise ValueError(
                f"provider {name!r} requires a non-empty argv string list"
            )

        result[name] = ProviderSpec(
            name=name,
            argv=list(argv),
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
