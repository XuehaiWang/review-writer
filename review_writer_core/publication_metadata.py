"""Publication-date extraction from untrusted paper front matter.

The canonical strategy is local-document first: deterministic rules and a
strictly validated model reading the MinerU Markdown/PDF first page establish
the publication date. External bibliography providers are a fallback only
when this module reports insufficient or conflicting local evidence.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


_YEAR_RE = re.compile(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
_REFERENCES_HEADING = re.compile(
    r"(?im)^\s*#{0,6}\s*(?:references|reference list|bibliography|works cited|参考文献|引用文献)\s*$"
)
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_DATE_TOKEN = rf"(?:\d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}|\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}|(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}}|(?:18|19|20|21)\d{{2}})"
_ONLINE_LABEL = re.compile(
    rf"\b(?:published\s+online|first\s+published|online\s+first|published\s+ahead\s+of\s+print|publication\s+date)\b\s*[:;,\-]?\s*(?P<date>{_DATE_TOKEN})",
    re.I,
)
_ISSUED_LABEL = re.compile(
    rf"\b(?:published|issued)\b\s*[:;,\-]?\s*(?P<date>{_DATE_TOKEN})",
    re.I,
)
_COPYRIGHT_YEAR = re.compile(
    r"(?:©|\(c\)|copyright(?:ed)?(?:\s+by)?)\s*(?P<year>(?:18|19|20|21)\d{2})",
    re.I,
)
_JOURNAL_HEADER_YEAR = re.compile(
    r"(?im)^\s*[A-Z][^\n]{2,100}?(?P<year>(?:18|19|20|21)\d{2})\s*,\s*"
    r"(?:vol(?:ume)?\.?\s*)?\d+\s*,\s*(?:no\.?|issue)\s*\d+",
)
_EXCLUDED_DATE_CONTEXT = re.compile(
    r"\b(?:received|revised|accepted|downloaded|accessed|retrieved|created|modified|uploaded|scanned)\b",
    re.I,
)
_MONTH_VALUE = re.compile(r"^((?:18|19|20|21)\d{2})-(0[1-9]|1[0-2])$")

RELIABLE_PUBLICATION_DATE_TYPES = frozenset(
    {"published_online", "issue_date", "published", "early_view"}
)
PUBLICATION_DATE_TYPES = frozenset(
    {
        *RELIABLE_PUBLICATION_DATE_TYPES,
        "accepted",
        "received",
        "copyright",
        "unknown",
    }
)


def _field(value: Any, source: str, confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "confidence": round(float(confidence), 3),
        "human_checked": False,
    }


def front_matter_text(text: str, *, limit: int = 30_000) -> str:
    """Return article front matter without allowing references into extraction."""

    candidate = str(text or "")[: max(1, int(limit))]
    match = _REFERENCES_HEADING.search(candidate)
    return candidate[: match.start()] if match else candidate


def read_pdf_first_page_text(path: Path, *, limit: int = 12_000) -> str:
    """Read one PDF page for local evidence without consulting PDF date metadata."""

    if not path.is_file():
        return ""
    try:
        from pypdf import PdfReader

        text = PdfReader(str(path), strict=False).pages[0].extract_text() or ""
    except Exception:
        return ""
    return text[: max(1, int(limit))]


def _normalized_date(raw: str) -> str:
    value = " ".join(str(raw or "").replace("/", "-").split()).strip(" ,.;:")
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value, flags=re.I)
    if re.fullmatch(r"(?:18|19|20|21)\d{2}", value):
        return value
    for pattern in (
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
    ):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    match = _YEAR_RE.search(value)
    return match.group(0) if match else ""


def _year(value: Any) -> int | None:
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(0)) if match else None


def _publication_month(raw: str) -> str | None:
    normalized = _normalized_date(raw)
    return normalized[:7] if len(normalized) >= 7 else None


def _evidence_text(front: str, start: int, end: int) -> str:
    line_start = front.rfind("\n", 0, start) + 1
    line_end = front.find("\n", end)
    if line_end < 0:
        line_end = min(len(front), end + 160)
    return re.sub(r"\s+", " ", front[line_start:line_end]).strip()[:500]


def _extraction(
    *,
    year: int | None,
    date: str | None,
    source_text: str,
    source_location: str,
    date_type: str,
    confidence: float,
    source: str,
    method: str = "deterministic_rule",
) -> dict[str, Any]:
    reliable = bool(
        year
        and date_type in RELIABLE_PUBLICATION_DATE_TYPES
        and float(confidence) >= 0.88
        and source_text
    )
    return {
        "basic_info": {
            "publication_year": year,
            "publication_date": date,
        },
        "publication_evidence": {
            "source_text": source_text,
            "source_location": source_location,
            "date_type": date_type,
            "confidence": round(max(0.0, min(1.0, float(confidence))), 3),
            "source": source,
        },
        "method": method,
        "status": "reliable" if reliable else "insufficient",
        "network_required": not reliable,
    }


def extract_publication_evidence(
    text: str,
    filename: str = "",
    *,
    source_location: str = "mineru_markdown_front_matter",
) -> dict[str, Any]:
    """Extract one best publication-date claim and its exact local evidence."""

    front = front_matter_text(text)
    online = _ONLINE_LABEL.search(front)
    if online:
        normalized = _normalized_date(online.group("date"))
        year = _year(normalized)
        if year:
            return _extraction(
                year=year,
                date=_publication_month(normalized),
                source_text=_evidence_text(front, online.start(), online.end()),
                source_location=source_location,
                date_type="published_online",
                confidence=0.94,
                source="labelled_online_publication_date",
            )

    for match in _ISSUED_LABEL.finditer(front):
        context = front[max(0, match.start() - 50) : match.end() + 30]
        if _EXCLUDED_DATE_CONTEXT.search(context):
            continue
        normalized = _normalized_date(match.group("date"))
        year = _year(normalized)
        if year:
            return _extraction(
                year=year,
                date=_publication_month(normalized),
                source_text=_evidence_text(front, match.start(), match.end()),
                source_location=source_location,
                date_type="published",
                confidence=0.9,
                source="labelled_publication_date",
            )

    header = _JOURNAL_HEADER_YEAR.search(front)
    if header:
        return _extraction(
            year=int(header.group("year")),
            date=None,
            source_text=_evidence_text(front, header.start(), header.end()),
            source_location=source_location,
            date_type="issue_date",
            confidence=0.96,
            source="journal_volume_issue_header",
        )

    copyright_match = _COPYRIGHT_YEAR.search(front)
    if copyright_match:
        return _extraction(
            year=int(copyright_match.group("year")),
            date=None,
            source_text=_evidence_text(front, copyright_match.start(), copyright_match.end()),
            source_location=source_location,
            date_type="copyright",
            confidence=0.7,
            source="copyright_year_candidate",
        )

    filename_match = _YEAR_RE.search(Path(str(filename or "")).stem)
    if filename_match:
        return _extraction(
            year=int(filename_match.group(0)),
            date=None,
            source_text=Path(str(filename or "")).name,
            source_location="filename",
            date_type="unknown",
            confidence=0.35,
            source="filename_year_candidate",
        )

    return _extraction(
        year=None,
        date=None,
        source_text="",
        source_location=source_location,
        date_type="unknown",
        confidence=0.0,
        source="not_found",
    )


def _normalized_evidence_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip().casefold()


def validate_model_publication_extraction(
    payload: Mapping[str, Any] | None,
    *,
    sources: Mapping[str, str],
) -> dict[str, Any]:
    """Validate model output against literal source text before trusting it."""

    payload = payload if isinstance(payload, Mapping) else {}
    basic = payload.get("basic_info")
    evidence = payload.get("publication_evidence")
    if not isinstance(basic, Mapping) or not isinstance(evidence, Mapping):
        return _extraction(
            year=None,
            date=None,
            source_text="",
            source_location="unknown",
            date_type="unknown",
            confidence=0.0,
            source="model_output_invalid",
            method="llm_validated_extraction",
        )

    raw_year = basic.get("publication_year")
    try:
        year = int(raw_year) if raw_year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not 1800 <= year <= datetime.now().year + 2:
        year = None

    raw_date = str(basic.get("publication_date") or "").strip()
    date = raw_date if _MONTH_VALUE.fullmatch(raw_date) else None
    if date and year is None:
        year = int(date[:4])
    if date and year != int(date[:4]):
        date = None

    date_type = str(evidence.get("date_type") or "unknown").strip().casefold()
    if date_type not in PUBLICATION_DATE_TYPES:
        date_type = "unknown"
    location = str(evidence.get("source_location") or "").strip()
    quote = re.sub(r"\s+", " ", str(evidence.get("source_text") or "")).strip()[:500]
    source = _normalized_evidence_text(sources.get(location, ""))
    quote_matches = bool(quote and source and _normalized_evidence_text(quote) in source)
    try:
        confidence = float(evidence.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not quote_matches:
        confidence = min(confidence, 0.35)
        date_type = "unknown"

    return _extraction(
        year=year,
        date=date,
        source_text=quote if quote_matches else "",
        source_location=location or "unknown",
        date_type=date_type,
        confidence=confidence,
        source="llm_source_grounded",
        method="llm_validated_extraction",
    )


def resolve_local_publication_extraction(
    *,
    markdown_text: str,
    pdf_first_page_text: str,
    filename: str = "",
    model_payload: Mapping[str, Any] | None = None,
    model_error: str = "",
) -> dict[str, Any]:
    """Resolve rule and model candidates into one local-document decision."""

    sources = {
        "mineru_markdown_front_matter": front_matter_text(markdown_text),
        "pdf_page_1": str(pdf_first_page_text or "")[:12_000],
    }
    candidates = [
        extract_publication_evidence(
            sources["pdf_page_1"], filename, source_location="pdf_page_1"
        ),
        extract_publication_evidence(
            sources["mineru_markdown_front_matter"],
            filename,
            source_location="mineru_markdown_front_matter",
        ),
    ]
    if model_payload is not None:
        candidates.append(validate_model_publication_extraction(model_payload, sources=sources))

    reliable = [row for row in candidates if row.get("status") == "reliable"]
    reliable_years = {
        int(row["basic_info"]["publication_year"])
        for row in reliable
        if row.get("basic_info", {}).get("publication_year")
    }
    if len(reliable_years) > 1:
        return {
            "basic_info": {"publication_year": None, "publication_date": None},
            "publication_evidence": {
                "source_text": "",
                "source_location": "multiple_local_sources",
                "date_type": "unknown",
                "confidence": 0.0,
                "source": "local_evidence_conflict",
            },
            "method": "rules+llm" if model_payload is not None else "rules",
            "status": "conflict",
            "network_required": True,
            "candidates": candidates,
            "model_error": str(model_error or "")[:500],
        }

    pool = reliable or candidates
    date_type_priority = {
        "published_online": 4,
        "early_view": 3,
        "issue_date": 2,
        "published": 1,
    }
    selected = max(
        pool,
        key=lambda row: (
            float(row.get("publication_evidence", {}).get("confidence") or 0.0),
            date_type_priority.get(
                str(row.get("publication_evidence", {}).get("date_type") or ""), 0
            ),
            bool(row.get("basic_info", {}).get("publication_date")),
        ),
    )
    resolved = {
        **selected,
        "method": "rules+llm" if model_payload is not None else "rules",
        "candidates": candidates,
        "model_error": str(model_error or "")[:500],
    }
    if reliable:
        selected_year = selected.get("basic_info", {}).get("publication_year")
        supporting = [
            row
            for row in reliable
            if row.get("basic_info", {}).get("publication_year") == selected_year
        ]
        if len(supporting) >= 2:
            resolved["publication_evidence"] = {
                **dict(selected.get("publication_evidence") or {}),
                "confidence": max(
                    0.97,
                    float(selected.get("publication_evidence", {}).get("confidence") or 0.0),
                ),
                "supported_by": [str(row.get("method") or "") for row in supporting],
            }
        resolved["status"] = "reliable"
        resolved["network_required"] = False
    else:
        resolved["status"] = "insufficient"
        resolved["network_required"] = True
    return resolved


def extract_publication_metadata(
    text: str, filename: str = ""
) -> dict[str, dict[str, Any]]:
    """Compatibility fields derived from the single local-evidence contract."""

    extraction = extract_publication_evidence(text, filename)
    basic = extraction["basic_info"]
    evidence = extraction["publication_evidence"]
    year = basic.get("publication_year")
    date = basic.get("publication_date")
    reliable = extraction.get("status") == "reliable"
    date_type = str(evidence.get("date_type") or "unknown")
    status = (
        "online_first"
        if date_type in {"published_online", "early_view"}
        else "issue_assigned"
        if reliable
        else "unknown"
    )
    source = f"local_document:{evidence.get('source') or 'not_found'}"
    confidence = float(evidence.get("confidence") or 0.0)
    first_date = date or (str(year) if reliable and year else None)
    return {
        "first_publication_date": _field(
            first_date, source, confidence if reliable else 0.0
        ),
        "bibliographic_year": _field(year, source, confidence),
        "publication_status": _field(
            status,
            "publication_date_semantics"
            if status != "unknown"
            else "local_evidence_insufficient",
            confidence if reliable else 0.0,
        ),
        "year": _field(year, source, confidence),
    }


def extract_front_matter_doi(text: str) -> dict[str, Any]:
    """Return a DOI candidate only from article front matter, never references."""

    front = front_matter_text(text, limit=40_000)
    candidates: list[tuple[int, str, str]] = []
    for match in _DOI_RE.finditer(front):
        doi = match.group(0).rstrip(".,;)").casefold()
        context = front[max(0, match.start() - 80) : match.end() + 30]
        explicit = bool(
            re.search(r"(?:https?://(?:dx\.)?doi\.org/|\bdoi\s*:)", context, re.I)
        )
        excluded = bool(_EXCLUDED_DATE_CONTEXT.search(context))
        score = 2 if explicit else 1
        if excluded:
            score -= 1
        candidates.append(
            (
                score,
                doi,
                "front_matter_explicit_doi"
                if explicit
                else "front_matter_doi_candidate",
            )
        )
    if not candidates:
        return _field(None, "rule_not_found", 0.0)
    score, doi, source = max(candidates, key=lambda item: item[0])
    return _field(doi, source, 0.78 if score >= 2 else 0.62)
