"""Shared contracts and HTTP behavior for external paper sources."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class PaperSearchRequest:
    query: str
    topic: str = ""
    limit: int = 8
    year_from: int | None = None
    year_to: int | None = None


@dataclass
class SourceSearchResult:
    source: str
    status: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0


class PaperSourceConnector(Protocol):
    name: str

    def search(self, request: PaperSearchRequest) -> SourceSearchResult: ...


class HttpPaperSourceConnector:
    name = "external"

    def __init__(self, *, timeout_seconds: float = 20.0, max_retries: int = 2):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(0, min(int(max_retries), 4))

    def _request_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "review-writer-discovery/1.0",
            **(headers or {}),
        }
        context = ssl.create_default_context()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(
                    request, context=context, timeout=self.timeout_seconds
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_HTTP_STATUSES or attempt >= self.max_retries:
                    raise
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise
            time.sleep(min(0.5 * (2**attempt), 2.0))
        if last_error is not None:
            raise last_error
        raise RuntimeError("Paper source request failed without an error.")

    def _request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = json.loads(self._request_bytes(url, headers=headers).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{self.name} returned a non-object response.")
        return payload

    def _run(self, request: PaperSearchRequest) -> SourceSearchResult:
        raise NotImplementedError

    def search(self, request: PaperSearchRequest) -> SourceSearchResult:
        started = time.monotonic()
        try:
            result = self._run(request)
        except Exception as exc:
            result = SourceSearchResult(
                source=self.name,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        result.elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        return result
