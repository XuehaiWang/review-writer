"""Deterministic Draft citation identity and numbered-reference repair."""

from __future__ import annotations

import re
import json
from typing import Any

from .author_metadata import clean_author_names
from .metadata_fields import metadata_value
from .chemical_typography import normalize_chemical_typography
from .paragraph_markers import PARAGRAPH_MARKER_RE, parse_marked_paragraphs


PARAGRAPH_MARKER = PARAGRAPH_MARKER_RE
CALLOUT_RE = re.compile(r"\[((?:\d+\s*(?:[-–]\s*\d+)?\s*[,;]?\s*)+)\]")
REFERENCE_HEADING_RE = re.compile(r"(?mi)^##\s+References\s*$")
CITATION_MAP_RE = re.compile(r"<!--\s*citation_map:\s*(\{[^\n]*\})\s*-->")
REFERENCE_WEB_RESIDUE = re.compile(
    r"\b(?:Cite\s+This|Read\s+Online|Article\s+Recommendations?|Supporting\s+Information)\b.*$",
    re.I,
)


def clean_reference_field(value: Any) -> str:
    text = " ".join(str(value or "").replace("\u00ad", "").split()).strip()
    text = REFERENCE_WEB_RESIDUE.sub("", text).strip(" .;,|")
    text = re.sub(r"\s*[★☆*]+\s*", " ", text)
    return " ".join(text.split()).strip(" .;,|")


def clean_reference_doi(value: Any) -> str:
    text = clean_reference_field(value)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    return text.rstrip(".,;)")


def reference_text(
    row: dict[str, Any],
    *,
    fallback: str = "",
) -> str:
    """Render one canonical bibliography row without provider-page residue.

    Keep the renderer deliberately style-neutral: downstream DOCX/PDF profiles
    may restyle punctuation, but they must receive the complete canonical
    identity (journal, year, volume/issue, pages, and DOI) in one stable order.
    """

    raw_authors = metadata_value(row, "authors")
    authors = ", ".join(clean_author_names(raw_authors))
    journal = clean_reference_field(metadata_value(row, "journal"))
    year = clean_reference_field(
        metadata_value(row, "bibliographic_year")
        or metadata_value(row, "year")
    )
    volume = clean_reference_field(metadata_value(row, "volume"))
    issue = clean_reference_field(
        metadata_value(row, "issue") or metadata_value(row, "number")
    )
    pages = clean_reference_field(
        metadata_value(row, "pages")
        or metadata_value(row, "page")
        or metadata_value(row, "article_number")
    )
    publication = journal
    if year:
        publication = f"{publication} {year}".strip()
    if volume:
        publication = f"{publication}, {volume}".strip(" ,")
    if issue:
        publication = f"{publication}({issue})".strip()
    if pages:
        publication = f"{publication}, {pages}".strip(" ,")
    doi = clean_reference_doi(metadata_value(row, "doi"))
    if doi:
        doi = f"https://doi.org/{doi}"
    parts = [
        authors,
        clean_reference_field(metadata_value(row, "title")),
        publication,
        doi,
    ]
    rendered = ". ".join(part.rstrip(".") for part in parts if part)
    return rendered or clean_reference_field(fallback) or "Unresolved paper"


def expand_callouts(value: str) -> list[int]:
    """Expand one numeric citation group while retaining first-seen order."""

    result: list[int] = []
    for part in re.split(r"\s*[,;]\s*", str(value or "")):
        part = part.strip()
        if not part:
            continue
        range_match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if range_match:
            left, right = int(range_match.group(1)), int(range_match.group(2))
            values = range(left, right + 1) if right >= left else (left, right)
        elif part.isdigit():
            values = (int(part),)
        else:
            continue
        for number in values:
            if number not in result:
                result.append(number)
    return result


def format_citation_group(numbers) -> str:
    """Use numeric ordering at every rendering boundary, independent of Paper ID order."""
    values = sorted({int(number) for number in numbers if str(number).isdigit() and int(number) > 0})
    return "[" + ", ".join(map(str, values)) + "]" if values else ""


def citation_map_comment(paper_to_number: dict[str, int]) -> str:
    mapping = {str(number): paper for paper, number in sorted(paper_to_number.items(), key=lambda pair: pair[1])}
    return "<!-- citation_map: " + json.dumps(mapping, ensure_ascii=True, separators=(",", ":")) + " -->"


