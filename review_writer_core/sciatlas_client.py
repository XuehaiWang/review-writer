"""Small dependency-free client for the hosted SciAtlas search API.

Environment variables:
  SCIATLAS_API_BASE_URL   (required; use HTTPS or a loopback HTTP proxy)
  SCIATLAS_API_KEY        (required for /v1/search)
  SCIATLAS_TIMEOUT        (seconds, default 240)
  SCIATLAS_ALLOW_INSECURE_HTTP
                          (explicit legacy opt-in for non-loopback HTTP)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = ""
DEFAULT_TIMEOUT = 240
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SciAtlasConfig:
    base_url: str
    api_key: str
    timeout: int

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url)


def load_config(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
    allow_insecure_http: bool | None = None,
) -> SciAtlasConfig:
    raw_base_url = (
        base_url if base_url is not None else os.environ.get("SCIATLAS_API_BASE_URL", DEFAULT_BASE_URL)
    )
    raw_base_url = str(raw_base_url or "").strip().rstrip("/")
    if allow_insecure_http is None:
        allow_insecure_http = (
            os.environ.get("SCIATLAS_ALLOW_INSECURE_HTTP", "").strip().casefold()
            in _TRUE_VALUES
        )
    if raw_base_url:
        parsed = urlsplit(raw_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "SCIATLAS_API_BASE_URL must be an HTTP(S) origin without credentials, "
                "query parameters, or fragments."
            )
        if (
            parsed.scheme == "http"
            and parsed.hostname.casefold() not in _LOOPBACK_HOSTS
            and not allow_insecure_http
        ):
            raise ValueError(
                "Remote SciAtlas HTTP would expose the API key in cleartext. Use an HTTPS "
                "endpoint or explicitly set SCIATLAS_ALLOW_INSECURE_HTTP=true for a legacy service."
            )
    return SciAtlasConfig(
        base_url=raw_base_url,
        api_key=(api_key if api_key is not None else os.environ.get("SCIATLAS_API_KEY", "")).strip(),
        timeout=int(timeout or os.environ.get("SCIATLAS_TIMEOUT") or DEFAULT_TIMEOUT),
    )


class SciAtlasClient:
    def __init__(self, config: SciAtlasConfig | None = None) -> None:
        self.config = config or load_config()

    def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        url = self.config.base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if endpoint.rstrip("/") != "/healthz":
            if not self.config.api_key:
                raise RuntimeError("SCIATLAS_API_KEY is required for this endpoint.")
            headers["Authorization"] = f"Bearer {self.config.api_key}"
            headers["X-API-Key"] = self.config.api_key
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError:
                detail = raw
            raise RuntimeError(f"SciAtlas API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"SciAtlas connection error: {exc.reason}") from exc

    def health(self) -> Any:
        return self._request("GET", "/healthz")

    def search_papers(
        self,
        *,
        query: str,
        keyword: str,
        keyword_score: int = 10,
        top_k: int = 8,
        retrieval_mode: str = "hybrid",
        time_range: str | None = None,
        domain: str | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> Any:
        """Call ``/v1/search`` for one keyword anchor and return the raw response."""
        plan: dict[str, Any] = {
            "query_text": query,
            "source_type": "idea_text",
            "source_title": None,
            "keywords": [{"text": keyword, "score": keyword_score}],
            "titles": [],
            "reference_titles": [],
        }
        if domain:
            plan["domain"] = domain
        if time_range:
            plan["time_range"] = time_range
        options: dict[str, Any] = {"top_k": top_k, "retrieval_mode": retrieval_mode}
        if extra_options:
            options.update(extra_options)
        return self._request("POST", "/v1/search", {"plan": plan, "options": options})


def _walk_paper_lists(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [paper for paper in node if isinstance(paper, dict)]
    if not isinstance(node, dict):
        return []
    for key in ("papers", "results", "items"):
        papers = _walk_paper_lists(node.get(key))
        if papers:
            return papers
    for key in ("result", "data", "ranking", "sources", "kg", "vector", "web"):
        papers = _walk_paper_lists(node.get(key))
        if papers:
            return papers
    return []


def papers_from_response(response: Any) -> list[dict[str, Any]]:
    """Extract the first known paper-record list from a SciAtlas response."""
    return _walk_paper_lists(response)
