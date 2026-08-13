"""Secure subprocess boundary for existing scientific scripts."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from review_writer_api.errors import WorkflowError, WorkflowValidationError


SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONPATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_ENVIRONMENT_KEY = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE
)
TRANSIENT_PATTERN = re.compile(
    r"(?:\b429\b|\b503\b|rate[_ -]?limit|temporar(?:y|ily)|timed?\s*out|timeout)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScientificRunResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    attempts: int
    outputs: tuple[str, ...]


class ScientificRunError(WorkflowError):
    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        retryable: bool,
        details: dict | None = None,
    ):
        super().__init__(message, details=details)
        self.attempts = attempts
        self.retryable = retryable


class ScientificRunFailed(ScientificRunError):
    code = "SCIENTIFIC_RUN_FAILED"


class ScientificRunCancelled(ScientificRunError):
    code = "SCIENTIFIC_RUN_CANCELLED"
    status_code = 409

    def __init__(self, message: str = "Scientific task was cancelled.", *, attempts: int):
        super().__init__(message, attempts=attempts, retryable=False)


class ScientificOutputMissing(ScientificRunError):
    code = "SCIENTIFIC_OUTPUT_MISSING"
    status_code = 422

    def __init__(self, message: str, *, attempts: int, missing: Sequence[str]):
        super().__init__(
            message,
            attempts=attempts,
            retryable=False,
            details={"missing_outputs": list(missing)},
        )


class ScientificRunner:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.5,
        poll_interval: float = 0.1,
        max_diagnostic_chars: int = 32_000,
    ):
        self.max_attempts = max(1, min(int(max_attempts), 3))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.poll_interval = max(0.01, float(poll_interval))
        self.max_diagnostic_chars = max(1_024, int(max_diagnostic_chars))

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        staging_directory: Path,
        expected_outputs: Sequence[str],
        env: Mapping[str, str] | None = None,
        secret_env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        timeout_seconds: float = 900,
    ) -> ScientificRunResult:
        safe_command = self._validate_command(command)
        working = Path(cwd).resolve()
        staging = Path(staging_directory).resolve()
        if not working.is_dir():
            raise WorkflowValidationError("Scientific working directory does not exist.")
        if not staging.is_dir():
            raise WorkflowValidationError("Scientific staging directory does not exist.")
        outputs = tuple(
            self._safe_output_path(staging, relative) for relative in expected_outputs
        )
        normal_environment = self._validate_environment(env or {})
        secrets = self._validate_environment(secret_env or {})
        sensitive_normal_keys = sorted(
            key for key in normal_environment if SENSITIVE_ENVIRONMENT_KEY.search(key)
        )
        if sensitive_normal_keys:
            raise WorkflowValidationError(
                "Sensitive child environment values must be supplied through secret_env.",
                details={"keys": sensitive_normal_keys},
            )
        secret_values = tuple(
            value for value in secrets.values() if str(value or "").strip()
        )
        if any(secret in argument for secret in secret_values for argument in safe_command):
            raise WorkflowValidationError(
                "Task secrets must be passed through the child environment, not command arguments."
            )
        child_environment = {
            key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_KEYS
        }
        child_environment.update(normal_environment)
        child_environment.update(secrets)
        child_environment.setdefault("PYTHONIOENCODING", "utf-8")
        cancellation = cancel_requested or (lambda: False)
        timeout = max(self.poll_interval, float(timeout_seconds))

        last_stdout = ""
        last_stderr = ""
        timed_out = False
        for attempt in range(1, self.max_attempts + 1):
            if cancellation():
                raise ScientificRunCancelled(attempts=attempt)
            try:
                process = subprocess.Popen(
                    list(safe_command),
                    cwd=working,
                    env=child_environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise WorkflowValidationError(
                    "Scientific command could not be started.",
                    details={"reason": exc.__class__.__name__},
                ) from exc

            started = time.monotonic()
            timed_out = False
            while True:
                if cancellation():
                    stdout, stderr = self._terminate(process)
                    raise ScientificRunCancelled(attempts=attempt) from None
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    last_stdout, last_stderr = self._terminate(process)
                    timed_out = True
                    break
                try:
                    last_stdout, last_stderr = process.communicate(
                        timeout=min(self.poll_interval, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue

            last_stdout = self._redact(last_stdout, secret_values)
            last_stderr = self._redact(last_stderr, secret_values)
            if not timed_out and process.returncode == 0:
                missing = [
                    path.relative_to(staging).as_posix()
                    for path in outputs
                    if not path.is_file()
                ]
                if missing:
                    raise ScientificOutputMissing(
                        "Scientific task completed without all required staging outputs.",
                        attempts=attempt,
                        missing=missing,
                    )
                return ScientificRunResult(
                    command=tuple(self._redact(argument, secret_values) for argument in safe_command),
                    returncode=0,
                    stdout=last_stdout,
                    stderr=last_stderr,
                    attempts=attempt,
                    outputs=tuple(path.relative_to(staging).as_posix() for path in outputs),
                )

            transient = timed_out or bool(
                TRANSIENT_PATTERN.search(f"{last_stdout}\n{last_stderr}")
            )
            if not transient or attempt >= self.max_attempts:
                reason = "Scientific task timed out." if timed_out else "Scientific task failed."
                raise ScientificRunFailed(
                    reason,
                    attempts=attempt,
                    retryable=transient,
                    details={
                        "returncode": process.returncode,
                        "stderr": last_stderr,
                    },
                )
            self._wait_before_retry(cancellation, attempt)

        raise ScientificRunFailed(
            "Scientific task failed.",
            attempts=self.max_attempts,
            retryable=timed_out,
        )

    @staticmethod
    def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, (str, bytes)) or not command:
            raise WorkflowValidationError("Scientific command must be a non-empty argument list.")
        normalized = tuple(str(argument) for argument in command)
        if not normalized[0].strip() or "\x00" in "".join(normalized):
            raise WorkflowValidationError("Scientific command contains an invalid argument.")
        return normalized

    @staticmethod
    def _validate_environment(values: Mapping[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for key, value in values.items():
            normalized_key = str(key)
            if not ENVIRONMENT_KEY.fullmatch(normalized_key):
                raise WorkflowValidationError("Child environment contains an invalid key.")
            validated[normalized_key] = str(value)
        return validated

    @staticmethod
    def _safe_output_path(staging: Path, relative: str) -> Path:
        raw = str(relative or "").strip()
        posix = PurePosixPath(raw.replace("\\", "/"))
        windows = PureWindowsPath(raw)
        if (
            not raw
            or Path(raw).is_absolute()
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise WorkflowValidationError("Expected output must be a safe relative path.")
        path = (staging / Path(*posix.parts)).resolve()
        try:
            path.relative_to(staging)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Expected output escaped the staging directory."
            ) from exc
        return path

    def _redact(self, value: str | None, secrets: Sequence[str]) -> str:
        redacted = str(value or "")
        for secret in sorted(set(secrets), key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
        if len(redacted) > self.max_diagnostic_chars:
            redacted = redacted[-self.max_diagnostic_chars :]
        return redacted

    @staticmethod
    def _terminate(process: subprocess.Popen) -> tuple[str, str]:
        if process.poll() is None:
            process.terminate()
            try:
                return process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        return process.communicate()

    def _wait_before_retry(
        self, cancellation: Callable[[], bool], attempt: int
    ) -> None:
        deadline = time.monotonic() + self.retry_delay_seconds * attempt
        while time.monotonic() < deadline:
            if cancellation():
                raise ScientificRunCancelled(attempts=attempt)
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