def _embedded_citation_map(markdown: str) -> dict[int, str]:
    match = CITATION_MAP_RE.search(markdown)
    if not match:
        return {}
    try:
        value = json.loads(match[1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {int(number): paper for number, paper in value.items()
            if str(number).isdigit() and int(number) > 0 and isinstance(paper, str) and paper}


def ordered_callouts(markdown: str) -> list[int]:
    result: list[int] = []
    for match in CALLOUT_RE.finditer(str(markdown or "")):
        for number in expand_callouts(match.group(1)):
            if number not in result:
                result.append(number)
    return result


def _paragraph_text_by_id(markdown: str) -> dict[str, str]:
    return {row["paragraph_id"]: row["text"] for row in parse_marked_paragraphs(markdown)}


def _structured_paragraph_identities(
    section_index: dict[str, Any],
) -> list[dict[str, Any]]:
    """Read paragraph-to-paper identity without inferring it from numbers.

    Claim-level citation groups are the primary source of truth.  The flattened
    paragraph list remains a compatibility fallback for older section indexes.
    """

    rows: list[dict[str, Any]] = []
    for section in section_index.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, dict):
                continue
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            if not paragraph_id:
                continue
            claim_groups: list[list[str]] = []
            for realization in paragraph.get("claim_realizations") or []:
                if not isinstance(realization, dict):
                    continue
                group = list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in realization.get("citation_group") or []
                        if str(value or "").strip()
                    )
                )
                if group:
                    claim_groups.append(group)
            paper_ids = list(
                dict.fromkeys(
                    value
                    for group in claim_groups
                    for value in group
                )
            )
            for value in (
                paragraph.get("cited_paper_ids")
                or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
            ):
                paper_id = str(value or "").strip()
                if paper_id and paper_id not in paper_ids:
                    paper_ids.append(paper_id)
            if paper_ids:
                rows.append(
                    {
                        "paragraph_id": paragraph_id,
                        "paper_ids": paper_ids,
                        "claim_groups": claim_groups,
                        "claim_realizations": [dict(item) for item in paragraph.get("claim_realizations") or [] if isinstance(item, dict)],
                    }
                )
    return rows


def strip_numeric_callouts(value: str) -> str:
    """Remove rendered numeric citations while preserving surrounding prose."""

    text = CALLOUT_RE.sub("", str(value or ""))
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _claim_citation_spans(text: str, identity: dict[str, Any]) -> list[tuple[int, int, list[str]]]:
    """Locate unchanged claim realizations without replacing edited prose."""
    spans = []
    cursor = 0
    for claim in identity.get("claim_realizations") or []:
        value = normalize_chemical_typography(strip_numeric_callouts(str(claim.get("text") or "")))
        papers = list(dict.fromkeys(str(item) for item in claim.get("citation_group") or [] if item))
        if not value or not papers:
            return []
        pattern = r"\s+".join(re.escape(word) for word in value.split())
        match = re.search(pattern, text[cursor:])
        if not match:
            return []
        start, end = cursor + match.start(), cursor + match.end()
        if text[cursor:start].strip():
            return []
        spans.append((start, end, papers))
        cursor = end
    tail = text[cursor:].strip()
    # Draft assembly may append figure explanations outside the claim plan.
    if tail and not re.fullmatch(r"(?:Figure \d+ [^.]+\.\s*)+", tail):
        return []
    return spans


