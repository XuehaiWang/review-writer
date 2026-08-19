"""OpenAlex paper-source connector."""

from __future__ import annotations

import urllib.parse
from typing import Any

from .base import HttpPaperSourceConnector, PaperSearchRequest, SourceSearchResult
from .normalize import normalize_doi


def _abstract_from_inverted_index(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in value.items():
        for index in indexes if isinstance(indexes, list) else []:
            if isinstance(index, int):
                positions.append((index, str(word)))
    return " ".join(word for _index, word in sorted(positions))


class OpenAlexConnector(HttpPaperSourceConnector):
    name = "openalex"

    def __init__(self, *, api_key: str = "", mailto: str = "", **kwargs):
        super().__init__(**kwargs)
        self.api_key = str(api_key or "").strip()
        self.mailto = str(mailto or "").strip()

    def _run(self, request: PaperSearchRequest) -> SourceSearchResult:
        params: dict[str, str] = {
            "search": request.query,
            "per-page": str(max(1, min(request.limit, 100))),
        }
        filters = []
        if request.year_from is not None:
            filters.append(f"from_publication_date:{request.year_from}-01-01")
        if request.year_to is not None:
            filters.append(f"to_publication_date:{request.year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto
        payload = self._request_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        candidates = []
        for rank, item in enumerate(payload.get("results") or [], start=1):
            if not isinstance(item, dict):
                continue
            ids = item.get("ids") or {}
            openalex_id = str(item.get("id") or ids.get("openalex") or "").rsplit("/", 1)[-1]
            doi = normalize_doi(item.get("doi") or ids.get("doi"))
            authors = [
                str((authorship.get("author") or {}).get("display_name") or "").strip()
                for authorship in (item.get("authorships") or [])[:20]
                if isinstance(authorship, dict)
            ]
            primary = item.get("primary_location") or {}
            source = primary.get("source") or {}
            oa = item.get("open_access") or {}
            best_oa = item.get("best_oa_location") or {}
            candidates.append(
                {
                    "identifiers": {"doi": doi, "arxiv_id": "", "openalex_id": openalex_id, "semantic_scholar_id": ""},
                    "title": str(item.get("display_name") or item.get("title") or "(untitled)"),
                    "abstract": _abstract_from_inverted_index(item.get("abstract_inverted_index")),
                    "authors": [name for name in authors if name],
                    "year": item.get("publication_year"),
                    "publication_date": str(item.get("publication_date") or ""),
                    "journal": str(source.get("display_name") or ""),
                    "document_type": str(item.get("type") or ""),
                    "citation_count": item.get("cited_by_count"),
                    "landing_url": str(primary.get("landing_page_url") or item.get("id") or ""),
                    "pdf_url": str(best_oa.get("pdf_url") or primary.get("pdf_url") or ""),
                    "open_access": {"is_oa": oa.get("is_oa"), "license": str(best_oa.get("license") or ""), "source": "openalex"},
                    "sources": [{"name": self.name, "provider_id": openalex_id, "provider_rank": rank, "provider_score": item.get("relevance_score")}],
                    "abstract_decision": {"status": "not_run", "reason": "", "model_tier_snapshot": ""},
                    "selected_for_download": False,
                }
            )
        return SourceSearchResult(source=self.name, status="completed", candidates=candidates)
