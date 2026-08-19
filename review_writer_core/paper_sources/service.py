"""Concurrent multi-source paper search orchestration."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .arxiv import ArxivConnector
from .base import PaperSearchRequest, PaperSourceConnector, SourceSearchResult
from .crossref import CrossrefConnector
from .deduplicate import deduplicate_candidates
from .normalize import backward_compatible_fields
from .openalex import OpenAlexConnector
from .rank import rank_candidates
from .semantic_scholar import SemanticScholarConnector


SUPPORTED_SOURCES = ("crossref", "openalex", "semantic_scholar", "arxiv")
SourceStatusCallback = Callable[[str, str, int, str], None]


@dataclass(frozen=True)
class PaperSourceSearchLimits:
    max_subtopics: int = 12
    max_external_requests: int = 48
    max_total_candidates: int = 400
    max_wall_seconds: float = 180.0


DEFAULT_SEARCH_LIMITS = PaperSourceSearchLimits()


def parse_source_names(value: str | Iterable[str] | None) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else list(value or SUPPORTED_SOURCES)
    names: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower().replace("-", "_")
        if name in SUPPORTED_SOURCES and name not in names:
            names.append(name)
    return tuple(names)


def default_connectors(source_names: Iterable[str] | None = None) -> list[PaperSourceConnector]:
    names = parse_source_names(source_names)
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    mapping: dict[str, PaperSourceConnector] = {
        "crossref": CrossrefConnector(mailto=mailto),
        "openalex": OpenAlexConnector(
            api_key=os.environ.get("OPENALEX_API_KEY", ""), mailto=mailto
        ),
        "semantic_scholar": SemanticScholarConnector(
            api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        ),
        "arxiv": ArxivConnector(),
    }
    return [mapping[name] for name in names]


def search_paper_sources(
    request: PaperSearchRequest,
    *,
    connectors: Iterable[PaperSourceConnector] | None = None,
    source_names: Iterable[str] | None = None,
    max_total_candidates: int = 400,
    status_callback: SourceStatusCallback | None = None,
) -> dict[str, Any]:
    active = list(connectors or default_connectors(source_names))
    source_statuses = {
        connector.name: {"status": "queued", "count": 0, "elapsed_ms": 0, "error": ""}
        for connector in active
    }
    if not active:
        return {
            "candidates": [],
            "source_statuses": {},
            "source_errors": {},
            "completion_state": "disabled",
            "degraded": False,
        }

    def notify(source: str, status: str, count: int = 0, error: str = "") -> None:
        if status_callback is not None:
            status_callback(source, status, count, error)

    results: list[SourceSearchResult] = []
    with ThreadPoolExecutor(max_workers=min(4, len(active)), thread_name_prefix="paper-source") as executor:
        futures = {}
        for connector in active:
            source_statuses[connector.name]["status"] = "running"
            notify(connector.name, "running")
            futures[executor.submit(connector.search, request)] = connector.name
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive boundary for custom connectors
                result = SourceSearchResult(
                    source=source,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            source_statuses[source] = {
                "status": result.status,
                "count": len(result.candidates),
                "elapsed_ms": result.elapsed_ms,
                "error": result.error,
            }
            notify(source, result.status, len(result.candidates), result.error)

    successful = [result for result in results if result.status == "completed"]
    failed = [result for result in results if result.status != "completed"]
    raw_candidates = [
        candidate
        for result in successful
        for candidate in result.candidates
    ][: max(1, int(max_total_candidates))]
    candidates = rank_candidates(deduplicate_candidates(raw_candidates), request.topic or request.query)
    candidates = [backward_compatible_fields(candidate) for candidate in candidates]
    completion_state = "failed" if not successful else ("partial" if failed else "complete")
    return {
        "candidates": candidates,
        "source_statuses": source_statuses,
        "source_errors": {
            result.source: result.error for result in failed if result.error
        },
        "completion_state": completion_state,
        "degraded": bool(failed),
    }