def _rerender_structured_citations(
    markdown: str,
    paragraph_identities: list[dict[str, Any]],
    matrix_rows: dict[str, dict[str, Any]],
    known_numbers: dict[int, str],
) -> tuple[str, dict[str, Any]]:
    """Preserve claim locations; never replace a paragraph with its old source prose."""
    identities = {str(row.get("paragraph_id") or ""): row for row in paragraph_identities}
    replacements = []
    needs_revalidation = []
    # Temporary source markers let prose and table citations share first-use numbering.
    def source_marker(papers):
        return "\x00CITE:" + json.dumps(list(dict.fromkeys(papers)), separators=(",", ":")) + "\x00"

    for paragraph in parse_marked_paragraphs(markdown):
        pid = str(paragraph["paragraph_id"])
        identity = identities.get(pid)
        if identity is None:
            continue
        clean = normalize_chemical_typography(strip_numeric_callouts(paragraph["text"]))
        spans = _claim_citation_spans(clean, identity)
        if spans:
            for _start, end, papers in reversed(spans):
                clean = clean[:end] + " " + source_marker(papers) + clean[end:]
            replacements.append((paragraph["start"], paragraph["end"], clean))
        elif identity.get("claim_realizations"):
            # Renumber existing local callouts below, preserving manual/rewritten text.
            needs_revalidation.append(pid)

    body = markdown
    for start, end, replacement in reversed(replacements):
        body = body[:start] + replacement + body[end:]
    unresolved = []
    def bind_existing(match):
        numbers = expand_callouts(match[1])
        if any(number not in known_numbers for number in numbers):
            unresolved.extend(number for number in numbers if number not in known_numbers)
            return match[0]
        return source_marker([known_numbers[number] for number in numbers])
    body = CALLOUT_RE.sub(bind_existing, body)
    marker_pattern = re.compile(r"\x00CITE:(.*?)\x00")
    paper_order = list(dict.fromkeys(paper for match in marker_pattern.finditer(body) for paper in json.loads(match[1])))
    missing_papers = sorted(set(paper_order) - set(matrix_rows))
    if unresolved or missing_papers or not paper_order:
        return markdown, {"status": "not_applied", "unresolved_callouts": sorted(set(unresolved)),
                          "missing_paper_ids": missing_papers,
                          "paragraph_ids_needing_revalidation": needs_revalidation}
    paper_to_number = {paper: index for index, paper in enumerate(paper_order, 1)}
    body = marker_pattern.sub(lambda match: format_citation_group(paper_to_number[paper] for paper in json.loads(match[1])), body)
    return body, {"status": "applied", "paper_order": paper_order, "paper_to_number": paper_to_number,
                  "paragraph_ids_needing_revalidation": needs_revalidation,
                  "missing_paper_ids": [], "missing_paragraph_ids": []}


def citation_entries_from_draft(
    markdown: str,
    section_index: dict[str, Any],
) -> dict[str, Any]:
    """Recover callout-to-paper identity from paragraph metadata.

    Numeric callouts alone never authorize a paper.  A mapping is accepted only
    when the paragraph's structured ``cited_paper_ids`` aligns with the visible
    citation group in the same paragraph.
    """

    paragraph_text = _paragraph_text_by_id(markdown)
    embedded = _embedded_citation_map(markdown)
    mapped: dict[int, str] = dict(embedded)
    conflicts: list[dict[str, Any]] = []
    for section in section_index.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, dict):
                continue
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            text = paragraph_text.get(paragraph_id, "")
            paper_ids = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in (
                        paragraph.get("cited_paper_ids")
                        or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                    )
                    if str(value or "").strip()
                )
            )
            if not text or not paper_ids:
                continue
            if embedded:
                continue
            groups = [expand_callouts(match.group(1)) for match in CALLOUT_RE.finditer(text)]
            callouts = list(dict.fromkeys(number for group in groups for number in group))
            if len(callouts) != len(paper_ids):
                conflicts.append(
                    {
                        "paragraph_id": paragraph_id,
                        "callouts": callouts,
                        "paper_ids": paper_ids,
                        "reason": "citation_count_does_not_match_structured_papers",
                    }
                )
                continue
            for callout, paper_id in zip(callouts, paper_ids, strict=True):
                previous = mapped.get(callout)
                if previous and previous != paper_id:
                    conflicts.append(
                        {
                            "paragraph_id": paragraph_id,
                            "callout": callout,
                            "paper_ids": [previous, paper_id],
                            "reason": "callout_maps_to_multiple_papers",
                        }
                    )
                    continue
                mapped[callout] = paper_id
    used = ordered_callouts(REFERENCE_HEADING_RE.split(markdown, maxsplit=1)[0])
    return {
        "entries": [
            {"callout": number, "paper_id": mapped[number]}
            for number in used
            if number in mapped
        ],
        "unresolved_callouts": [number for number in used if number not in mapped],
        "conflicts": conflicts,
        "structured_paragraphs": _structured_paragraph_identities(section_index),
    }


