"""Shared Markdown paragraph-marker utilities.

The dashboard paragraph editor and the optional quality loop both depend on
stable ``paragraph_id`` comments.  This module keeps marker creation independent
of numbered headings so introductions, conclusions, uploaded outline styles,
and manually inserted prose are covered as well.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any


PARAGRAPH_MARKER_RE = re.compile(
    r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->"
)
REFERENCES_RE = re.compile(
    r"^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献|參考文獻)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
FIGURE_CAPTION_RE = re.compile(
    r"^\*{0,2}\s*(?:figure|scheme|table)\s+[A-Za-z0-9.-]+\b", re.IGNORECASE
)


def split_body_and_references(markdown: str) -> tuple[str, str]:
    match = REFERENCES_RE.search(markdown or "")
    if not match:
        return markdown, ""
    return markdown[: match.start()], markdown[match.start() :]


def _markdown_blocks(text: str) -> list[dict[str, Any]]:
    """Return blank-line-delimited blocks with source offsets."""

    blocks: list[dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    start: int | None = None
    parts: list[str] = []
    for line in lines:
        if line.strip():
            if start is None:
                start = offset
            parts.append(line)
        elif start is not None:
            blocks.append({"start": start, "end": offset, "text": "".join(parts).rstrip("\r\n")})
            start = None
            parts = []
        offset += len(line)
    if start is not None:
        blocks.append({"start": start, "end": len(text), "text": "".join(parts).rstrip("\r\n")})
    return blocks


def _is_prose_block(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped or PARAGRAPH_MARKER_RE.search(stripped):
        return False
    if stripped.startswith(("#", "<!--", "![", "|", "```", "~~~", "$$", "---", "<svg", "<figure")):
        return False
    if FIGURE_CAPTION_RE.match(stripped):
        return False
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if lines and all(re.match(r"^(?:[-+*]\s+|\d+[.)]\s+|>)", line) for line in lines):
        return False
    return bool(re.search(r"[A-Za-z\u0080-\uffff]", stripped))


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    if normalized in {"introduction", "background"}:
        return "intro"
    if normalized in {"conclusion", "conclusions", "summary"}:
        return "conclusion"
    if normalized in {"outlook", "perspective", "perspectives"}:
        return "outlook"
    return normalized[:32] or "section"


def _marker_prefix(body: str, position: int) -> str:
    headings = [match for match in HEADING_RE.finditer(body) if match.end() <= position]
    if not headings:
        return "front"
    heading = headings[-1].group(2).strip()
    numbered = re.match(r"\s*(\d+)\b", heading)
    return f"sec{numbered.group(1)}" if numbered else _slug(heading)


def ensure_prose_paragraph_markers(markdown: str) -> tuple[str, dict[str, Any]]:
    """Insert stable markers after every unmarked prose block.

    Existing IDs are never changed.  New IDs use the nearest heading plus a
    collision-free paragraph counter.  References, figures, tables, comments,
    headings, and list-only blocks are not treated as review paragraphs.
    """

    body, references = split_body_and_references(markdown or "")
    existing_ids = set(PARAGRAPH_MARKER_RE.findall(body))
    counters: dict[str, int] = {}
    insertions: list[tuple[int, str, str]] = []
    prose_count = 0
    for block in _markdown_blocks(body):
        if not _is_prose_block(str(block["text"])):
            continue
        prose_count += 1
        tail = body[int(block["end"]) :]
        marker = re.match(r"\s*<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->", tail)
        if marker:
            continue
        prefix = _marker_prefix(body, int(block["start"]))
        counter = counters.get(prefix, 0)
        while True:
            counter += 1
            paragraph_id = f"{prefix}-p{counter}"
            if paragraph_id not in existing_ids:
                break
        counters[prefix] = counter
        existing_ids.add(paragraph_id)
        insertions.append((int(block["end"]), f"\n\n<!-- paragraph_id: {paragraph_id} -->", paragraph_id))

    updated_body = body
    for position, marker_text, _paragraph_id in reversed(insertions):
        updated_body = updated_body[:position] + marker_text + updated_body[position:]
    updated = updated_body + references
    report = {
        "prose_paragraph_count": prose_count,
        "marker_count": len(PARAGRAPH_MARKER_RE.findall(updated_body)),
        "inserted": [paragraph_id for _position, _text, paragraph_id in insertions],
        "changed": bool(insertions),
    }
    return updated, report


def build_paragraph_manifest(markdown: str, project_id: str) -> dict[str, Any]:
    """Build a manifest from markers without assuming numbered headings."""

    body, _references = split_body_and_references(markdown or "")
    headings = list(HEADING_RE.finditer(body))
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for marker in PARAGRAPH_MARKER_RE.finditer(body):
        paragraph_id = marker.group(1)
        prefix_match = re.match(r"(.+?)-p\d+$", paragraph_id)
        section_id = prefix_match.group(1) if prefix_match else paragraph_id
        preceding = [heading for heading in headings if heading.end() <= marker.start()]
        heading = preceding[-1].group(2).strip() if preceding else "Front Matter"
        section = grouped.setdefault(
            section_id,
            {"section_id": section_id, "heading": heading, "paragraph_count": 0, "paragraphs": []},
        )
        section["paragraphs"].append({"paragraph_id": paragraph_id})
        section["paragraph_count"] = len(section["paragraphs"])
    sections = list(grouped.values())
    return {
        "project_id": project_id,
        "paragraph_count": sum(section["paragraph_count"] for section in sections),
        "sections": sections,
    }
