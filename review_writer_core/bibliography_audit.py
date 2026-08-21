"""Non-blocking, multi-source bibliography verification for Library metadata."""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .paper_sources.base import PaperSearchRequest, PaperSourceConnector
from .paper_sources.normalize import normalize_doi, normalize_title


def _value(metadata: dict[str, Any], key: str, default: Any = "") -> Any:
    value = metadata.get(key, default)
    return value.get("value", default) if isinstance(value, dict) else value


def _author_keys(authors: Any) -> set[str]:
    values = authors if isinstance(authors, list) else [authors] if authors else []
    keys = set()
    for value in values:
        if isinstance(value, dict):
            value = (
                value.get("name")
                or " ".join(
                    part for part in (value.get("given"), value.get("family")) if part
                )
            )
        key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())
        if key:
            keys.add(key)
    return keys


def _candidate_score(metadata: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    expected_doi = normalize_doi(_value(metadata, "doi"))
    candidate_doi = normalize_doi(
        (candidate.get("identifiers") or {}).get("doi") or candidate.get("doi")
    )
    expected_title = normalize_title(_value(metadata, "title"))
    candidate_title = normalize_title(candidate.get("title"))
    title_similarity = (
        SequenceMatcher(None, expected_title, candidate_title).ratio()
        if expected_title and candidate_title
        else 0.0
    )
    expected_authors = _author_keys(_value(metadata, "authors", []))
    candidate_authors = _author_keys(candidate.get("authors") or [])
    author_overlap = (
        len(expected_authors & candidate_authors) / max(1, len(expected_authors))
        if expected_authors
        else 0.0
    )
    return {
        "doi_exact": bool(expected_doi and candidate_doi == expected_doi),
        "title_similarity": round(title_similarity, 4),
        "author_overlap": round(author_overlap, 4),
        "year_match": bool(
            _value(metadata, "year")
            and str(_value(metadata, "year")) == str(candidate.get("year") or "")
        ),
    }


def _source_state(error: str) -> str:
    lowered = str(error or "").casefold()
    if "429" in lowered or "rate" in lowered:
        return "rate_limited"
    if "404" in lowered or "not found" in lowered:
        return "not_found"
    return "unavailable"


def _pdf_first_page(path: Path | None, metadata: dict[str, Any]) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"status": "unavailable", "error": "PDF is unavailable."}
    try:
        from pypdf import PdfReader

        text = (PdfReader(str(path), strict=False).pages[0].extract_text() or "")[:12000]
    except Exception as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    title = normalize_title(_value(metadata, "title"))
    normalized_page = normalize_title(text)
    return {
        "status": "verified" if title and title[:80] in normalized_page else "available",
        "title_present": bool(title and title[:80] in normalized_page),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _candidate_value(candidate: dict[str, Any], field: str) -> Any:
    if field == "doi":
        return (candidate.get("identifiers") or {}).get("doi") or candidate.get("doi")
    return candidate.get(field)


def _normalized_field(field: str, value: Any) -> str:
    if field == "doi":
        return normalize_doi(value)
    if field in {"title", "journal"}:
        return normalize_title(value)
    if field == "authors":
        return "|".join(sorted(_author_keys(value)))
    if field == "year":
        match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
        return match.group(0) if match else ""
    return str(value or "").strip().casefold()


def audit_bibliography(
    metadata: dict[str, Any],
    *,
    connectors: list[PaperSourceConnector],
    pdf_path: Path | None = None,
    previous_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify one canonical record without mutating it or depending on an LLM."""

    query = normalize_doi(_value(metadata, "doi")) or str(_value(metadata, "title") or "").strip()
    source_rows: dict[str, Any] = dict((previous_audit or {}).get("sources") or {})
    for connector in connectors:
        result = connector.search(PaperSearchRequest(query=query, limit=5))
        if result.status != "completed":
            source_rows[connector.name] = {
                "status": _source_state(result.error),
                "error": result.error,
                "elapsed_ms": result.elapsed_ms,
            }
            continue
        ranked = []
        for candidate in result.candidates:
            score = _candidate_score(metadata, candidate)
            ranked.append({"candidate": candidate, "match": score})
        ranked.sort(
            key=lambda row: (
                bool(row["match"]["doi_exact"]),
                float(row["match"]["title_similarity"]),
                float(row["match"]["author_overlap"]),
            ),
            reverse=True,
        )
        best = ranked[0] if ranked else None
        if best is None:
            source_rows[connector.name] = {
                "status": "not_found",
                "elapsed_ms": result.elapsed_ms,
            }
            continue
        match = best["match"]
        verified = bool(
            match["doi_exact"]
            and match["title_similarity"] >= 0.86
            and (not _author_keys(_value(metadata, "authors", [])) or match["author_overlap"] >= 0.5)
        )
        row = {
            "status": "verified" if verified else "conflict",
            "elapsed_ms": result.elapsed_ms,
            "match": match,
            "candidate": {
                key: best["candidate"].get(key)
                for key in ("title", "authors", "year", "journal", "landing_url", "identifiers")
            },
        }
        source_rows[connector.name] = row
    pdf_first_page = _pdf_first_page(pdf_path, metadata)
    field_provenance: dict[str, list[dict[str, Any]]] = {}
    conflicts: list[dict[str, Any]] = []
    for field in ("doi", "title", "authors", "year", "journal"):
        provenance: list[dict[str, Any]] = []
        canonical = _value(metadata, field)
        if canonical not in (None, "", []):
            provenance.append(
                {"source": "canonical_metadata", "value": canonical, "confidence": 1.0}
            )
        if field == "title" and pdf_first_page.get("title_present"):
            provenance.append(
                {
                    "source": "pdf_first_page",
                    "value": canonical,
                    "confidence": 0.85,
                }
            )
        for source_name, source_row in source_rows.items():
            candidate = source_row.get("candidate") if isinstance(source_row, dict) else None
            if not isinstance(candidate, dict):
                continue
            value = _candidate_value(candidate, field)
            if value in (None, "", []):
                continue
            match = dict(source_row.get("match") or {})
            confidence = (
                0.99
                if field == "doi" and match.get("doi_exact")
                else max(
                    float(match.get("title_similarity") or 0.0),
                    float(match.get("author_overlap") or 0.0),
                )
            )
            provenance.append(
                {
                    "source": source_name,
                    "value": value,
                    "confidence": round(confidence, 4),
                    "verification_status": source_row.get("status"),
                }
            )
        if provenance:
            field_provenance[field] = provenance
        distinct = {
            normalized
            for item in provenance
            if (normalized := _normalized_field(field, item.get("value")))
        }
        if len(distinct) > 1:
            conflicts.append(
                {
                    "field": field,
                    "status": "unresolved",
                    "candidates": provenance,
                }
            )

    statuses = {str(row.get("status") or "") for row in source_rows.values()}
    overall = (
        "conflict"
        if conflicts or "conflict" in statuses
        else "verified"
        if "verified" in statuses
        else "pending_retry"
        if statuses & {"unavailable", "rate_limited"}
        else "not_found"
    )
    metadata_hash = hashlib.sha256(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "status": overall,
        "source_metadata_sha256": metadata_hash,
        "sources": source_rows,
        "pdf_first_page": pdf_first_page,
        "field_provenance": field_provenance,
        "conflicts": conflicts,
        "resolved_by": str((previous_audit or {}).get("resolved_by") or "unresolved"),
        "automatic_update_eligible": "verified" in statuses and not conflicts,
        "canonical_metadata_changed": False,
        "manual_review_status": str(
            (previous_audit or {}).get("manual_review_status") or "not_reviewed"
        ),
    }
