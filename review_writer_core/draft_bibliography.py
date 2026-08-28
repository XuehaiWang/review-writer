"""Deterministic Draft citation identity and numbered-reference repair."""

from __future__ import annotations

import re
from typing import Any


PARAGRAPH_MARKER = re.compile(
    r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->"
)
CALLOUT_RE = re.compile(r"\[((?:\d+\s*(?:[-–]\s*\d+)?\s*[,;]?\s*)+)\]")
REFERENCE_HEADING_RE = re.compile(r"(?mi)^##\s+References\s*$")
REFERENCE_WEB_RESIDUE = re.compile(
    r"\b(?:Cite\s+This|Read\s+Online|Article\s+Recommendations?|Supporting\s+Information)\b.*$",
    re.I,
)


def _clean_reference_field(value: Any) -> str:
    text = " ".join(str(value or "").replace("\u00ad", "").split()).strip()
    text = REFERENCE_WEB_RESIDUE.sub("", text).strip(" .;,|")
    text = re.sub(r"\s*[★☆*]+\s*", " ", text)
    return " ".join(text.split()).strip(" .;,|")


def _clean_reference_doi(value: Any) -> str:
    text = _clean_reference_field(value)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    return text.rstrip(".,;)")


def _metadata_value(row: dict[str, Any], field: str) -> Any:
    value = row.get(field)
    return value.get("value") if isinstance(value, dict) else value


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

    raw_authors = _metadata_value(row, "authors")
    authors = (
        ", ".join(
            cleaned
            for item in raw_authors
            if (cleaned := _clean_reference_field(item))
        )
        if isinstance(raw_authors, list)
        else _clean_reference_field(raw_authors)
    )
    journal = _clean_reference_field(_metadata_value(row, "journal"))
    year = _clean_reference_field(
        _metadata_value(row, "bibliographic_year")
        or _metadata_value(row, "year")
    )
    volume = _clean_reference_field(_metadata_value(row, "volume"))
    issue = _clean_reference_field(
        _metadata_value(row, "issue") or _metadata_value(row, "number")
    )
    pages = _clean_reference_field(
        _metadata_value(row, "pages")
        or _metadata_value(row, "page")
        or _metadata_value(row, "article_number")
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
    doi = _clean_reference_doi(_metadata_value(row, "doi"))
    if doi:
        doi = f"https://doi.org/{doi}"
    parts = [
        authors,
        _clean_reference_field(_metadata_value(row, "title")),
        publication,
        doi,
    ]
    rendered = ". ".join(part.rstrip(".") for part in parts if part)
    return rendered or _clean_reference_field(fallback) or "Unresolved paper"


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


def ordered_callouts(markdown: str) -> list[int]:
    result: list[int] = []
    for match in CALLOUT_RE.finditer(str(markdown or "")):
        for number in expand_callouts(match.group(1)):
            if number not in result:
                result.append(number)
    return result


def _paragraph_text_by_id(markdown: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for marker in PARAGRAPH_MARKER.finditer(str(markdown or "")):
        prefix = markdown[: marker.start()].rstrip()
        start = prefix.rfind("\n\n") + 2
        text = prefix[start:].strip()
        if text and not text.startswith(("#", "![", "<!--")):
            result[str(marker.group(1))] = text
    return result


def _paragraph_spans(markdown: str) -> list[dict[str, Any]]:
    """Return paragraph bodies immediately preceding stable paragraph markers."""

    rows: list[dict[str, Any]] = []
    for marker in PARAGRAPH_MARKER.finditer(str(markdown or "")):
        prefix = markdown[: marker.start()].rstrip()
        start = prefix.rfind("\n\n") + 2
        text = prefix[start:].strip()
        if not text or text.startswith(("#", "![", "<!--")):
            continue
        rows.append(
            {
                "paragraph_id": str(marker.group(1)),
                "text": text,
                "start": start,
                "end": len(prefix),
            }
        )
    return rows


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
                    }
                )
    return rows


