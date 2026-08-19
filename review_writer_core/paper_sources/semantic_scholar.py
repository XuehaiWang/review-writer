"""Semantic Scholar paper-source connector."""

from __future__ import annotations

import urllib.parse

from .base import HttpPaperSourceConnector, PaperSearchRequest, SourceSearchResult
from .normalize import normalize_arxiv_id, normalize_doi


class SemanticScholarConnector(HttpPaperSourceConnector):
    name = "semantic_scholar"

    def __init__(self, *, api_key: str = "", **kwargs):
        super().__init__(**kwargs)
        self.api_key = str(api_key or "").strip()

    def _run(self, request: PaperSearchRequest) -> SourceSearchResult:
        params = {
            "query": request.query,
            "limit": str(max(1, min(request.limit, 100))),
            "fields": "paperId,title,abstract,authors,year,venue,externalIds,url,openAccessPdf,citationCount,publicationDate,publicationTypes",
        }
        if request.year_from is not None or request.year_to is not None:
            lower = request.year_from or ""
            upper = request.year_to or ""
            params["year"] = f"{lower}-{upper}" if lower != upper else str(lower)
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        payload = self._request_json(
            "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params),
            headers=headers,
        )
        candidates = []
        for rank, item in enumerate(payload.get("data") or [], start=1):
            if not isinstance(item, dict):
                continue
            external = item.get("externalIds") or {}
            paper_id = str(item.get("paperId") or "")
            open_pdf = item.get("openAccessPdf") or {}
            candidates.append(
                {
                    "identifiers": {
                        "doi": normalize_doi(external.get("DOI")),
                        "arxiv_id": normalize_arxiv_id(external.get("ArXiv")),
                        "openalex_id": "",
                        "semantic_scholar_id": paper_id,
                    },
                    "title": str(item.get("title") or "(untitled)"),
                    "abstract": str(item.get("abstract") or ""),
                    "authors": [str(author.get("name") or "") for author in item.get("authors") or [] if isinstance(author, dict) and author.get("name")],
                    "year": item.get("year"),
                    "publication_date": str(item.get("publicationDate") or ""),
                    "journal": str(item.get("venue") or ""),
                    "document_type": ",".join(item.get("publicationTypes") or []),
                    "citation_count": item.get("citationCount"),
                    "landing_url": str(item.get("url") or ""),
                    "pdf_url": str(open_pdf.get("url") or ""),
                    "open_access": {"is_oa": bool(open_pdf.get("url")), "license": str(open_pdf.get("status") or ""), "source": "semantic_scholar"},
                    "sources": [{"name": self.name, "provider_id": paper_id, "provider_rank": rank, "provider_score": None}],
                    "abstract_decision": {"status": "not_run", "reason": "", "model_tier_snapshot": ""},
                    "selected_for_download": False,
                }
            )
        return SourceSearchResult(source=self.name, status="completed", candidates=candidates)
