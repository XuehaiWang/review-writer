"""Deterministic bibliography recovery from MinerU front matter.

The Library ingest path must not depend on a public metadata service in order
to identify the paper that the user uploaded.  This module treats MinerU's
layout blocks and Markdown as the primary evidence, rejects common publisher
boilerplate, and returns provenance-wrapped fields that can be stored directly
in canonical Library metadata.

The bounded bibliography agent remains a fallback for fields that this module
cannot recover with adequate confidence.  It is not allowed to invent values:
all model results are validated against literal MinerU text elsewhere.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .author_metadata import authors_are_publication_ready, clean_author_names
from .document_front_matter import extract_markdown_title
from .paper_sources.normalize import normalize_doi, normalize_title
from .publication_metadata import extract_front_matter_doi, front_matter_text


_BOILERPLATE = re.compile(
    r"\b(?:received|revised|accepted|read online|cite this|article recommendations?|"
    r"supporting information|metrics\s*&\s*more|downloaded|accessed|copyright)\b",
    re.I,
)
_AFFILIATION = re.compile(
    r"\b(?:university|institute|institution|department|laboratory|school|college|"
    r"academy|faculty|hospital|centre|center|research institute|correspondence|"
    r"postal|road|street|avenue|china|usa|japan|germany|france|italy|india)\b",
    re.I,
)
_SECTION_LABEL = re.compile(
    r"^(?:article|letter|communication|abstract|keywords?|introduction|references?|"
    r"article info|a\s*r\s*t\s*i\s*c\s*l\s*e\s*i\s*n\s*f\s*o)$",
    re.I,
)
_INVALID_AUTHOR = re.compile(
    r"\b(?:vol(?:ume)?\.?|no\.?|issue|pp?\.?|pages?|doi|journal|publisher|"
    r"submitted|checked|edited|reviewed)\b",
    re.I,
)
_CITE_LINE = re.compile(
    r"(?im)^\s*(?:cite\s+this\s*:\s*)?"
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’\- ]{2,100}?)\s+"
    r"(?P<year>(?:18|19|20|21)\d{2})\s*,\s*"
    r"(?P<volume>\d{1,5})"
    r"(?:\s*\(\s*(?P<issue>[A-Za-z0-9\-]+)\s*\))?\s*,\s*"
    r"(?P<locator>[A-Za-z]?[0-9][A-Za-z0-9.\-−–]{1,40})\s*$"
)
_JOURNAL_HEADER = re.compile(
    r"(?im)^\s*(?P<journal>[A-Z][A-Za-z0-9&.'’\- ]{2,100}?)\s+"
    r"(?P<year>(?:18|19|20|21)\d{2})\s*,\s*"
    r"(?:vol(?:ume)?\.?\s*)?(?P<volume>\d{1,5})\s*,\s*"
    r"(?:no\.?|issue)\s*(?P<issue>[A-Za-z0-9\-]+)\b"
)
_PDF_JOURNAL_VOLUME_YEAR = re.compile(
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,120}?)\s+"
    r"(?P<volume>\d{1,5})\s*\(\s*(?P<year>(?:18|19|20|21)\d{2})\s*\)\s*"
    r"(?P<locator>[A-Za-z]?\d[A-Za-z0-9.\-−–]{1,40})",
)
_PDF_CITE_THIS_HEADER = re.compile(
    r"^cite\s+this\s*:\s*"
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,100}?)\s*,\s*"
    r"(?P<year>(?:18|19|20|21)\d{2})\s*,\s*"
    r"(?P<volume>\d{1,5})\s*,?",
    re.I,
)
_PDF_JOURNAL_YEAR_VOLUME = re.compile(
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,120}?)\s*,?\s*"
    r"(?P<year>(?:18|19|20|21)\d{2})\s*,\s*"
    r"(?:vol(?:ume)?\.?\s*)?(?P<volume>\d{1,5})"
    r"(?:\s*\(\s*(?P<issue>[A-Za-z0-9\-]+)\s*\))?\s*,\s*"
    r"(?P<locator>[A-Za-z]?\d[A-Za-z0-9.\-−–]{1,40})",
    re.I,
)
_PDF_JOURNAL_YEAR_LOCATOR = re.compile(
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,120}?)\s*,\s*"
    r"(?P<year>(?:18|19|20|21)\d{2})\s*,\s*"
    r"(?P<locator>(?:[A-Za-z]?\d{5,}|\d{1,6}\s*[\-−–]\s*\d{1,6}))",
    re.I,
)
_PDF_JOURNAL_ISSUE_PAGES_YEAR = re.compile(
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,100}?)\s+"
    r"No\.?\s*(?P<issue>[A-Za-z0-9\-]+)\s*,\s*"
    r"p{1,2}\.?\s*,?\s*(?P<locator>\d{1,6}\s*[\-−–]\s*\d{1,6})\s*,\s*"
    r"(?P<year>(?:18|19|20|21)\d{2})",
    re.I,
)
_PDF_JOURNAL_VOLUME_ISSUE_YEAR = re.compile(
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,100}?)\s+"
    r"Vol\.?\s*(?P<volume>\d{1,5})\s*,?\s*"
    r"(?:No\.?\s*(?P<issue>[A-Za-z0-9\-]+)\s*,?\s*)?"
    r"(?:[A-Za-z]+\s+)?(?P<year>(?:18|19|20|21)\d{2})",
    re.I,
)
_PDF_VOLUME_ISSUE_YEAR_JOURNAL = re.compile(
    r"Vol\.?\s*(?P<volume>\d{1,5})\s*,?\s*"
    r"(?:No\.?\s*(?P<issue>[A-Za-z0-9\-]+)\s*,?\s*)?"
    r"(?:[A-Za-z]+\s+)?(?P<year>(?:18|19|20|21)\d{2})\s+"
    r"(?P<locator>\d{1,6}\s*[\-−–]\s*\d{1,6})\s+"
    r"(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,100}?)(?=\s+\d|$)",
    re.I,
)
_PDF_PIPE_VOLUME_ARTICLE = re.compile(
    r"^(?P<journal>[A-Z][A-Za-z0-9&.'’:/()\- ]{2,120}?)\s*\|\s*"
    r"(?P<volume>\d{1,5})\s*:\s*(?P<locator>[A-Za-z]?\d{3,})\s*\|\s*"
    r"(?:doi\s*:?\s*|https?://(?:dx\.)?doi\.org/)"
    r"(?P<doi>10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.I,
)
_ARTICLE_NUMBER = re.compile(r"^(?:e\d{4,}|\d{5,}|[A-Za-z]\d{5,})$", re.I)


def _clean(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\$\^\{[^}]*\}\$|\^\{[^}]*\}", " ", text)
    text = text.replace("\\*", " ").replace("*", " ").replace("\u00ad", "")
    text = re.sub(r"[†‡§¶]+", " ", text)
    return " ".join(text.split()).strip(" ,;|")


def _field(
    value: Any,
    *,
    source: str,
    confidence: float,
    source_text: str = "",
    source_location: str = "",
    block_id: str = "",
    page_idx: int | None = None,
) -> dict[str, Any]:
    present = value not in (None, "", [])
    confidence = max(0.0, min(1.0, float(confidence))) if present else 0.0
    if not present:
        status = "missing"
    elif confidence >= 0.88:
        status = "confirmed"
    else:
        status = "uncertain"
    evidence = {
        "source_text": _clean(source_text)[:800],
        "source_location": source_location,
        "block_id": block_id,
        "page_idx": page_idx,
    }
    return {
        "value": value,
        "source": source,
        "confidence": round(confidence, 3),
        "human_checked": False,
        "verification_status": status,
        "evidence": {key: item for key, item in evidence.items() if item not in ("", None)},
    }


def _front_blocks(blocks: Iterable[Mapping[str, Any]], *, max_page: int = 1) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(blocks):
        if not isinstance(raw, Mapping):
            continue
        try:
            page_idx = int(raw.get("page_idx", 0))
        except (TypeError, ValueError):
            page_idx = 0
        if page_idx > max_page:
            continue
        text = _clean(raw.get("text") or raw.get("content") or "")
        if not text:
            continue
        rows.append(
            {
                "block_id": f"mineru-block-{index}",
                "page_idx": page_idx,
                "text": text,
                "text_level": raw.get("text_level"),
                "bbox": raw.get("bbox"),
            }
        )
    rows.sort(
        key=lambda row: (
            int(row["page_idx"]),
            (row.get("bbox") or [0, 0])[1]
            if isinstance(row.get("bbox"), list) and len(row.get("bbox") or []) >= 2
            else 0,
        )
    )
    return rows


def _title_field(rows: list[dict[str, Any]], markdown: str, filename: str) -> dict[str, Any]:
    markdown_title = extract_markdown_title(markdown)
    if markdown_title is not None:
        value = _clean(markdown_title.get("value"))
        if value:
            for row in rows:
                if normalize_title(value) in normalize_title(row["text"]):
                    return _field(
                        value,
                        source="mineru_front_matter_title",
                        confidence=max(0.9, float(markdown_title.get("confidence") or 0.0)),
                        source_text=row["text"],
                        source_location="mineru_content_list",
                        block_id=row["block_id"],
                        page_idx=row["page_idx"],
                    )
            return _field(
                value,
                source=str(markdown_title.get("source") or "mineru_markdown_title"),
                confidence=max(0.88, float(markdown_title.get("confidence") or 0.0)),
                source_text=value,
                source_location="mineru_markdown_front_matter",
            )
    for row in rows[:8]:
        value = row["text"]
        if 16 <= len(value) <= 500 and not _BOILERPLATE.search(value) and not _SECTION_LABEL.fullmatch(value):
            return _field(
                value,
                source="mineru_first_page_title_block",
                confidence=0.88,
                source_text=value,
                source_location="mineru_content_list",
                block_id=row["block_id"],
                page_idx=row["page_idx"],
            )
    fallback = _clean(Path(str(filename or "paper")).stem.replace("_", " ").replace("-", " "))
    return _field(
        fallback or None,
        source="filename_fallback",
        confidence=0.3 if fallback else 0.0,
        source_text=fallback,
        source_location="filename",
    )


def _candidate_author_line(value: str) -> bool:
    if not 3 <= len(value) <= 900:
        return False
    if _BOILERPLATE.search(value) or _AFFILIATION.search(value) or _INVALID_AUTHOR.search(value):
        return False
    if _SECTION_LABEL.fullmatch(value):
        return False
    return bool(
        re.search(r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", value)
        and ("," in value or re.search(r"\s+and\s+", value, re.I))
    )


def _author_names(raw: str) -> list[str]:
    return clean_author_names(raw)


def _authors_field(rows: list[dict[str, Any]], title: str, markdown: str) -> dict[str, Any]:
    title_key = normalize_title(title)
    title_index = -1
    for index, row in enumerate(rows[:20]):
        row_key = normalize_title(row["text"])
        if title_key and (title_key in row_key or row_key in title_key):
            title_index = index
            break
    for row in rows[title_index + 1 : title_index + 8]:
        if _candidate_author_line(row["text"]):
            names = _author_names(row["text"])
            if names:
                return _field(
                    names,
                    source="mineru_first_page_byline",
                    confidence=0.94,
                    source_text=row["text"],
                    source_location="mineru_content_list",
                    block_id=row["block_id"],
                    page_idx=row["page_idx"],
                )

    lines = front_matter_text(markdown, limit=16_000).splitlines()
    heading_index = -1
    for index, raw in enumerate(lines):
        line = _clean(raw.lstrip("# "))
        if title_key and line and normalize_title(line) == title_key:
            heading_index = index
            break
    for raw in lines[heading_index + 1 : heading_index + 10]:
        line = _clean(raw)
        if not line or line.startswith("!"):
            continue
        if _candidate_author_line(line):
            names = _author_names(line)
            if names:
                return _field(
                    names,
                    source="mineru_markdown_byline",
                    confidence=0.94,
                    source_text=line,
                    source_location="mineru_markdown_front_matter",
                )
        if _AFFILIATION.search(line) or _BOILERPLATE.search(line):
            break
    return _field(None, source="mineru_not_found", confidence=0.0)


def _pdf_authors_field(pdf_first_page_text: str, title: str) -> dict[str, Any]:
    """Recover a byline omitted by MinerU using the same local PDF page only."""

    lines = [_clean(value) for value in str(pdf_first_page_text or "").splitlines()]
    lines = [value for value in lines if value]
    title_key = normalize_title(title)
    title_end = -1
    for index in range(min(40, len(lines))):
        for width in range(1, 5):
            window = " ".join(lines[index : index + width])
            window_key = normalize_title(window)
            if (
                title_key
                and len(window_key) >= 20
                and (title_key in window_key or window_key in title_key)
            ):
                title_end = index + width - 1
                break
        if title_end >= 0:
            break
    if title_end < 0:
        return _field(None, source="pdf_first_page_title_not_found", confidence=0.0)

    names: list[str] = []
    evidence: list[str] = []
    for line in lines[title_end + 1 : title_end + 22]:
        if (
            _AFFILIATION.search(line)
            or _BOILERPLATE.search(line)
            or "@" in line
            or re.search(r"\b(?:abstract|keywords?)\b", line, re.I)
        ):
            if names:
                break
            continue
        if len(line) > 180 or (names and line.rstrip().endswith((".", ":"))):
            if names:
                break
            continue
        line = re.sub(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b", "", line).strip()
        candidates = clean_author_names(line)
        candidates = [
            name
            for name in candidates
            if 1 < len(name.split()) <= 6
            and not re.search(r"\bet\s+al\b", name, re.I)
            and any(character.isupper() for character in name)
        ]
        if not candidates:
            if names:
                break
            continue
        names.extend(candidates)
        evidence.append(line)
    names = list(dict.fromkeys(names))
    if not names:
        return _field(None, source="pdf_first_page_byline_not_found", confidence=0.0)
    return _field(
        names,
        source="pdf_first_page_byline",
        confidence=0.96,
        source_text="; ".join(evidence),
        source_location="pdf_page_1",
        page_idx=0,
    )


def _citation_fields(markdown: str) -> dict[str, dict[str, Any]]:
    front = front_matter_text(markdown, limit=24_000)
    match = _CITE_LINE.search(front) or _JOURNAL_HEADER.search(front)
    if not match:
        return {}
    journal = _clean(match.group("journal"))
    if _BOILERPLATE.search(journal) or _INVALID_AUTHOR.fullmatch(journal):
        return {}
    evidence = _clean(match.group(0))
    fields = {
        "journal": _field(
            journal,
            source="mineru_citation_header",
            confidence=0.96,
            source_text=evidence,
            source_location="mineru_markdown_front_matter",
        ),
        "year": _field(
            int(match.group("year")),
            source="mineru_citation_header",
            confidence=0.96,
            source_text=evidence,
            source_location="mineru_markdown_front_matter",
        ),
        "bibliographic_year": _field(
            int(match.group("year")),
            source="mineru_citation_header",
            confidence=0.96,
            source_text=evidence,
            source_location="mineru_markdown_front_matter",
        ),
        "volume": _field(
            match.group("volume"),
            source="mineru_citation_header",
            confidence=0.96,
            source_text=evidence,
            source_location="mineru_markdown_front_matter",
        ),
    }
    issue = match.groupdict().get("issue")
    if issue:
        fields["issue"] = _field(
            issue,
            source="mineru_citation_header",
            confidence=0.94,
            source_text=evidence,
            source_location="mineru_markdown_front_matter",
        )
    locator = match.groupdict().get("locator")
    if locator:
        normalized = locator.replace("−", "-").replace("–", "-")
        key = "article_number" if _ARTICLE_NUMBER.fullmatch(normalized) else "pages"
        fields[key] = _field(
            normalized,
            source="mineru_citation_header",
            confidence=0.96,
            source_text=evidence,
            source_location="mineru_markdown_front_matter",
        )
    return fields


def _journal_candidate(value: str) -> str:
    """Clean a journal name captured from a PDF header or footer line."""

    candidate = _clean(value)
    candidate = re.sub(r"^cite\s+this\s*:\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"^.*?\bdoi\s*:\s*\S+\s+", "", candidate, flags=re.I)
    if "|" in candidate:
        candidate = candidate.rsplit("|", 1)[-1].strip()
    # ACS footers can prepend copyright/accounting text and page counters before
    # the actual abbreviated journal name (for example ``7212 9 J. AM...``).
    candidate = re.sub(r"^.*?\d{3,6}\s+\d{1,3}\s+(?=[A-Z])", "", candidate)
    candidate = re.sub(
        r"^.*?(?:American Chemical Society|The Royal Society of Chemistry|"
        r"Wiley-VCH(?: Verlag)? GmbH(?:\s*&\s*Co\.\s*KGaA)?(?:,\s*Weinheim)?|"
        r"Thieme\.\s*All rights reserved\.|Pergamon)\s*(?:\d{4}\s+)?",
        "",
        candidate,
        flags=re.I,
    )
    candidate = re.sub(r"^Weinheim\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"^DOI\s*:\s*\S+\s+", "", candidate, flags=re.I)
    candidate = re.sub(r"^[^A-Za-z]+", "", candidate).strip(" ,;|.-")
    candidate = re.sub(r"^atrahedron\b", "Tetrahedron", candidate, flags=re.I)
    candidate = re.sub(r"\s+([.,])", r"\1", candidate)
    if not 2 < len(candidate) <= 120:
        return ""
    if len(candidate.split()) > 14:
        return ""
    if _BOILERPLATE.search(candidate) or _AFFILIATION.search(candidate):
        return ""
    if re.search(r"\b(?:https?|www|doi|email|issn|contents?\s+lists?)\b", candidate, re.I):
        return ""
    if re.search(r"\bet\s+al\.?\b", candidate, re.I) or ";" in candidate:
        return ""
    if _SECTION_LABEL.fullmatch(candidate) or not re.search(r"[A-Za-z]{2}", candidate):
        return ""
    return candidate


def _pdf_match_fields(
    match: re.Match[str],
    evidence: str,
    *,
    source_location: str = "pdf_page_1",
) -> dict[str, dict[str, Any]]:
    journal = _journal_candidate(match.group("journal"))
    if not journal:
        return {}
    groups = match.groupdict()
    source = "pdf_first_page_citation_header"
    common = {
        "source": source,
        "source_text": evidence,
        "source_location": source_location,
        "page_idx": 0,
    }
    fields: dict[str, dict[str, Any]] = {
        "journal": _field(journal, confidence=0.96, **common),
    }
    year = groups.get("year")
    if year:
        fields["year"] = _field(int(year), confidence=0.96, **common)
        fields["bibliographic_year"] = _field(int(year), confidence=0.96, **common)
    volume = groups.get("volume")
    if volume:
        fields["volume"] = _field(volume, confidence=0.96, **common)
    issue = groups.get("issue")
    if issue:
        fields["issue"] = _field(issue, confidence=0.94, **common)
    locator = groups.get("locator")
    if locator:
        normalized = re.sub(r"\s*[−–]\s*", "-", locator)
        key = "article_number" if _ARTICLE_NUMBER.fullmatch(normalized) else "pages"
        fields[key] = _field(normalized, confidence=0.96, **common)
    return fields


def _pdf_homepage_journal(
    lines: list[str],
    *,
    source_location: str = "pdf_page_1",
) -> dict[str, dict[str, Any]]:
    for index, line in enumerate(lines[:40]):
        if not (
            re.search(r"journal\s+homepage\s*:", line, re.I)
            or (
                re.search(r"journal\s+homepage", line, re.I)
                and re.search(r"https?://|www\.", line, re.I)
            )
        ):
            continue
        for candidate_line in reversed(lines[max(0, index - 4) : index]):
            if re.search(r"contents?\s+lists?|available\s+at|sciencedirect", candidate_line, re.I):
                continue
            journal = _journal_candidate(candidate_line)
            if journal:
                return {
                    "journal": _field(
                        journal,
                        source="pdf_first_page_journal_homepage",
                        confidence=0.97,
                        source_text=f"{candidate_line} | {line}",
                        source_location=source_location,
                        page_idx=0,
                    )
                }
    return {}


def _pdf_pipe_footer_fields(
    lines: list[str],
    *,
    source_location: str = "pdf_page_1",
) -> dict[str, dict[str, Any]]:
    """Recover pipe-delimited publisher footers and an adjacent copyright year."""

    for index, line in enumerate(lines):
        match = _PDF_PIPE_VOLUME_ARTICLE.search(line)
        if not match:
            continue
        journal = _journal_candidate(match.group("journal"))
        doi = normalize_doi(match.group("doi"))
        if not journal or not doi:
            continue

        nearby_lines = lines[max(0, index - 2) : index + 3]
        year_match: re.Match[str] | None = None
        year_evidence = ""
        for nearby in nearby_lines:
            if not re.search(
                r"(?:©|&\s*(?:18|19|20|21)\d{2}|copyright|all rights reserved|"
                r"published|publishers?\b)",
                nearby,
                re.I,
            ):
                continue
            year_match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", nearby)
            if year_match:
                year_evidence = nearby
                break

        evidence = " | ".join(
            dict.fromkeys([line, year_evidence] if year_evidence else [line])
        )
        common = {
            "source": "pdf_first_page_pipe_footer",
            "source_text": evidence,
            "source_location": source_location,
            "page_idx": 0,
        }
        fields: dict[str, dict[str, Any]] = {
            "journal": _field(journal, confidence=0.98, **common),
            "volume": _field(match.group("volume"), confidence=0.98, **common),
            "article_number": _field(
                match.group("locator"), confidence=0.98, **common
            ),
            "doi": _field(doi, confidence=0.99, **common),
        }
        if year_match:
            year = int(year_match.group(0))
            fields["year"] = _field(year, confidence=0.96, **common)
            fields["bibliographic_year"] = _field(year, confidence=0.96, **common)
        return fields
    return {}


def _pdf_coordinate_split_header(
    lines: list[str],
) -> dict[str, dict[str, Any]]:
    """Recover a citation whose columns were merged into separate top lines."""

    journal_lines: list[str] = []
    for line in lines[:4]:
        if re.fullmatch(
            r"(?:RESEARCH\s+)?(?:ARTICLE|COMMUNICATIONS?|REVIEW)",
            line,
            re.I,
        ):
            break
        if not re.fullmatch(r"[A-Z][A-Z&.'’: \-]{2,80}", line):
            break
        journal_lines.append(line)
    journal = _journal_candidate(" ".join(journal_lines))
    if not journal:
        return {}

    window_lines = lines[:10]
    window = " | ".join(window_lines)
    year = re.search(r"(?:18|19|20|21)\d{2}", window)
    volume = re.search(
        r"\bVol\.?\s*(\d{1,5})(?:\s*,?\s*No\.?\s*([A-Za-z0-9\-]+))?",
        window,
        re.I,
    )
    locator = re.search(r"\b\d{1,6}\s*[\-−–]\s*\d{1,6}\b", window)
    if not year or not volume or not locator:
        return {}
    common = {
        "source": "pdf_first_page_split_citation_header",
        "source_text": window,
        "source_location": "pdf_page_1_header",
        "page_idx": 0,
    }
    pages = re.sub(r"\s*[−–]\s*", "-", locator.group(0))
    return {
        "journal": _field(journal, confidence=0.95, **common),
        "year": _field(int(year.group(0)), confidence=0.95, **common),
        "bibliographic_year": _field(int(year.group(0)), confidence=0.95, **common),
        "volume": _field(volume.group(1), confidence=0.95, **common),
        **(
            {"issue": _field(volume.group(2), confidence=0.93, **common)}
            if volume.group(2)
            else {}
        ),
        "pages": _field(pages, confidence=0.95, **common),
    }


def _pdf_citation_fields(pdf_first_page_text: str) -> dict[str, dict[str, Any]]:
    """Recover journal metadata from local PDF page-one headers and footers.

    MinerU intentionally focuses on article content and often drops publisher
    furniture.  PDF text is therefore used only in bounded, citation-shaped
    locations: the first 28 lines, the last 14 lines, and an explicit journal
    homepage marker.  This avoids treating bibliography entries as the paper's
    own publication metadata.
    """

    full_lines = [_clean(value) for value in str(pdf_first_page_text or "").splitlines()]
    full_lines = [value for value in full_lines if value]
    coordinate_regions = (
        ("pdf_page_1_header", str(getattr(pdf_first_page_text, "header_text", "") or "")),
        ("pdf_page_1_footer", str(getattr(pdf_first_page_text, "footer_text", "") or "")),
    )
    header_regional_lines = [
        line
        for raw in coordinate_regions[0][1].splitlines()
        if (line := _clean(raw))
    ]
    footer_regional_lines = [
        line
        for raw in coordinate_regions[1][1].splitlines()
        if (line := _clean(raw))
    ]
    regional_lines = [
        *(("pdf_page_1_header", line) for line in header_regional_lines),
        *(("pdf_page_1_footer", line) for line in reversed(footer_regional_lines)),
    ]
    if regional_lines:
        candidate_lines = list(dict.fromkeys(regional_lines))
    else:
        indexes = list(range(min(12, len(full_lines))))
        indexes.extend(range(max(0, len(full_lines) - 8), len(full_lines)))
        candidate_lines = [
            ("pdf_page_1", full_lines[index])
            for index in dict.fromkeys(indexes)
        ]
    lines = [line for _, line in candidate_lines]
    if not lines:
        return {}

    patterns = (
        _PDF_CITE_THIS_HEADER,
        _PDF_JOURNAL_VOLUME_YEAR,
        _PDF_JOURNAL_YEAR_VOLUME,
        _PDF_JOURNAL_YEAR_LOCATOR,
        _PDF_JOURNAL_ISSUE_PAGES_YEAR,
        _PDF_JOURNAL_VOLUME_ISSUE_YEAR,
        _PDF_VOLUME_ISSUE_YEAR_JOURNAL,
    )
    if header_regional_lines:
        split_header = _pdf_coordinate_split_header(header_regional_lines)
        if split_header:
            return split_header
    pipe_footer = _pdf_pipe_footer_fields(
        footer_regional_lines if regional_lines else full_lines[-8:],
        source_location=("pdf_page_1_footer" if regional_lines else "pdf_page_1"),
    )
    if pipe_footer:
        return pipe_footer
    for source_location, line in candidate_lines:
        if ";" in line and not re.search(r"cite\s+this", line, re.I):
            continue
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                fields = _pdf_match_fields(
                    match,
                    line,
                    source_location=source_location,
                )
                if fields:
                    return fields

    header_lines = [
        line
        for source_location, line in candidate_lines
        if source_location != "pdf_page_1_footer"
    ]
    homepage = _pdf_homepage_journal(
        header_lines,
        source_location=(
            "pdf_page_1_header"
            if regional_lines
            else "pdf_page_1"
        ),
    )
    if homepage:
        return homepage

    # Some ACS PDFs split a footer into a two-line all-caps journal title,
    # followed by a year, volume/issue and page range on separate lines.
    for index, line in enumerate(lines):
        if not re.fullmatch(r"(?:18|19|20|21)\d{2}", line):
            continue
        following = lines[index + 1 : index + 4]
        volume_match = next(
            (
                match
                for item in following
                if (
                    match := re.search(
                        r"\bVol\.?\s*(\d{1,5})(?:\s*,?\s*No\.?\s*([A-Za-z0-9\-]+))?",
                        item,
                        re.I,
                    )
                )
            ),
            None,
        )
        locator_match = next(
            (
                match
                for item in following
                if (match := re.fullmatch(r"\d{1,6}\s*[\-−–]\s*\d{1,6}", item))
            ),
            None,
        )
        if not volume_match or not locator_match:
            continue
        title_lines: list[str] = []
        for candidate_line in reversed(lines[max(0, index - 3) : index]):
            if not re.fullmatch(r"[A-Z][A-Z&.'’\- ]{2,60}", candidate_line):
                break
            title_lines.insert(0, candidate_line)
        journal = _journal_candidate(" ".join(title_lines))
        if not journal:
            continue
        evidence = " | ".join(title_lines + [line, *following])
        common = {
            "source": "pdf_first_page_split_citation_header",
            "source_text": evidence,
            "source_location": (
                "pdf_page_1_footer" if regional_lines else "pdf_page_1"
            ),
            "page_idx": 0,
        }
        normalized_locator = re.sub(r"\s*[−–]\s*", "-", locator_match.group(0))
        return {
            "journal": _field(journal, confidence=0.94, **common),
            "year": _field(int(line), confidence=0.94, **common),
            "bibliographic_year": _field(int(line), confidence=0.94, **common),
            "volume": _field(volume_match.group(1), confidence=0.94, **common),
            **(
                {"issue": _field(volume_match.group(2), confidence=0.92, **common)}
                if volume_match.group(2)
                else {}
            ),
            "pages": _field(normalized_locator, confidence=0.94, **common),
        }
    return {}


def extract_mineru_bibliography(
    blocks: Iterable[Mapping[str, Any]],
    markdown: str,
    *,
    filename: str = "",
    pdf_first_page_text: str = "",
) -> dict[str, Any]:
    """Recover canonical bibliographic fields from bounded MinerU evidence."""

    rows = _front_blocks(blocks)
    title = _title_field(rows, markdown, filename)
    authors = _authors_field(rows, str(title.get("value") or ""), markdown)
    if not authors_are_publication_ready(authors.get("value")):
        authors = _pdf_authors_field(
            pdf_first_page_text,
            str(title.get("value") or ""),
        )
    # Markdown evidence remains authoritative when both representations contain
    # the same field; the local PDF page fills publisher furniture MinerU lost.
    fields: dict[str, dict[str, Any]] = {
        "title": title,
        "authors": authors,
        **_pdf_citation_fields(pdf_first_page_text),
        **_citation_fields(markdown),
    }
    doi = extract_front_matter_doi(markdown)
    raw_doi = normalize_doi(doi.get("value")) if isinstance(doi, Mapping) else ""
    if raw_doi:
        explicit = str(doi.get("source") or "") == "front_matter_explicit_doi"
        fields["doi"] = _field(
            raw_doi,
            source="mineru_front_matter_doi",
            confidence=0.94 if explicit else 0.76,
            source_text=raw_doi,
            source_location="mineru_markdown_front_matter",
        )
    statuses = {
        key: str(value.get("verification_status") or "missing")
        for key, value in fields.items()
        if isinstance(value, Mapping)
    }
    return {
        "schema_version": 1,
        "method": "mineru_front_matter_rules",
        "fields": fields,
        "field_statuses": statuses,
        "needs_agent_fields": sorted(
            key
            for key in ("title", "authors", "journal", "year", "volume", "pages_or_article_number", "doi")
            if (
                key == "pages_or_article_number"
                and not any(fields.get(item, {}).get("value") for item in ("pages", "article_number"))
            )
            or (
                key != "pages_or_article_number"
                and (
                    fields.get(key, {}).get("value") in (None, "", [])
                    or float(fields.get(key, {}).get("confidence") or 0.0) < 0.88
                )
            )
        ),
        "front_block_count": len(rows),
    }


def as_document_audit_extraction(result: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt confirmed deterministic fields to the bibliography-audit contract."""

    roles = {
        "title": "article_title",
        "authors": "article_authors",
        "journal": "journal",
        "year": "publication_year",
        "volume": "volume",
        "issue": "issue",
        "pages": "pages",
        "article_number": "article_number",
        "doi": "doi",
    }
    accepted: dict[str, dict[str, Any]] = {}
    raw_fields = result.get("fields") if isinstance(result, Mapping) else {}
    for key, role in roles.items():
        raw = raw_fields.get(key) if isinstance(raw_fields, Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("verification_status") or "") != "confirmed":
            continue
        evidence = raw.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        source_text = str(evidence.get("source_text") or "").strip()
        value = raw.get("value")
        if value in (None, "", []) or not source_text:
            continue
        accepted[key] = {
            "value": value,
            "role": role,
            "source_excerpt": source_text,
            "source_location": str(
                evidence.get("source_location") or "mineru_markdown_front_matter"
            ),
            "block_id": str(evidence.get("block_id") or ""),
            "page_idx": evidence.get("page_idx"),
            "confidence": float(raw.get("confidence") or 0.0),
            "verification_status": "verified",
        }
    return {
        "schema_version": 1,
        "status": "reliable" if accepted else "insufficient",
        "method": "mineru_deterministic_bibliography",
        "fields": accepted,
        "model_attempted": False,
        "model_error": "",
    }
