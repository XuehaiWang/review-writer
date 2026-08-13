"""Low-level in-process executor for the dashboard compatibility routes.

The dashboard still uses ``BaseHTTPRequestHandler`` internally.  Keeping that
transport detail behind this small boundary lets the FastAPI application and
its user/project services stay independent while endpoints are migrated.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Mapping

from review_writer_core.dashboard_assets import dashboard_assets

from view.serve_review_dashboard import (
    DashboardHandler,
    configured_external_file_access,
)


@dataclass(frozen=True)
class DashboardResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class _MemorySocket:
    def __init__(self, request_bytes: bytes):
        self.request_stream = io.BytesIO(request_bytes)
        self.response_stream = io.BytesIO()

    def makefile(self, mode: str, buffering: int = -1):
        if "r" in mode:
            return self.request_stream
        return self.response_stream

    def sendall(self, payload: bytes) -> None:
        self.response_stream.write(payload)

    def close(self) -> None:
        return None


class DashboardRequestExecutor:
    """Execute one dashboard request against a caller-supplied review root."""

    def __init__(self):
        view_root = Path(__file__).resolve().parents[1] / "view"
        self.asset_paths = dashboard_assets(view_root)

    def dispatch(
        self,
        review_root: Path,
        *,
        method: str,
        path_and_query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> DashboardResponse:
        root = Path(review_root).resolve()
        external_files, external_directories = configured_external_file_access(root)
        attributes = {
            "review_root": root,
            "library_app_path": self.asset_paths[0],
            "discovery_app_path": self.asset_paths[1],
            "matrix_app_path": self.asset_paths[2],
            "blueprint_app_path": self.asset_paths[3],
            "sections_app_path": self.asset_paths[4],
            "figures_app_path": self.asset_paths[5],
            "figure_review_app_path": self.asset_paths[6],
            "draft_app_path": self.asset_paths[7],
            "final_app_path": self.asset_paths[8],
            "settings_app_path": self.asset_paths[9],
            "external_file_allowlist": external_files,
            "external_directory_allowlist": external_directories,
            "access_token": "",
            "log_message": lambda self, fmt, *args: None,
        }
        handler_type = type("ScopedDashboardHandler", (DashboardHandler,), attributes)
        request_bytes = self._request_bytes(method, path_and_query, headers, body)
        memory_socket = _MemorySocket(request_bytes)
        handler_type(memory_socket, ("127.0.0.1", 0), object())
        return self._parse_response(memory_socket.response_stream.getvalue())

    @staticmethod
    def _request_bytes(
        method: str,
        path_and_query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> bytes:
        safe_headers = {
            str(name): str(value)
            for name, value in headers.items()
            if str(name).casefold() not in {"connection", "content-length", "transfer-encoding"}
        }
        safe_headers["Host"] = "review-writer.internal"
        safe_headers["Content-Length"] = str(len(body))
        lines = [f"{method.upper()} {path_and_query} HTTP/1.1"]
        lines.extend(f"{name}: {value}" for name, value in safe_headers.items())
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

    @staticmethod
    def _parse_response(payload: bytes) -> DashboardResponse:
        head, separator, body = payload.partition(b"\r\n\r\n")
        if not separator:
            raise RuntimeError("Dashboard returned an invalid HTTP response.")
        status_line, _, raw_headers = head.partition(b"\r\n")
        status_parts = status_line.decode("latin-1").split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise RuntimeError("Dashboard returned an invalid status line.")
        parsed_headers = BytesParser(policy=HTTP).parsebytes(raw_headers + b"\r\n\r\n")
        excluded = {"connection", "content-length", "server", "date"}
        response_headers = {
            str(name): str(value)
            for name, value in parsed_headers.items()
            if str(name).casefold() not in excluded
        }
        return DashboardResponse(int(status_parts[1]), response_headers, body)
