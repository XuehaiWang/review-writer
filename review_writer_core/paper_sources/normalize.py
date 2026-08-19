"""Normalization helpers shared by paper-source connectors."""

from __future__ import annotations

import re
from typing import Any


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    return text.removeprefix("doi:").strip().rstrip(".,;)")


def normalize_arxiv_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", text)
    text = text.removesuffix(".pdf")
    return re.sub(r"v\d+$", "", text)


def normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(value or "").casefold()).strip()


def first_author_key(authors: Any) -> str:
    if not isinstance(authors, list) or not authors:
        return ""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(authors[0]).casefold())


def candidate_id(candidate: dict[str, Any]) -> str:
    identifiers = candidate.get("identifiers") or {}
    doi = normalize_doi(identifiers.get("doi") or candidate.get("doi"))
    if doi:
        return f"doi:{doi}"
    arxiv_id = normalize_arxiv_id(identifiers.get("arxiv_id"))
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    sources = candidate.get("sources") or []
    if sources:
        source = sources[0]
        provider_id = str(source.get("provider_id") or "").strip()
        if provider_id:
            return f"{source.get('name') or 'external'}:{provider_id}"
    return "title:" + normalize_title(candidate.get("title"))


def backward_compatible_fields(candidate: dict[str, Any]) -> dict[str, Any]:
    identifiers = candidate.get("identifiers") or {}
    sources = candidate.get("sources") or []
    score = candidate.get("score") or {}
    source_names = [str(item.get("name") or "") for item in sources if item.get("name")]
    candidate["candidate_id"] = candidate_id(candidate)
    candidate["doi"] = normalize_doi(identifiers.get("doi"))
    candidate["url"] = candidate.get("landing_url") or candidate.get("pdf_url") or ""
    candidate["source"] = "+".join(source_names) or "external"
    candidate["score_components"] = score
    candidate["rank_score"] = float(score.get("total") or 0.0)
    candidate["score"] = candidate["rank_score"]
    candidate["keep"] = candidate["rank_score"] > 0
    candidate["selected_for_matrix"] = bool(candidate.get("selected_for_matrix"))
    candidate["reason"] = candidate.get("reason") or "Multi-source deterministic ranking"
    return candidate
