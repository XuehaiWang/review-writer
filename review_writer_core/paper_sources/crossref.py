"""Crossref paper-source connector."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .base import HttpPaperSourceConnector, PaperSearchRequest, SourceSearchResult
from .normalize import normalize_doi


class CrossrefConnector(HttpPaperSourceConnector):
    name = "crossref"

    def __init__(self, *, mailto: str = "", **kwargs):
        super().__init__(**kwargs)
        self.mailto = str(mailto or "").strip()

    @staticmethod
    def _authors(values: Any) -> list[str]:
        return [
            name
            for item in (values or [])[:20]
            if isinstance(item, dict)
            if (name := " ".join(str(item.get(key) or "").strip() for key in ("given", "family")).strip())
        ]

    @staticmethod
    def _year(item: dict[str, Any]) -> int | None:
        for field in ("published-print", "published-online", "issued", "created"):
            parts = (item.get(field) or {}).get("date-parts") or []
            if parts and parts[0] and isinstance(parts[0][0], int):
                return parts[0][0]
        return None

    @staticmethod
    def _pdf_url(item: dict[str, Any]) -> str:
        for link in item.get("link") or []:
            if isinstance(link, dict) and "pdf" in str(link.get("content-type") or "").casefold():
                return str(link.get("URL") or "")
        return ""

    def _run(self, request: PaperSearchRequest) -> SourceSearchResult:
        params: dict[str, str] = {
            "query.bibliographic": request.query,
            "rows": str(max(1, min(request.limit, 100))),
            "select": "DOI,title,author,issued,published-print,published-online,created,container-title,abstract,URL,link,license,type,is-referenced-by-count,score",
        }
        filters = []
        if request.year_from is not None:
            filters.append(f"from-pub-date:{request.year_from}-01-01")
        if request.year_to is not None:
            filters.append(f"until-pub-date:{request.year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        if self.mailto:
            params["mailto"] = self.mailto
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        payload = self._request_json(url)
        items = (payload.get("message") or {}).get("items") or []
        candidates = []
        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            doi = normalize_doi(item.get("DOI"))
            title = " ".join(item.get("title") or []).strip()
            abstract = re.sub(r"<[^>]+>", " ", str(item.get("abstract") or ""))
            licenses = item.get("license") or []
            pdf_url = self._pdf_url(item)
            candidates.append(
                {
                    "identifiers": {"doi": doi, "arxiv_id": "", "openalex_id": "", "semantic_scholar_id": ""},
                    "title": title or "(untitled)",
                    "abstract": " ".join(abstract.split()),
                    "authors": self._authors(item.get("author")),
                    "year": self._year(item),
                    "publication_date": "",
                    "journal": " ".join(item.get("container-title") or []),
                    "document_type": str(item.get("type") or ""),
                    "citation_count": item.get("is-referenced-by-count"),
                    "landing_url": str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
                    "pdf_url": pdf_url,
                    "open_access": {
                        "is_oa": bool(licenses),
                        "license": str((licenses[0] if licenses else {}).get("URL") or ""),
                        "source": "crossref",
                    },
                    "sources": [{"name": self.name, "provider_id": doi or str(item.get("URL") or rank), "provider_rank": rank, "provider_score": item.get("score")}],
                    "abstract_decision": {"status": "not_run", "reason": "", "model_tier_snapshot": ""},
                    "selected_for_download": False,
                }
            )
        return SourceSearchResult(source=self.name, status="completed", candidates=candidates)