def repair_numbered_references(
    markdown: str,
    citation_identity: dict[str, Any],
    matrix: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Renumber citations and rebuild References from stable Paper IDs."""

    identity = {
        int(item["callout"]): str(item["paper_id"])
        for item in citation_identity.get("entries") or []
        if isinstance(item, dict)
        and str(item.get("callout") or "").isdigit()
        and str(item.get("paper_id") or "").strip()
    }
    for conflict in citation_identity.get("conflicts") or []:
        if conflict.get("reason") == "callout_maps_to_multiple_papers":
            identity.pop(int(conflict["callout"]), None)
    heading = REFERENCE_HEADING_RE.search(str(markdown or ""))
    body = markdown[: heading.start()].rstrip() if heading else str(markdown or "").rstrip()
    used = ordered_callouts(body)
    unresolved = sorted(set(used) - set(identity))
    matrix_rows = {
        str(row.get("paper_id") or ""): row
        for row in (matrix.get("rows") or matrix.get("papers") or [])
        if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
    }
    missing_papers = sorted(
        {identity[number] for number in used if number in identity} - set(matrix_rows)
    )

    structured_paragraphs = [
        dict(row)
        for row in citation_identity.get("structured_paragraphs") or []
        if isinstance(row, dict) and str(row.get("paragraph_id") or "")
    ]
    structured: dict[str, Any] = {}
    if structured_paragraphs:
        structured_body, structured = _rerender_structured_citations(
            body,
            structured_paragraphs,
            matrix_rows,
            identity,
        )
        if structured.get("status") == "applied":
            paper_order = list(structured.get("paper_order") or [])
            paper_to_new = dict(structured.get("paper_to_number") or {})
            references = ["## References"]
            for paper_id in paper_order:
                number = int(paper_to_new[paper_id])
                references.append(
                    f"[{number}] {reference_text(matrix_rows[paper_id], fallback=paper_id)}"
                )
            structured_body = CITATION_MAP_RE.sub("", structured_body).rstrip()
            repaired = structured_body + "\n\n" + citation_map_comment(paper_to_new) + "\n\n" + "\n".join(references) + "\n"
            return repaired, {
                "status": "applied",
                "changed": repaired != markdown,
                "mode": "structured_claim_identity",
                "paragraph_ids_needing_revalidation": structured.get("paragraph_ids_needing_revalidation", []),
                "entries": [
                    {"callout": int(paper_to_new[paper_id]), "paper_id": paper_id}
                    for paper_id in paper_order
                ],
                "unresolved_callouts": [],
                "resolved_legacy_callouts": unresolved,
                "missing_paper_ids": [],
                "conflicts": [],
                "resolved_legacy_conflicts": list(
                    citation_identity.get("conflicts") or []
                ),
            }
        missing_papers = list(
            dict.fromkeys(
                [*missing_papers, *(structured.get("missing_paper_ids") or [])]
            )
        )
    if not used or unresolved or missing_papers:
        return markdown, {
            "status": "not_applied",
            "changed": False,
            "unresolved_callouts": unresolved,
            "missing_paper_ids": missing_papers,
            "conflicts": list(citation_identity.get("conflicts") or []),
            "entries": list(citation_identity.get("entries") or []),
            "paragraph_ids_needing_revalidation": structured.get("paragraph_ids_needing_revalidation", []),
        }

    paper_order: list[str] = []
    for number in used:
        paper_id = identity[number]
        if paper_id not in paper_order:
            paper_order.append(paper_id)
    paper_to_new = {paper_id: index for index, paper_id in enumerate(paper_order, 1)}
    old_to_new = {number: paper_to_new[identity[number]] for number in used}

    def replace_group(match: re.Match[str]) -> str:
        values = []
        for old in expand_callouts(match.group(1)):
            new = old_to_new.get(old)
            if new is not None and new not in values:
                values.append(new)
        return format_citation_group(values) or match.group(0)

    repaired_body = CALLOUT_RE.sub(replace_group, CITATION_MAP_RE.sub("", body)).rstrip()
    references = ["## References"]
    for paper_id in paper_order:
        number = paper_to_new[paper_id]
        references.append(
            f"[{number}] {reference_text(matrix_rows[paper_id], fallback=paper_id)}"
        )
    repaired = repaired_body + "\n\n" + citation_map_comment(paper_to_new) + "\n\n" + "\n".join(references) + "\n"
    return repaired, {
        "status": "applied",
        "changed": repaired != markdown,
        "old_to_new": {str(key): value for key, value in old_to_new.items()},
        "entries": [
            {"callout": paper_to_new[paper_id], "paper_id": paper_id}
            for paper_id in paper_order
        ],
        "unresolved_callouts": [],
        "missing_paper_ids": [],
        "conflicts": list(citation_identity.get("conflicts") or []),
        "paragraph_ids_needing_revalidation": structured.get("paragraph_ids_needing_revalidation", []),
    }
