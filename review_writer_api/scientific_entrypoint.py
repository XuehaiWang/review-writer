"""Production adapter for scientific commands executed by ScientificRunner.

The adapter instruments the stdlib HTTP transport used by the repository's
scientific scripts. It emits a machine-readable retry envelope for transient
provider failures and records successful non-idempotent provider calls before
later parsing/publication work can fail.
"""

from __future__ import annotations

import json
import os
import runpy
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ERROR_ENVELOPE_PREFIX = "REVIEW_WRITER_ERROR:"
COMPLETION_FILE_ENV = "REVIEW_WRITER_PROVIDER_CALL_COMPLETED_FILE"


def emit_error(
    category: str,
    *,
    provider_call_completed: bool = False,
    http_status: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "category": category,
        "provider_call_completed": bool(provider_call_completed),
    }
    if http_status is not None:
        payload["http_status"] = int(http_status)
    sys.stderr.write(ERROR_ENVELOPE_PREFIX + json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def mark_provider_call_completed() -> None:
    marker_value = os.environ.get(COMPLETION_FILE_ENV, "").strip()
    if not marker_value:
        return
    marker = Path(marker_value)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("completed\n", encoding="utf-8")


def _error_category(status: int) -> str:
    if status == 429:
        return "transient_rate_limited"
    if status in {502, 503, 504}:
        return "transient_service_unavailable"
    if status in {401, 403}:
        return "permission"
    if 400 <= status < 500:
        return "validation"
    return "provider_http_error"


def _request_method(target: Any, data: Any) -> str:
    if isinstance(target, urllib.request.Request):
        return target.get_method().upper()
    return "POST" if data is not None else "GET"


def install_urllib_protocol() -> None:
    original = urllib.request.urlopen

    def instrumented(target, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):
        method = _request_method(target, data)
        try:
            response = original(target, data=data, timeout=timeout, *args, **kwargs)
        except urllib.error.HTTPError as exc:
            emit_error(_error_category(int(exc.code)), http_status=int(exc.code))
            raise
        except (TimeoutError, socket.timeout):
            emit_error("transient_timeout")
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                emit_error("transient_timeout")
            raise
        if method == "POST":
            mark_provider_call_completed()
        return response

    urllib.request.urlopen = instrumented


def _is_python(command: list[str]) -> bool:
    executable = Path(command[0]).name.casefold()
    return executable.startswith("python") or Path(command[0]).resolve() == Path(sys.executable).resolve()


def run_python(command: list[str]) -> int:
    arguments = command[1:]
    if not arguments:
        return 0
    install_urllib_protocol()
    if arguments[0] == "-c" and len(arguments) >= 2:
        sys.argv = ["-c", *arguments[2:]]
        namespace = {"__name__": "__main__", "__file__": "<string>"}
        exec(compile(arguments[1], "<string>", "exec"), namespace, namespace)
        return 0
    if arguments[0] == "-m" and len(arguments) >= 2:
        sys.argv = [arguments[1], *arguments[2:]]
        runpy.run_module(arguments[1], run_name="__main__", alter_sys=True)
        return 0
    script = Path(arguments[0]).resolve()
    if not script.is_file():
        raise SystemExit(f"Scientific Python script not found: {script}")
    sys.argv = [str(script), *arguments[1:]]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        raise SystemExit("A scientific command is required after --.")
    if _is_python(arguments):
        return run_python(arguments)
    completed = subprocess.run(arguments, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
