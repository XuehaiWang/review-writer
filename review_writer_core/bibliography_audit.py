"""Non-blocking, multi-source bibliography verification for Library metadata."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .author_metadata import author_quality_issues, authors_are_publication_ready
from .paper_sources.base import PaperSearchRequest, PaperSourceConnector
from .paper_sources.normalize import normalize_doi, normalize_title
from .publication_metadata import (
    extract_front_matter_doi,
    read_pdf_first_page_text,
    resolve_local_publication_extraction,
)


class BibliographyResolutionError(ValueError):
    """A user-facing bibliography resolution payload is incomplete or unsafe."""

    def __init__(self, message: str, *, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = list(fields or [])


DOCUMENT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "journal_article": ("title", "authors", "journal", "year"),
    "book_chapter": ("authors", "title", "book_title", "publisher", "year"),
    "thesis": ("authors", "title", "school", "year"),
    "patent": ("title", "authors", "patent_number", "year"),
    "supporting_information": ("title", "parent_paper_id"),
    "other": ("title", "responsible_entity", "source_type", "year", "locator"),
}

RESOLVABLE_FIELDS = (
    "title",
    "authors",
    "journal",
    "year",
    "first_publication_date",
    "bibliographic_year",
    "publication_status",
    "volume",
    "issue",
    "pages",
    "article_number",
    "doi",
    "book_title",
    "publisher",
    "school",
    "degree_type",
    "patent_number",
    "responsible_entity",
    "source_type",
    "locator",
)


def _candidate_id(source: str, candidate: dict[str, Any]) -> str:
    identity = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def bibliography_candidates(audit: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one normalized candidate list for both current and legacy audits."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, raw_row in (audit.get("sources") or {}).items():
        if not isinstance(raw_row, dict):
            continue
        raw_candidates = raw_row.get("candidates")
        rows = raw_candidates if isinstance(raw_candidates, list) else []
        if not rows and isinstance(raw_row.get("candidate"), dict):
            rows = [
                {
                    "candidate": raw_row["candidate"],
                    "match": raw_row.get("match") or {},
                    "status": raw_row.get("status"),
                }
            ]
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else raw
            candidate = dict(candidate or {})
            if not candidate:
                continue
            candidate_id = str(raw.get("candidate_id") or _candidate_id(str(source), candidate))
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "source": str(source),
                    "status": str(raw.get("status") or raw_row.get("status") or ""),
                    "match": dict(raw.get("match") or raw_row.get("match") or {}),
                    "fields": candidate,
                }
            )
    normalized.sort(
        key=lambda item: (
            bool((item.get("match") or {}).get("doi_exact")),
            float((item.get("match") or {}).get("title_similarity") or 0.0),
            float((item.get("match") or {}).get("author_overlap") or 0.0),
        ),
        reverse=True,
    )
    return normalized


def _resolution_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _normalize_resolution_fields(
    fields: dict[str, Any], *, document_type: str, parent_paper_id: str = ""
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in RESOLVABLE_FIELDS:
        value = _resolution_value(fields.get(key))
        if value not in (None, "", []):
            normalized[key] = value
    identifiers = fields.get("identifiers")
    if "doi" not in normalized and isinstance(identifiers, dict):
        doi_value = identifiers.get("doi")
        if doi_value not in (None, "", []):
            normalized["doi"] = doi_value
    if "publication_date" in fields and "first_publication_date" not in normalized:
        value = _resolution_value(fields.get("publication_date"))
        if value not in (None, "", []):
            normalized["first_publication_date"] = value
    if isinstance(normalized.get("authors"), str):
        normalized["authors"] = [
            item.strip()
            for item in re.split(r"\s*;\s*|\r?\n", str(normalized["authors"]))
            if item.strip()
        ]
    if "year" in normalized:
        match = re.fullmatch(r"(?:18|19|20|21)\d{2}", str(normalized["year"]).strip())
        if not match:
            raise BibliographyResolutionError(
                "Publication year must be a four-digit integer.", fields=["year"]
            )
        normalized["year"] = int(match.group(0))
        normalized.setdefault("bibliographic_year", normalized["year"])
    if "doi" in normalized:
        doi = normalize_doi(normalized["doi"])
        if not doi or not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            raise BibliographyResolutionError(
                "DOI is not in a valid canonical format.", fields=["doi"]
            )
        normalized["doi"] = doi
    normalized["document_type"] = document_type
    if parent_paper_id:
        normalized["parent_paper_id"] = parent_paper_id
    return normalized


def _missing_resolution_fields(
    fields: dict[str, Any], *, document_type: str, parent_paper_id: str = ""
) -> list[str]:
    available = {
        key
        for key, value in fields.items()
        if _resolution_value(value) not in (None, "", [])
    }
    if parent_paper_id:
        available.add("parent_paper_id")
    return [
        field
        for field in DOCUMENT_REQUIREMENTS.get(document_type, DOCUMENT_REQUIREMENTS["other"])
        if field not in available
    ]


_BIBLIOGRAPHY_RESIDUE = re.compile(
    r"<[^>]+>|\b(?:received|accepted|cite this|read online|"
    r"article recommendations?|supporting information)\b",
    re.IGNORECASE,
)


def bibliography_field_readiness(
    metadata: dict[str, Any], audit: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return field-level publication readiness for canonical metadata.

    A traceable local PDF or a paper-level audit status is not sufficient.
    Final release depends on the canonical fields that will actually be
    rendered in the reference list.
    """

    audit_row = dict(audit or {})
    document_type = str(
        _value(metadata, "document_type")
        or audit_row.get("document_type")
        or "journal_article"
    ).strip()
    if document_type not in DOCUMENT_REQUIREMENTS:
        document_type = "journal_article"
    parent_paper_id = str(
        _value(metadata, "parent_paper_id")
        or audit_row.get("parent_paper_id")
        or ""
    ).strip()
    missing = _missing_resolution_fields(
        metadata,
        document_type=document_type,
        parent_paper_id=parent_paper_id,
    )
    if document_type == "journal_article":
        canonical_doi = normalize_doi(_value(metadata, "doi"))
        online_first = str(
            _value(metadata, "publication_status") or ""
        ).casefold() in {"online_first", "early_view", "accepted_manuscript"}
        has_locator = bool(
            _value(metadata, "pages")
            or _value(metadata, "article_number")
            or _value(metadata, "locator")
        )
        if not has_locator and not (canonical_doi and online_first):
            missing.append("pages_or_article_number")
        if not canonical_doi and not has_locator:
            missing.append("doi_or_locator")
    polluted: list[str] = []
    raw_authors = _value(metadata, "authors")
    author_issues = author_quality_issues(raw_authors)
    if raw_authors not in (None, "", []) and author_issues:
        polluted.append("authors")
    for field in (
        "title",
        "authors",
        "journal",
        "book_title",
        "publisher",
        "pages",
        "article_number",
    ):
        value = _value(metadata, field)
        serialized = " ".join(map(str, value)) if isinstance(value, list) else str(value or "")
        if serialized and _BIBLIOGRAPHY_RESIDUE.search(serialized):
            polluted.append(field)
    year = _value(metadata, "year") or _value(metadata, "publication_year")
    if year not in (None, "") and not re.fullmatch(r"(?:18|19|20|21)\d{2}", str(year).strip()):
        polluted.append("year")
    unresolved_conflicts = [
        dict(value)
        for value in audit_row.get("unresolved_conflicts") or []
        if isinstance(value, dict)
    ]
    missing = list(dict.fromkeys(missing))
    polluted = list(dict.fromkeys(polluted))
    return {
        "document_type": document_type,
        "parent_paper_id": parent_paper_id,
        "required_fields": list(DOCUMENT_REQUIREMENTS[document_type]),
        "missing_fields": missing,
        "polluted_fields": polluted,
        "author_quality_issues": author_issues,
        "unresolved_conflicts": unresolved_conflicts,
        "ready": not missing and not polluted and not unresolved_conflicts,
    }


