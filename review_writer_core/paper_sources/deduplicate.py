"""Conservative cross-source paper deduplication."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .normalize import first_author_key, normalize_arxiv_id, normalize_doi, normalize_title


IDENTIFIER_KEYS = ("doi", "arxiv_id", "openalex_id", "semantic_scholar_id")


def _strong_keys(candidate: dict[str, Any]) -> list[str]:
    identifiers = candidate.get("identifiers") or {}
    keys: list[str] = []
    doi = normalize_doi(identifiers.get("doi"))
    arxiv_id = normalize_arxiv_id(identifiers.get("arxiv_id"))
    if doi:
        keys.append(f"doi:{doi}")
    if arxiv_id:
        keys.append(f"arxiv:{arxiv_id}")
    for name in ("openalex_id", "semantic_scholar_id"):
        value = str(identifiers.get(name) or "").strip().lower()
        if value:
            keys.append(f"{name}:{value}")
    return keys


def _same_bibliographic_record(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if normalize_title(left.get("title")) != normalize_title(right.get("title")):
        return False
    left_author = first_author_key(left.get("authors"))
    right_author = first_author_key(right.get("authors"))
    if not left_author or left_author != right_author:
        return False
    left_year = left.get("year")
    right_year = right.get("year")
    return not (
        isinstance(left_year, int)
        and isinstance(right_year, int)
        and abs(left_year - right_year) > 1
    )


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(left)
    identifiers = dict(merged.get("identifiers") or {})
    for key in IDENTIFIER_KEYS:
        if not identifiers.get(key) and (right.get("identifiers") or {}).get(key):
            identifiers[key] = (right.get("identifiers") or {}).get(key)
    merged["identifiers"] = identifiers
    seen_sources = {
        (str(item.get("name") or ""), str(item.get("provider_id") or ""))
        for item in merged.get("sources") or []
    }
    for source in right.get("sources") or []:
        identity = (str(source.get("name") or ""), str(source.get("provider_id") or ""))
        if identity not in seen_sources:
            merged.setdefault("sources", []).append(deepcopy(source))
            seen_sources.add(identity)
    for field in ("title", "authors", "year", "publication_date", "journal", "document_type", "landing_url", "pdf_url"):
        if merged.get(field) in (None, "", []):
            merged[field] = deepcopy(right.get(field))
    if len(str(right.get("abstract") or "")) > len(str(merged.get("abstract") or "")):
        merged["abstract"] = right.get("abstract")
    if (right.get("citation_count") or 0) > (merged.get("citation_count") or 0):
        merged["citation_count"] = right.get("citation_count")
    left_oa = merged.get("open_access") or {}
    right_oa = right.get("open_access") or {}
    if right_oa.get("is_oa") and not left_oa.get("is_oa"):
        merged["open_access"] = deepcopy(right_oa)
    return merged


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    strong_index: dict[str, int] = {}
    for candidate in candidates:
        keys = _strong_keys(candidate)
        target = next((strong_index[key] for key in keys if key in strong_index), None)
        if target is None:
            target = next(
                (index for index, current in enumerate(merged) if _same_bibliographic_record(current, candidate)),
                None,
            )
        if target is None:
            target = len(merged)
            merged.append(deepcopy(candidate))
        else:
            merged[target] = _merge(merged[target], candidate)
        for key in _strong_keys(merged[target]):
            strong_index[key] = target
    return merged
