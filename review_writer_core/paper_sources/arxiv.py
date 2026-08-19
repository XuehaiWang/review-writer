"""arXiv paper-source connector."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from .base import HttpPaperSourceConnector, PaperSearchRequest, SourceSearchResult
from .normalize import normalize_arxiv_id, normalize_doi


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivConnector(HttpPaperSourceConnector):
    name = "arxiv"

    def _run(self, request: PaperSearchRequest) -> SourceSearchResult:
        params = {
            "search_query": f"all:{request.query}",
            "start": "0",
            "max_results": str(max(1, min(request.limit, 100))),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        raw = self._request_bytes(
            "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params),
            headers={"Accept": "application/atom+xml"},
        )
        root = ET.fromstring(raw)
        candidates = []
        for rank, entry in enumerate(root.findall(f"{ATOM}entry"), start=1):
            identifier = normalize_arxiv_id(entry.findtext(f"{ATOM}id") or "")
            published = entry.findtext(f"{ATOM}published") or ""
            year = int(published[:4]) if published[:4].isdigit() else None
            if request.year_from is not None and (year is None or year < request.year_from):
                continue
            if request.year_to is not None and (year is None or year > request.year_to):
                continue
            landing_url = ""
            pdf_url = ""
            for link in entry.findall(f"{ATOM}link"):
                href = str(link.attrib.get("href") or "")
                if link.attrib.get("type") == "application/pdf":
                    pdf_url = href
                if link.attrib.get("rel") == "alternate":
                    landing_url = href
            doi = normalize_doi(entry.findtext(f"{ARXIV}doi") or "")
            candidates.append(
                {
                    "identifiers": {"doi": doi, "arxiv_id": identifier, "openalex_id": "", "semantic_scholar_id": ""},
                    "title": " ".join((entry.findtext(f"{ATOM}title") or "(untitled)").split()),
                    "abstract": " ".join((entry.findtext(f"{ATOM}summary") or "").split()),
                    "authors": [str(author.findtext(f"{ATOM}name") or "") for author in entry.findall(f"{ATOM}author")],
                    "year": year,
                    "publication_date": published,
                    "journal": str(entry.findtext(f"{ARXIV}journal_ref") or "arXiv"),
                    "document_type": "preprint",
                    "citation_count": None,
                    "landing_url": landing_url or f"https://arxiv.org/abs/{identifier}",
                    "pdf_url": pdf_url or f"https://arxiv.org/pdf/{identifier}.pdf",
                    "open_access": {"is_oa": True, "license": "arXiv", "source": "arxiv"},
                    "sources": [{"name": self.name, "provider_id": identifier, "provider_rank": rank, "provider_score": None}],
                    "abstract_decision": {"status": "not_run", "reason": "", "model_tier_snapshot": ""},
                    "selected_for_download": False,
                }
            )
        return SourceSearchResult(source=self.name, status="completed", candidates=candidates)