def _manual_evidence(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("manual_evidence")
    evidence = dict(raw) if isinstance(raw, dict) else {}
    evidence_type = str(evidence.get("evidence_type") or "").strip()
    location = str(evidence.get("location") or "").strip()
    if not evidence_type or not location:
        raise BibliographyResolutionError(
            "Manual bibliography resolution requires a source type and location.",
            fields=["manual_evidence.evidence_type", "manual_evidence.location"],
        )
    return {
        "evidence_type": evidence_type,
        "location": location,
        "note": str(evidence.get("note") or "").strip(),
    }


def _human_field(value: Any, source: str) -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "confidence": 1.0,
        "human_checked": True,
    }


def bibliography_change_impact(
    before: dict[str, Any], after: dict[str, Any], changed_fields: list[str]
) -> dict[str, Any]:
    changed = set(changed_fields)
    impact = "bibliography_only"
    affected = ["bibliography_overlay", "references", "final_release_check"]
    if "doi" in changed and normalize_doi(_value(before, "doi")) != normalize_doi(_value(after, "doi")):
        impact = "identity_review"
        affected = ["paper_identity", "deduplication", "matrix", "downstream_from_matrix"]
    elif changed & {"title", "authors"}:
        title_similarity = SequenceMatcher(
            None, normalize_title(_value(before, "title")), normalize_title(_value(after, "title"))
        ).ratio()
        if title_similarity < 0.94 or "authors" in changed:
            impact = "identity_review"
            affected = ["paper_identity", "deduplication", "metadata_index", "matrix_review"]
    if "year" in changed or "bibliographic_year" in changed:
        if impact == "bibliography_only":
            impact = "scope_review"
        affected = list(dict.fromkeys([*affected, "coverage_diagnostics", "scope_review"]))
    return {"impact": impact, "affected": affected, "changed_fields": sorted(changed)}


