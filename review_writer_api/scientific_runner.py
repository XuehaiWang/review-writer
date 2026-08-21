"""Secure subprocess boundary for existing scientific scripts."""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse
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
ERROR_ENVELOPE_PREFIX = "REVIEW_WRITER_ERROR:"
TRANSIENT_ERROR_CATEGORIES = frozenset(
    {
        "transient_rate_limited",
        "transient_service_unavailable",
        "transient_timeout",
    }
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


class ScientificInsufficientCredit(ScientificRunError):
    code = "INSUFFICIENT_CREDIT"
    status_code = 402

    def __init__(self, *, attempts: int):
        super().__init__(
            "余额不足，无法执行本次智能任务。请在“API 设置”中查看余额，或联系管理员添加额度。",
            attempts=attempts,
            retryable=False,
        )


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
        allow_private_networks: bool = False,
        trusted_proxy_networks: Sequence[str] = (),
    ):
        self.max_attempts = max(1, min(int(max_attempts), 3))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.poll_interval = max(0.01, float(poll_interval))
        self.max_diagnostic_chars = max(1_024, int(max_diagnostic_chars))
        self.allow_private_networks = bool(allow_private_networks)
        self.trusted_proxy_networks = tuple(str(item) for item in trusted_proxy_networks)

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
        progress_callback: Callable[[], None] | None = None,
        timeout_seconds: float = 900,
    ) -> ScientificRunResult:
        safe_command = self._validate_command(command)
        working = Path(cwd).resolve()
        staging = Path(staging_directory).resolve()
        if not working.is_dir():
            raise WorkflowValidationError("Scientific working directory does not exist.")
        if not staging.is_dir():
            raise WorkflowValidationError("Scientific staging directory does not exist.")
        if not expected_outputs:
            raise WorkflowValidationError(
                "Scientific tasks must declare at least one required staging output."
            )
        outputs = tuple(
            self._safe_output_path(staging, relative) for relative in expected_outputs
        )
        runner_directory = staging / ".runner"
        if runner_directory.is_symlink():
            raise WorkflowValidationError("Scientific runner directory is not trusted.")
        runner_directory.mkdir(parents=True, exist_ok=True)
        if runner_directory.resolve().parent != staging:
            raise WorkflowValidationError("Scientific runner directory escaped staging.")
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
        application_root = str(Path(__file__).resolve().parents[1])
        python_paths = [
            item
            for item in str(child_environment.get("PYTHONPATH") or "").split(os.pathsep)
            if item
        ]
        child_environment["PYTHONPATH"] = os.pathsep.join(
            [application_root, *(item for item in python_paths if item != application_root)]
        )
        child_environment["REVIEW_WRITER_ALLOW_PRIVATE_EGRESS"] = (
            "1" if self.allow_private_networks else "0"
        )
        child_environment["REVIEW_WRITER_TRUSTED_PROXY_NETWORKS"] = ",".join(
            self.trusted_proxy_networks
        )
        gateway_url = str(normal_environment.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "")
        if gateway_url and secrets.get("REVIEW_WRITER_TASK_TOKEN"):
            parsed_gateway = urlparse(gateway_url)
            if parsed_gateway.hostname and parsed_gateway.port:
                child_environment["REVIEW_WRITER_INTERNAL_GATEWAY_ENDPOINT"] = (
                    f"{parsed_gateway.hostname.casefold()}:{parsed_gateway.port}"
                )
        cancellation = cancel_requested or (lambda: False)
        timeout = max(self.poll_interval, float(timeout_seconds))

        last_stdout = ""
        last_stderr = ""
        timed_out = False
        for attempt in range(1, self.max_attempts + 1):
            if cancellation():
                raise ScientificRunCancelled(attempts=attempt)
            self._remove_previous_outputs(outputs)
            completion_marker = runner_directory / f"provider-attempt-{attempt}.completed"
            completion_marker.unlink(missing_ok=True)
            attempt_environment = dict(child_environment)
            attempt_environment["REVIEW_WRITER_PROVIDER_CALL_COMPLETED_FILE"] = str(
                completion_marker
            )
            process_options: dict = {}
            if os.name == "nt":
                process_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            else:
                process_options["start_new_session"] = True
            adapter_command = (
                sys.executable,
                str(Path(__file__).with_name("scientific_entrypoint.py")),
                "--",
                *safe_command,
            )
            try:
                process = subprocess.Popen(
                    list(adapter_command),
                    cwd=working,
                    env=attempt_environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **process_options,
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
                    self._terminate(process)
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
                    if progress_callback is not None:
                        progress_callback()
                    continue

            last_stdout = self._redact(last_stdout, secret_values)
            last_stderr = self._redact(last_stderr, secret_values)
            if not timed_out and process.returncode == 0:
                if progress_callback is not None:
                    progress_callback()
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

            envelope = self._error_envelope(last_stdout, last_stderr)
            category = str(envelope.get("category") or "")
            provider_call_completed = bool(
                completion_marker.is_file()
                or envelope.get("provider_call_completed") is True
            )
            transient = (
                (timed_out or category in TRANSIENT_ERROR_CATEGORIES)
                and not provider_call_completed
            )
            if not transient or attempt >= self.max_attempts:
                http_status = envelope.get("http_status")
                provider_timed_out = bool(
                    category == "transient_timeout"
                    or http_status in {408, 504, 524}
                )
                if category == "insufficient_credit":
                    raise ScientificInsufficientCredit(attempts=attempt)
                if timed_out:
                    reason = (
                        "Scientific task exceeded its execution time limit after "
                        f"{attempt} task attempt{'s' if attempt != 1 else ''}."
                    )
                elif provider_timed_out:
                    suffix = f" (HTTP {http_status})" if http_status else ""
                    reason = (
                        f"Scientific provider timed out{suffix} after "
                        f"{attempt} attempts. Please retry the task."
                    )
                elif category == "transient_service_unavailable":
                    diagnostic = self._diagnostic_summary(last_stderr, last_stdout)
                    if provider_call_completed:
                        reason = (
                            "Scientific provider became unavailable during task "
                            f"attempt {attempt} after the failed model call exhausted "
                            "its internal request retries. Earlier model calls in this "
                            "batch had already completed, so the whole paid batch was "
                            "not replayed automatically."
                        )
                    else:
                        reason = (
                            "Scientific provider remained unavailable after "
                            f"{attempt} task attempt{'s' if attempt != 1 else ''}."
                        )
                    if diagnostic:
                        reason += f" Last provider error: {diagnostic}"
                    reason += " Please retry the task."
                else:
                    diagnostic = self._diagnostic_summary(last_stderr, last_stdout)
                    reason = (
                        f"Scientific task failed: {diagnostic}"
                        if diagnostic
                        else "Scientific task failed."
                    )
                raise ScientificRunFailed(
                    reason,
                    attempts=attempt,
                    retryable=transient,
                    details={
                        "returncode": process.returncode,
                        "stderr": last_stderr,
                        "category": category or ("transient_timeout" if timed_out else "unknown"),
                        "http_status": http_status,
                        "timeout_seconds": timeout if timed_out else None,
                        "provider_call_completed": provider_call_completed,
                    },
                )
            self._wait_before_retry(cancellation, attempt)

        raise ScientificRunFailed(
            "Scientific task failed.",
            attempts=self.max_attempts,
            retryable=timed_out,
        )

    @staticmethod
    def _diagnostic_summary(stderr: str, stdout: str = "") -> str:
        """Return one redacted, bounded, user-actionable failure line."""

        def useful_lines(value: str) -> list[str]:
            return [
                line.strip()
                for line in value.splitlines()
                if line.strip()
                and not line.lstrip().startswith(ERROR_ENVELOPE_PREFIX)
            ]

        # Scientific scripts write failures to stderr and progress to stdout.
        # Prefer stderr so a trailing progress line cannot mask the real error.
        lines = useful_lines(stderr) or useful_lines(stdout)
        if not lines:
            return ""
        # A few scientific scripts persist a diagnostic report after printing
        # the actual ``ERROR: ...`` line.  The trailing "Report saved to" path
        # is useful for developers but is not the failure reason users need.
        # Prefer the last explicit error/exception line whenever one exists.
        explicit_errors = [
            line
            for line in lines
            if re.search(
                r"(?:^|\b)(?:ERROR|RuntimeError|ValueError|PermissionError|"
                r"FileNotFoundError|Exception)\s*:",
                line,
                re.IGNORECASE,
            )
        ]
        diagnostic = (explicit_errors or lines)[-1]
        diagnostic = re.sub(
            r"^(?:RuntimeError|ValueError|PermissionError|FileNotFoundError|Exception):\s*",
            "",
            diagnostic,
        )
        diagnostic = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", diagnostic)
        diagnostic = re.sub(r"\s+", " ", diagnostic).strip()
        return diagnostic[:700]

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
            or posix.parts[0] == ".runner"
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise WorkflowValidationError("Expected output must be a safe relative path.")
        lexical = staging / Path(*posix.parts)
        path = lexical.resolve()
        try:
            path.relative_to(staging)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Expected output escaped the staging directory."
            ) from exc
        if lexical.is_symlink() or path != lexical.absolute():
            raise WorkflowValidationError(
                "Expected output path must not contain symbolic links."
            )
        return path

    @staticmethod
    def _remove_previous_outputs(outputs: Sequence[Path]) -> None:
        for path in outputs:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.exists():
                raise WorkflowValidationError(
                    "A required scientific output path is not a regular file."
                )

    @staticmethod
    def _error_envelope(stdout: str, stderr: str) -> dict:
        for line in reversed(f"{stderr}\n{stdout}".splitlines()):
            stripped = line.strip()
            if not stripped.startswith(ERROR_ENVELOPE_PREFIX):
                continue
            try:
                payload = json.loads(stripped[len(ERROR_ENVELOPE_PREFIX) :])
            except (json.JSONDecodeError, TypeError):
                return {}
            return payload if isinstance(payload, dict) else {}
        return {}

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
            if os.name == "nt":
                if not ScientificRunner._terminate_windows_tree(process.pid):
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
            try:
                return process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                else:
                    process.kill()
        try:
            return process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            return "", "Process output was unavailable after forced termination."

    @staticmethod
    def _terminate_windows_tree(root_pid: int) -> bool:
        if os.name != "nt":
            return False

        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = (
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot == invalid_handle:
            return False
        parent_by_pid: dict[int, int] = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while has_entry:
                parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)

        descendants: set[int] = {int(root_pid)}
        changed = True
        while changed:
            changed = False
            for process_id, parent_id in parent_by_pid.items():
                if parent_id in descendants and process_id not in descendants:
                    descendants.add(process_id)
                    changed = True

        terminated = False
        for process_id in sorted(descendants, key=lambda item: item == root_pid):
            handle = kernel32.OpenProcess(0x0001, False, process_id)
            if not handle:
                continue
            try:
                if kernel32.TerminateProcess(handle, 1):
                    terminated = True
            finally:
                kernel32.CloseHandle(handle)
        return terminated

    def _wait_before_retry(
        self, cancellation: Callable[[], bool], attempt: int
    ) -> None:
        deadline = time.monotonic() + self.retry_delay_seconds * attempt
        while time.monotonic() < deadline:
            if cancellation():
                raise ScientificRunCancelled(attempts=attempt)
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