def strip_numeric_callouts(value: str) -> str:
    """Remove rendered numeric citations while preserving surrounding prose."""

    text = CALLOUT_RE.sub("", str(value or ""))
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _append_paragraph_callout(text: str, callout: str) -> str:
    """Place the source callout before generated Figure-context sentences."""

    cleaned = strip_numeric_callouts(text)
    figure_suffix = re.search(r"(?<!^)\s+(?=Figure\s+\d+\b)", cleaned)
    if figure_suffix:
        body = cleaned[: figure_suffix.start()].rstrip()
        suffix = cleaned[figure_suffix.start() :]
        return f"{body} {callout}{suffix}".strip()
    return f"{cleaned} {callout}".strip()


def _rerender_structured_citations(
    markdown: str,
    paragraph_identities: list[dict[str, Any]],
    matrix_rows: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Render one canonical citation group per paragraph from stable Paper IDs."""

    identity_by_paragraph = {
        str(row.get("paragraph_id") or ""): row
        for row in paragraph_identities
        if isinstance(row, dict) and str(row.get("paragraph_id") or "")
    }
    spans = _paragraph_spans(markdown)
    paper_order: list[str] = []
    missing_paragraphs: list[str] = []
    for paragraph in spans:
        paragraph_id = str(paragraph["paragraph_id"])
        identity = identity_by_paragraph.get(paragraph_id)
        if identity is None:
            if CALLOUT_RE.search(str(paragraph.get("text") or "")):
                missing_paragraphs.append(paragraph_id)
            continue
        for paper_id in identity.get("paper_ids") or []:
            value = str(paper_id or "").strip()
            if value and value not in paper_order:
                paper_order.append(value)
    missing_papers = sorted(set(paper_order) - set(matrix_rows))
    if missing_paragraphs or missing_papers or not paper_order:
        return markdown, {
            "status": "not_applied",
            "missing_paragraph_ids": missing_paragraphs,
            "missing_paper_ids": missing_papers,
        }

    paper_to_number = {
        paper_id: index for index, paper_id in enumerate(paper_order, start=1)
    }
    repaired_body = markdown
    for paragraph in reversed(spans):
        identity = identity_by_paragraph.get(str(paragraph["paragraph_id"]))
        if identity is None:
            continue
        numbers = list(
            dict.fromkeys(
                paper_to_number[str(paper_id)]
                for paper_id in identity.get("paper_ids") or []
                if str(paper_id) in paper_to_number
            )
        )
        if not numbers:
            continue
        callout = "[" + ", ".join(map(str, numbers)) + "]"
        replacement = _append_paragraph_callout(str(paragraph["text"]), callout)
        repaired_body = (
            repaired_body[: int(paragraph["start"])]
            + replacement
            + repaired_body[int(paragraph["end"]) :]
        )
    return repaired_body, {
        "status": "applied",
        "paper_order": paper_order,
        "paper_to_number": paper_to_number,
        "missing_paragraph_ids": [],
        "missing_paper_ids": [],
    }


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
    mapped: dict[int, str] = {}
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
            groups = [expand_callouts(match.group(1)) for match in CALLOUT_RE.finditer(text)]
            callouts = list(dict.fromkeys(number for group in groups for number in group))
            if len(callouts) != len(paper_ids) and groups:
                # Draft assembly appends the paragraph's structured citation
                # group at the end.  Prefer that group when earlier prose also
                # contains a comparison citation.
                callouts = groups[-1]
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
    if structured_paragraphs:
        structured_body, structured = _rerender_structured_citations(
            body,
            structured_paragraphs,
            matrix_rows,
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
            repaired = structured_body.rstrip() + "\n\n" + "\n".join(references) + "\n"
            return repaired, {
                "status": "applied",
                "changed": repaired != markdown,
                "mode": "structured_paragraph_identity",
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
        return "[" + ", ".join(map(str, values)) + "]" if values else match.group(0)

    repaired_body = CALLOUT_RE.sub(replace_group, body).rstrip()
    references = ["## References"]
    for paper_id in paper_order:
        number = paper_to_new[paper_id]
        references.append(
            f"[{number}] {reference_text(matrix_rows[paper_id], fallback=paper_id)}"
        )
    repaired = repaired_body + "\n\n" + "\n".join(references) + "\n"
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
    }