def resolve_bibliography(
    metadata: dict[str, Any],
    audit: dict[str, Any],
    payload: dict[str, Any],
    *,
    parent_exists: bool = False,
    resolved_at: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Resolve an audit without allowing an evidence-free release override."""

    action = str(payload.get("action") or "").strip()
    document_type = str(payload.get("document_type") or "journal_article").strip()
    if document_type not in DOCUMENT_REQUIREMENTS:
        raise BibliographyResolutionError("Unknown bibliography document type.")
    parent_paper_id = str(payload.get("parent_paper_id") or "").strip()
    timestamp = resolved_at or datetime.now(timezone.utc).isoformat()
    updated_metadata = json.loads(json.dumps(metadata, ensure_ascii=False))
    updated_audit = json.loads(json.dumps(audit or {}, ensure_ascii=False))
    changed_fields: list[str] = []
    evidence: dict[str, str] | None = None
    selected_candidate_source = ""
    selected_candidate_id = ""

    if action == "reject":
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise BibliographyResolutionError(
                "Rejecting a bibliography record requires a reason.", fields=["reason"]
            )
        updated_audit.update(
            {
                "manual_review_status": "rejected",
                "resolved_by": "human",
                "resolved_at": timestamp,
                "resolution_reason": reason,
                "bibliography_role": "rejected",
                "direct_claim_eligible": False,
                "context_only": False,
            }
        )
        return updated_metadata, updated_audit, {
            "changed_fields": [],
            "impact": {"impact": "matrix_review", "affected": ["matrix", "downstream_from_matrix"], "changed_fields": []},
        }

    if action == "accept_candidate":
        selected_candidate_id = str(payload.get("candidate_id") or "").strip()
        selected = next(
            (item for item in bibliography_candidates(updated_audit) if item["candidate_id"] == selected_candidate_id),
            None,
        )
        if selected is None:
            raise BibliographyResolutionError(
                "The selected bibliography candidate is unavailable or stale.",
                fields=["candidate_id"],
            )
        selected_candidate_source = str(selected.get("source") or "")
        fields = _normalize_resolution_fields(
            dict(selected.get("fields") or {}),
            document_type=document_type,
            parent_paper_id=parent_paper_id,
        )
    else:
        evidence = _manual_evidence(payload)
        fields = _normalize_resolution_fields(
            dict(payload.get("fields") or {}),
            document_type=document_type,
            parent_paper_id=parent_paper_id,
        )

    missing = _missing_resolution_fields(
        fields, document_type=document_type, parent_paper_id=parent_paper_id
    )
    if action in {"save_manual", "accept_candidate"} and missing:
        raise BibliographyResolutionError(
            "The bibliography record is missing required fields: " + ", ".join(missing),
            fields=missing,
        )
    if document_type == "supporting_information" and not parent_exists:
        raise BibliographyResolutionError(
            "Supporting Information must be bound to a citable parent paper.",
            fields=["parent_paper_id"],
        )

    source = (
        f"bibliography_candidate:{selected_candidate_source}"
        if action == "accept_candidate"
        else "bibliography_manual_resolution"
    )
    for field, value in fields.items():
        if field in {"document_type", "parent_paper_id"}:
            continue
        if _normalized_field(field, _value(updated_metadata, field)) == _normalized_field(field, value):
            current = updated_metadata.get(field)
            if isinstance(current, dict) and not current.get("human_checked"):
                updated_metadata[field] = _human_field(value, source)
                changed_fields.append(field)
            continue
        updated_metadata[field] = _human_field(value, source)
        changed_fields.append(field)
    if _value(updated_metadata, "document_type") != document_type:
        updated_metadata["document_type"] = document_type
        changed_fields.append("document_type")
    if parent_paper_id:
        if str(_value(updated_metadata, "parent_paper_id") or "") != parent_paper_id:
            updated_metadata["parent_paper_id"] = parent_paper_id
            changed_fields.append("parent_paper_id")

    own_citable_identity = not missing and document_type != "supporting_information"
    direct_claim_eligible = bool(own_citable_identity or parent_exists)
    if action == "supporting_only":
        updated_audit_status = "supporting_only"
        bibliography_role = "supporting_only"
        context_only = not direct_claim_eligible
    else:
        updated_audit_status = "resolved"
        bibliography_role = "primary"
        context_only = False
        direct_claim_eligible = True
    updated_audit.update(
        {
            "status": "verified" if direct_claim_eligible else "not_found",
            "manual_review_status": updated_audit_status,
            "resolved_by": "human",
            "resolved_at": timestamp,
            "resolved_fields": sorted(set(changed_fields) | set(fields)),
            "selected_candidate_source": selected_candidate_source,
            "selected_candidate_id": selected_candidate_id,
            "bibliography_role": bibliography_role,
            "parent_paper_id": parent_paper_id or None,
            "primary_reference_allowed": action != "supporting_only",
            "direct_claim_eligible": direct_claim_eligible,
            "context_only": context_only,
            "manual_evidence": evidence,
            "resolution_reason": str(payload.get("reason") or "").strip(),
            "unresolved_conflicts": [],
        }
    )
    impact = bibliography_change_impact(metadata, updated_metadata, changed_fields)
    return updated_metadata, updated_audit, {
        "changed_fields": sorted(set(changed_fields)),
        "impact": impact,
    }


def _value(metadata: dict[str, Any], key: str, default: Any = "") -> Any:
    value = metadata.get(key, default)
    return value.get("value", default) if isinstance(value, dict) else value


def _field_confidence(metadata: dict[str, Any], key: str) -> float:
    value = metadata.get(key)
    if isinstance(value, dict):
        try:
            return max(0.0, min(1.0, float(value.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            return 0.0
    return 0.5 if value not in (None, "", []) else 0.0


def _field_human_checked(metadata: dict[str, Any], key: str) -> bool:
    value = metadata.get(key)
    return bool(isinstance(value, dict) and value.get("human_checked"))


def _mineru_field_confirmed(metadata: dict[str, Any], key: str) -> bool:
    """Return whether one canonical field is grounded in literal MinerU evidence."""

    value = metadata.get(key)
    if not isinstance(value, dict) or value.get("value") in (None, "", []):
        return False
    if bool(value.get("human_checked")):
        return True
    if key == "authors" and not authors_are_publication_ready(value.get("value")):
        return False
    source = str(value.get("source") or "").casefold()
    status = str(value.get("verification_status") or "").casefold()
    evidence = value.get("evidence")
    return bool(
        source.startswith("mineru_")
        and status == "confirmed"
        and isinstance(evidence, dict)
        and str(evidence.get("source_text") or "").strip()
        and float(value.get("confidence") or 0.0) >= 0.88
    )


def _mineru_record_is_locally_complete(
    metadata: dict[str, Any], *, document_type: str
) -> bool:
    """Accept a complete, clean article identity without a network round trip."""

    if document_type != "journal_article":
        return False
    if not all(
        _mineru_field_confirmed(metadata, field)
        for field in DOCUMENT_REQUIREMENTS[document_type]
    ):
        return False
    if not any(
        _mineru_field_confirmed(metadata, field)
        for field in ("pages", "article_number", "doi")
    ):
        return False
    readiness = bibliography_field_readiness(metadata, {"unresolved_conflicts": []})
    return bool(readiness.get("ready"))


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
        cleaned = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
        # MinerU sometimes keeps multiple authors and HTML footnote fragments in
        # one list item.  Compare the actual names, not the serialized markup.
        for name in re.split(r"\s+(?:and|&)\s+|\s*;\s*", cleaned, flags=re.IGNORECASE):
            key = re.sub(
                r"[^a-z0-9\u4e00-\u9fff]+", "", str(name or "").casefold()
            )
            if key and key not in {"and", "sup"}:
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
    text = read_pdf_first_page_text(path)
    if not text:
        return {"status": "unavailable", "error": "PDF first-page text is unavailable."}
    title = normalize_title(_value(metadata, "title"))
    normalized_page = normalize_title(text)
    return {
        "status": "verified" if title and title[:80] in normalized_page else "available",
        "title_present": bool(title and title[:80] in normalized_page),
        "doi": extract_front_matter_doi(text).get("value") or "",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _candidate_value(candidate: dict[str, Any], field: str) -> Any:
    if field == "doi":
        return (candidate.get("identifiers") or {}).get("doi") or candidate.get("doi")
    if field == "bibliographic_year":
        return candidate.get("bibliographic_year") or candidate.get("year")
    if field == "first_publication_date":
        return candidate.get("first_publication_date") or candidate.get("publication_date")
    if field == "publication_status":
        value = str(candidate.get("publication_status") or "").strip()
        return value if value and value != "unknown" else None
    return candidate.get(field)


def _normalized_field(field: str, value: Any) -> str:
    if field == "doi":
        return normalize_doi(value)
    if field in {"title", "journal"}:
        return normalize_title(value)
    if field == "authors":
        return "|".join(sorted(_author_keys(value)))
    if field in {"year", "bibliographic_year"}:
        match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
        return match.group(0) if match else ""
    if field == "first_publication_date":
        normalized = str(value or "").strip().casefold()
        match = re.match(r"^((?:18|19|20|21)\d{2})(?:[-/.](\d{1,2}))?", normalized)
        if not match:
            return normalized
        return (
            f"{match.group(1)}-{int(match.group(2)):02d}"
            if match.group(2)
            else match.group(1)
        )
    return str(value or "").strip().casefold()


def _local_extraction_value(local_extraction: dict[str, Any], field: str) -> Any:
    basic = local_extraction.get("basic_info") or {}
    evidence = local_extraction.get("publication_evidence") or {}
    year = basic.get("publication_year")
    if field in {"year", "bibliographic_year"}:
        return year
    if field == "first_publication_date":
        return basic.get("publication_date") or (str(year) if year else None)
    if field == "publication_status":
        date_type = str(evidence.get("date_type") or "unknown")
        if date_type in {"published_online", "early_view"}:
            return "online_first"
        if local_extraction.get("status") == "reliable":
            return "issue_assigned"
    return None


def audit_bibliography(
    metadata: dict[str, Any],
    *,
    connectors: list[PaperSourceConnector],
    pdf_path: Path | None = None,
    previous_audit: dict[str, Any] | None = None,
    local_extraction: dict[str, Any] | None = None,
    document_agent_extraction: dict[str, Any] | None = None,
    network_mode: str = "fallback",
) -> dict[str, Any]:
    """Verify one canonical record, using providers only when locally necessary."""

    pdf_first_page = _pdf_first_page(pdf_path, metadata)
    pdf_first_page_text = str(pdf_first_page.pop("text", "") or "")
    if not isinstance(local_extraction, dict):
        local_extraction = resolve_local_publication_extraction(
            markdown_text="",
            pdf_first_page_text=pdf_first_page_text,
            filename=pdf_path.name if pdf_path else "",
        )
    local_status = str(local_extraction.get("status") or "insufficient")
    normalized_network_mode = str(network_mode or "fallback").strip().casefold()
    if normalized_network_mode not in {"fallback", "force", "disabled"}:
        normalized_network_mode = "fallback"
    network_used = normalized_network_mode == "force" or (
        normalized_network_mode == "fallback" and local_status != "reliable"
    )
    agent_extraction = (
        dict(document_agent_extraction)
        if isinstance(document_agent_extraction, dict)
        else {}
    )
    agent_fields = (
        dict(agent_extraction.get("fields") or {})
        if isinstance(agent_extraction.get("fields"), dict)
        else {}
    )
    identity_metadata = json.loads(json.dumps(metadata, ensure_ascii=False))
    for field, raw in agent_fields.items():
        if field not in {
            "doi",
            "title",
            "authors",
            "year",
            "journal",
            "volume",
            "issue",
            "pages",
            "article_number",
        }:
            continue
        row = dict(raw) if isinstance(raw, dict) else {}
        value = row.get("value")
        if value in (None, "", []) or _field_human_checked(metadata, field):
            continue
        current = _value(metadata, field)
        # Agent evidence is an identity recovery hint, not permission to replace
        # a strong stored identity before source/provider comparison.
        if current not in (None, "", []) and _field_confidence(metadata, field) >= 0.75:
            continue
        identity_metadata[field] = {
            "value": value,
            "source": "bounded_document_agent",
            "confidence": float(row.get("confidence") or 0.0),
            "human_checked": False,
        }

    pdf_doi = normalize_doi(pdf_first_page.get("doi"))
    canonical_doi = normalize_doi(_value(metadata, "doi"))
    agent_doi = normalize_doi(_value(identity_metadata, "doi"))
    doi_is_trusted = bool(
        canonical_doi
        and (
            _field_human_checked(metadata, "doi")
            or _field_confidence(metadata, "doi") >= 0.9
        )
    )
    query = pdf_doi or (
        canonical_doi
        if doi_is_trusted
        else agent_doi
        or str(_value(identity_metadata, "title") or "").strip()
    )
    if pdf_doi and pdf_doi != canonical_doi:
        identity_metadata = json.loads(json.dumps(identity_metadata, ensure_ascii=False))
        identity_metadata["doi"] = {
            "value": pdf_doi,
            "source": "pdf_first_page",
            "confidence": 1.0,
            "human_checked": False,
        }
    # Preserve successful providers across a partial network retry, but discard
    # old unconditional lookup rows once reliable local evidence skips network.
    source_rows: dict[str, Any] = (
        dict((previous_audit or {}).get("sources") or {}) if network_used else {}
    )
    for connector in connectors if network_used else []:
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
            score = _candidate_score(identity_metadata, candidate)
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
        expected_authors = _author_keys(_value(metadata, "authors", []))
        title_identity = bool(
            len(normalize_title(_value(metadata, "title"))) >= 20
            and match["title_similarity"] >= 0.94
            and (not expected_authors or match["author_overlap"] >= 0.45)
        )
        doi_identity = bool(
            match["doi_exact"]
            and match["title_similarity"] >= 0.86
            and (
                bool(pdf_doi)
                or not expected_authors
                or match["author_overlap"] >= 0.5
            )
        )
        verified = title_identity or doi_identity
        row = {
            "status": "verified" if verified else "conflict",
            "elapsed_ms": result.elapsed_ms,
            "match": match,
            "candidate": {
                key: best["candidate"].get(key)
                for key in (
                    "title",
                    "authors",
                    "year",
                    "bibliographic_year",
                    "first_publication_date",
                    "publication_date",
                    "publication_status",
                    "journal",
                    "landing_url",
                    "identifiers",
                )
            },
            "candidates": [
                {
                    "candidate_id": _candidate_id(connector.name, dict(item["candidate"])),
                    "status": (
                        "verified"
                        if bool(
                            item["match"].get("doi_exact")
                            or (
                                float(item["match"].get("title_similarity") or 0.0) >= 0.94
                                and float(item["match"].get("author_overlap") or 0.0) >= 0.45
                            )
                        )
                        else "conflict"
                    ),
                    "match": dict(item["match"]),
                    "candidate": {
                        key: item["candidate"].get(key)
                        for key in (
                            "title",
                            "authors",
                            "year",
                            "bibliographic_year",
                            "first_publication_date",
                            "publication_date",
                            "publication_status",
                            "journal",
                            "volume",
                            "issue",
                            "pages",
                            "article_number",
                            "landing_url",
                            "identifiers",
                        )
                    },
                }
                for item in ranked[:5]
            ],
        }
        source_rows[connector.name] = row
    field_provenance: dict[str, list[dict[str, Any]]] = {}
    conflicts: list[dict[str, Any]] = []
    for field in (
        "doi",
        "title",
        "authors",
        "year",
        "bibliographic_year",
        "first_publication_date",
        "publication_status",
        "journal",
        "volume",
        "issue",
        "pages",
        "article_number",
    ):
        provenance: list[dict[str, Any]] = []
        canonical = _value(metadata, field)
        if field == "publication_status" and str(canonical or "").casefold() == "unknown":
            canonical = None
        if canonical not in (None, "", []):
            provenance.append(
                {
                    "source": "canonical_metadata",
                    "value": canonical,
                    "confidence": _field_confidence(metadata, field),
                    "human_checked": _field_human_checked(metadata, field),
                }
            )
        if field == "title" and pdf_first_page.get("title_present"):
            provenance.append(
                {
                    "source": "pdf_first_page",
                    "value": canonical,
                    "confidence": 0.85,
                }
            )
        if field == "doi" and pdf_doi:
            provenance.append(
                {
                    "source": "pdf_first_page",
                    "value": pdf_doi,
                    "confidence": 1.0,
                    "verification_status": "verified",
                }
            )
        if field in {
            "year",
            "bibliographic_year",
            "first_publication_date",
            "publication_status",
        }:
            evidence = local_extraction.get("publication_evidence") or {}
            local_value = _local_extraction_value(local_extraction, field)
            if local_value not in (None, "", []):
                provenance.append(
                    {
                        "source": "local_document",
                        "value": local_value,
                        "confidence": float(evidence.get("confidence") or 0.0),
                        "verification_status": (
                            "verified" if local_status == "reliable" else "available"
                        ),
                        "source_location": evidence.get("source_location"),
                        "source_text": evidence.get("source_text"),
                    }
                )
        agent_row = agent_fields.get(field)
        if isinstance(agent_row, dict) and agent_row.get("value") not in (None, "", []):
            provenance.append(
                {
                    "source": "bounded_document_agent",
                    "value": agent_row.get("value"),
                    "confidence": float(agent_row.get("confidence") or 0.0),
                    "verification_status": agent_row.get("verification_status"),
                    "source_location": agent_row.get("source_location"),
                    "source_text": agent_row.get("source_excerpt"),
                    "role": agent_row.get("role"),
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

    canonical_updates: dict[str, dict[str, Any]] = {}
    for field, provenance in field_provenance.items():
        if _field_human_checked(metadata, field):
            continue
        verified_candidates = [
            item
            for item in provenance
            if item.get("source") != "canonical_metadata"
            and item.get("verification_status") == "verified"
            and _normalized_field(field, item.get("value"))
        ]
        if not verified_candidates:
            continue
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in verified_candidates:
            grouped.setdefault(
                _normalized_field(field, item.get("value")), []
            ).append(item)
        if len(grouped) != 1:
            continue
        best_group = next(iter(grouped.values()))
        best = max(best_group, key=lambda item: float(item.get("confidence") or 0.0))
        best_confidence = float(best.get("confidence") or 0.0)
        canonical_normalized = _normalized_field(field, _value(metadata, field))
        candidate_normalized = _normalized_field(field, best.get("value"))
        if best_confidence < 0.94:
            continue
        canonical_is_polluted_author = bool(
            field == "authors"
            and not authors_are_publication_ready(_value(metadata, "authors"))
        )
        if canonical_normalized == candidate_normalized and not canonical_is_polluted_author:
            continue
        # Human-reviewed fields were excluded above. A literal source-grounded
        # document/provider value must beat an unreviewed parser confidence,
        # even when that legacy confidence was incorrectly recorded as 1.0.
        canonical_updates[field] = {
            "value": best.get("value"),
            "source": f"bibliography_audit:{best.get('source')}",
            "confidence": round(best_confidence, 4),
            "human_checked": False,
        }

    for conflict in conflicts:
        if conflict.get("field") in canonical_updates:
            conflict["status"] = "auto_resolved"
            conflict["resolved_value"] = canonical_updates[str(conflict["field"])]["value"]

    unresolved_conflicts = [
        conflict for conflict in conflicts if conflict.get("status") == "unresolved"
    ]
    statuses = {str(row.get("status") or "") for row in source_rows.values()}
    overall = (
        "conflict"
        if local_status == "conflict"
        or unresolved_conflicts
        or ("conflict" in statuses and "verified" not in statuses)
        else "verified"
        if "verified" in statuses or local_status == "reliable"
        else "pending_retry"
        if statuses & {"unavailable", "rate_limited"}
        else "not_found"
    )
    metadata_hash = hashlib.sha256(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    preview_metadata = json.loads(json.dumps(metadata, ensure_ascii=False))
    for field, proposed in canonical_updates.items():
        if isinstance(proposed, dict) and proposed.get("value") not in (None, "", []):
            preview_metadata[field] = dict(proposed)
    document_type = str(_value(preview_metadata, "document_type") or "journal_article")
    if document_type not in DOCUMENT_REQUIREMENTS:
        document_type = "journal_article"
    automatic_missing = _missing_resolution_fields(
        preview_metadata, document_type=document_type
    )
    preview_readiness = bibliography_field_readiness(
        preview_metadata,
        {"unresolved_conflicts": unresolved_conflicts},
    )
    for field in preview_readiness.get("polluted_fields") or []:
        if field in DOCUMENT_REQUIREMENTS[document_type] and field not in automatic_missing:
            automatic_missing.append(field)
    automatic_missing = list(dict.fromkeys(automatic_missing))
    if any(
        field in DOCUMENT_REQUIREMENTS[document_type]
        for field in preview_readiness.get("polluted_fields") or []
    ):
        overall = "conflict"
    mineru_locally_complete = _mineru_record_is_locally_complete(
        preview_metadata,
        document_type=document_type,
    )
    if overall != "conflict" and mineru_locally_complete and not unresolved_conflicts:
        overall = "verified"
    if (
        overall != "verified"
        and str(agent_extraction.get("status") or "") == "reliable"
        and canonical_updates
        and not automatic_missing
        and not unresolved_conflicts
    ):
        overall = "verified"
    previous_manual_status = str(
        (previous_audit or {}).get("manual_review_status") or "not_reviewed"
    )
    automatically_resolved = bool(
        previous_manual_status == "not_reviewed"
        and overall == "verified"
        and not unresolved_conflicts
        and not automatic_missing
    )
    verification_method = (
        "local_document+provider"
        if network_used
        else "mineru_local_document"
        if mineru_locally_complete
        else "local_document"
        if local_status == "reliable"
        else "local_document_insufficient"
    )
    extraction_method = str(agent_extraction.get("method") or "")
    if "bounded_document_agent" in extraction_method:
        verification_method += "+bounded_agent"
    elif agent_extraction and "mineru" not in verification_method:
        verification_method += "+mineru_rules"
    audit = {
        "schema_version": 2,
        "status": overall,
        "verification_method": verification_method,
        "network_lookup": {
            "mode": normalized_network_mode,
            "used": network_used,
            "reason": (
                "manual_force"
                if normalized_network_mode == "force"
                else "local_evidence_insufficient_or_conflicting"
                if network_used
                else "local_evidence_reliable"
                if local_status == "reliable"
                else "disabled"
            ),
        },
        "source_metadata_sha256": metadata_hash,
        "sources": source_rows,
        "pdf_first_page": pdf_first_page,
        "local_extraction": local_extraction,
        "document_agent_extraction": agent_extraction,
        "field_provenance": field_provenance,
        "conflicts": conflicts,
        "unresolved_conflicts": unresolved_conflicts,
        "canonical_updates": canonical_updates,
        "resolved_by": (
            "automatic"
            if automatically_resolved or canonical_updates
            else str((previous_audit or {}).get("resolved_by") or "unresolved")
        ),
        "automatic_update_eligible": bool(canonical_updates),
        "canonical_metadata_changed": False,
        "manual_review_status": "resolved" if automatically_resolved else previous_manual_status,
        "resolved_at": (
            datetime.now(timezone.utc).isoformat()
            if automatically_resolved
            else (previous_audit or {}).get("resolved_at")
        ),
        "resolved_fields": (
            sorted(
                field
                for field in DOCUMENT_REQUIREMENTS[document_type]
                if field != "parent_paper_id"
            )
            if automatically_resolved
            else list((previous_audit or {}).get("resolved_fields") or [])
        ),
        "document_type": document_type,
        "automatic_resolution_missing_fields": automatic_missing,
        "field_readiness": preview_readiness,
    }
    audit["candidates"] = bibliography_candidates(audit)
    return audit


def apply_bibliography_updates(
    metadata: dict[str, Any], audit: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Apply only high-confidence, non-human canonical updates from an audit."""

    updated = json.loads(json.dumps(metadata, ensure_ascii=False))
    changed: list[str] = []
    updates = audit.get("canonical_updates") or {}
    if not isinstance(updates, dict):
        return updated, changed
    for field in (
        "doi",
        "title",
        "authors",
        "year",
        "bibliographic_year",
        "first_publication_date",
        "publication_status",
        "journal",
        "volume",
        "issue",
        "pages",
        "article_number",
    ):
        proposed = updates.get(field)
        if not isinstance(proposed, dict) or proposed.get("value") in (None, "", []):
            continue
        if _field_human_checked(updated, field):
            continue
        normalized_equal = _normalized_field(
            field, _value(updated, field)
        ) == _normalized_field(field, proposed.get("value"))
        replace_polluted_authors = bool(
            field == "authors"
            and not authors_are_publication_ready(_value(updated, "authors"))
            and authors_are_publication_ready(proposed.get("value"))
        )
        if normalized_equal and not replace_polluted_authors:
            continue
        updated[field] = dict(proposed)
        changed.append(field)
    if "year" in changed and "bibliographic_year" not in changed:
        updated["bibliographic_year"] = {
            **dict(updated["year"]),
            "source": str(updated["year"].get("source") or "bibliography_audit"),
        }
        changed.append("bibliographic_year")
    if (
        ("year" in changed or "bibliographic_year" in changed)
        and "publication_status" not in changed
        and str(_value(updated, "publication_status") or "unknown") == "unknown"
    ):
        updated["publication_status"] = {
            "value": "issue_assigned",
            "source": "bibliography_audit",
            "confidence": max(
                float((updated.get(field) or {}).get("confidence") or 0.0)
                for field in changed
                if isinstance(updated.get(field), dict)
            ),
            "human_checked": False,
        }
        changed.append("publication_status")
    return updated, changed
