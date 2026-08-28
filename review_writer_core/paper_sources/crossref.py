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
    def _date(item: dict[str, Any], field: str) -> str:
        parts = (item.get(field) or {}).get("date-parts") or []
        values = parts[0] if parts and isinstance(parts[0], list) else []
        if not values or not isinstance(values[0], int):
            return ""
        year = values[0]
        if len(values) >= 3 and all(isinstance(value, int) for value in values[:3]):
            return f"{year:04d}-{values[1]:02d}-{values[2]:02d}"
        if len(values) >= 2 and isinstance(values[1], int):
            return f"{year:04d}-{values[1]:02d}"
        return str(year)

    @classmethod
    def _publication_fields(cls, item: dict[str, Any]) -> dict[str, Any]:
        printed = cls._date(item, "published-print")
        online = cls._date(item, "published-online")
        issued = cls._date(item, "issued")
        bibliographic = printed or issued or online
        first_publication = online or printed or issued
        year_match = re.search(r"(?:18|19|20|21)\d{2}", bibliographic)
        year = int(year_match.group(0)) if year_match else None
        return {
            "year": year,
            "bibliographic_year": year,
            "first_publication_date": first_publication,
            "publication_date": first_publication,
            "publication_status": (
                "issue_assigned"
                if printed
                else "online_first"
                if online
                else "issue_assigned"
                if issued
                else "unknown"
            ),
        }

    @staticmethod
    def _pdf_url(item: dict[str, Any]) -> str:
        for link in item.get("link") or []:
            if isinstance(link, dict) and "pdf" in str(link.get("content-type") or "").casefold():
                return str(link.get("URL") or "")
        return ""

    def _run(self, request: PaperSearchRequest) -> SourceSearchResult:
        requested_doi = normalize_doi(request.query)
        if not re.fullmatch(r"10\.\d{4,9}/\S+", requested_doi):
            requested_doi = ""
        if requested_doi:
            url = "https://api.crossref.org/works/" + urllib.parse.quote(
                requested_doi, safe=""
            )
            if self.mailto:
                url += "?" + urllib.parse.urlencode({"mailto": self.mailto})
            message = self._request_json(url).get("message") or {}
            items = [message] if isinstance(message, dict) and message else []
        else:
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
            publication = self._publication_fields(item)
            candidates.append(
                {
                    "identifiers": {"doi": doi, "arxiv_id": "", "openalex_id": "", "semantic_scholar_id": ""},
                    "title": title or "(untitled)",
                    "abstract": " ".join(abstract.split()),
                    "authors": self._authors(item.get("author")),
                    **publication,
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
