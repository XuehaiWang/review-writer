"""Deterministic and explainable candidate ranking."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any


def _terms(text: str) -> set[str]:
    return {
        value.casefold()
        for value in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#'\-]*|[\u4e00-\u9fff]{2,}", text or "")
        if len(value) >= 2
    }


def rank_candidates(candidates: list[dict[str, Any]], topic: str) -> list[dict[str, Any]]:
    if not candidates:
        return []
    topic_terms = _terms(topic)
    max_citations = max(int(item.get("citation_count") or 0) for item in candidates)
    source_rrf_values: list[float] = []
    for candidate in candidates:
        rrf = sum(
            1.0 / (60.0 + max(1, int(source.get("provider_rank") or 1)))
            for source in candidate.get("sources") or []
        )
        source_rrf_values.append(rrf)
    max_rrf = max(source_rrf_values) if source_rrf_values else 0.0
    current_year = datetime.now().year
    for candidate, source_rrf in zip(candidates, source_rrf_values):
        title_terms = _terms(str(candidate.get("title") or ""))
        abstract_terms = _terms(str(candidate.get("abstract") or ""))
        denominator = max(1, len(topic_terms))
        title_match = len(topic_terms & title_terms) / denominator
        abstract_match = len(topic_terms & abstract_terms) / denominator
        topical = min(1.0, title_match * 0.7 + abstract_match * 0.3)
        source_normalized = source_rrf / max_rrf if max_rrf else 0.0
        citations = int(candidate.get("citation_count") or 0)
        citation = math.log1p(citations) / math.log1p(max_citations) if max_citations else 0.0
        year = candidate.get("year")
        recency = max(0.0, min(1.0, 1.0 - (current_year - year) / 20.0)) if isinstance(year, int) else 0.0
        metadata_fields = ("title", "authors", "year", "journal", "abstract")
        metadata_quality = sum(bool(candidate.get(field)) for field in metadata_fields) / len(metadata_fields)
        total = topical * 0.65 + source_normalized * 0.15 + citation * 0.10 + recency * 0.05 + metadata_quality * 0.05
        candidate["score"] = {
            "total": round(total, 6),
            "title_abstract": round(topical, 6),
            "source_rank_rrf": round(source_rrf, 6),
            "source_rank_normalized": round(source_normalized, 6),
            "citation": round(citation, 6),
            "recency": round(recency, 6),
            "metadata_quality": round(metadata_quality, 6),
            "abstract_relevance": None,
        }
    candidates.sort(
        key=lambda item: (
            float((item.get("score") or {}).get("total") or 0.0),
            int(item.get("year") or 0),
        ),
        reverse=True,
    )
    return candidates
