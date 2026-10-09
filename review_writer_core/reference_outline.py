"""Turn an uploaded reference review into the outline headings it contains.

This module is deliberately deterministic: it extracts the headings a document
already has, keeps their nesting, and hands back Markdown for the outline editor.
It never writes new headings of its own, so nothing can appear in the outline
that is not in the uploaded document — except the Introduction the editor
contract requires, which is inserted only when the document has none.

PDF input reaches this module through MinerU's Markdown (per-heading ``level``),
DOCX through Word heading styles, and Markdown/text through their own headings.
The browser-side outline parser accepts the ``##``–``######`` headings and the
optional ``Section role:`` / notes lines produced here.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable

# A reference review is not a section of the manuscript, so these never become
# outline sections. Conclusion stays: it is part of the review's structure.
SKIPPED_TITLE_PATTERN = re.compile(
    r"^(?:abstract|graphical abstract|highlights?|acknowledg(?:e)?ments?|funding"
    r"|declaration of (?:competing |conflicting )?interests?|conflicts? of interest"
    r"|author contributions?|data availability|supplementary (?:information|material)"
    r"|supporting information|references?|bibliography|literature cited|list of references"
    r"|appendi(?:x|ces)(?:\s+[a-z0-9]+)?|abbreviations|glossary|nomenclature"
    r"|table of contents|contents)$",
    re.IGNORECASE,
)
# Front-matter lists announce themselves with a colon and then their contents.
SKIPPED_LIST_PATTERN = re.compile(
    r"^(?:keywords?|key words|abbreviations|highlights?)\s*[:\-\u2013\u2014].*$",
    re.IGNORECASE,
)
INTRODUCTION_TITLE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s*)?(?:general\s+)?"
    r"(?:introduction|introductory\s+remarks?|background|overview|introduction\s+and\s+\w[\w\s]*"
    r"|引言|绪论|前言|概述|介绍)"
    r"[\s:.\-–—]*$",
    re.IGNORECASE,
)
CONCLUSION_TITLE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s*)?"
    r"(?:conclusions?(?:\s+and\s+(?:outlook|perspectives?|future\s+\w+))?|summary\s+and\s+\w[\w\s]*"
    r"|concluding\s+remarks?|outlook|perspectives?|future\s+(?:directions?|perspectives?|outlook)"
    r"|总结|结论|结论与展望|展望)"
    r"[\s:.\-–—]*$",
    re.IGNORECASE,
)
MAX_SECTIONS = 80
UNVERIFIED_NOTE = "Verification: 标题未能在原文中逐字定位，请核对后再使用。"


@dataclass(frozen=True)
class Heading:
    """One heading as the document presents it."""

    level: int
    title: str


@dataclass(frozen=True)
class OutlineSection:
    """One heading as the outline editor needs it."""

    level: int  # 2..6
    title: str
    role: str = "body"  # introduction | body | conclusion
    unverified: bool = False


@dataclass(frozen=True)
class ExtractionResult:
    sections: list[OutlineSection]
    skipped: list[str]
    dropped_duplicates: list[str]
    introduced_heading: bool
    truncated: int
    confidence: str  # high | medium | low


def _clean_title(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ")
    text = re.sub(r"[\s\u00a0]+", " ", text).strip()
    return text.strip("#*_`").strip()


def _match_without_number(title: str) -> str:
    return re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", title).strip()


def is_skipped_title(title: str) -> bool:
    stem = _match_without_number(title)
    return bool(
        SKIPPED_TITLE_PATTERN.match(stem)
        or SKIPPED_TITLE_PATTERN.match(title)
        or SKIPPED_LIST_PATTERN.match(stem)
    )


def _role_for(title: str, index: int) -> str:
    if index == 0:
        return "introduction"
    if CONCLUSION_TITLE_PATTERN.match(_match_without_number(title)) or CONCLUSION_TITLE_PATTERN.match(title):
        return "conclusion"
    return "body"


def headings_from_markdown(text: str) -> list[Heading]:
    """Read ``#``-style headings, ignoring fenced code blocks."""
    headings: list[Heading] = []
    fence = False
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = not fence
            continue
        if fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        if not match:
            continue
        title = _clean_title(match.group(2))
        if title:
            headings.append(Heading(level=len(match.group(1)), title=title))
    return headings


def headings_from_plain_text(text: str) -> list[Heading]:
    """Read numbered plain-text headings (``1. Introduction``, ``2.1 Scope``).

    Plain text carries no style information, so only lines that clearly look
    like headings qualify; the caller is told the confidence is lower.
    """
    headings: list[Heading] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or len(line) > 160:
            continue
        match = re.match(r"^(\d+(?:\.\d+)*)[.)]?\s+(\S.*?)\s*$", line)
        if not match:
            match = re.match(r"^(?:\d+\.\d+\s+)?([A-Z][^.!?]{2,80})\s*$", line)
            if not match or line.endswith("."):
                continue
            title = _clean_title(match.group(1))
            level = 1
        else:
            title = _clean_title(match.group(2))
            level = match.group(1).count(".") + 1
        if title:
            headings.append(Heading(level=level, title=title))
    return headings


def _docx_paragraphs(xml: str) -> list[tuple[int | None, str]]:
    """Return ``(heading_level, text)`` for every paragraph of a DOCX body."""
    paragraphs: list[tuple[int | None, str]] = []
    for chunk in re.split(r"</w:p>", xml):
        style = re.search(r'<w:pStyle[^>]*w:val="([^"]+)"', chunk)
        level: int | None = None
        if style:
            name = style.group(1)
            heading = re.search(r"heading\s*([1-9])", name, re.IGNORECASE)
            if heading:
                level = int(heading.group(1))
            elif re.fullmatch(r"[Hh]eading", name):
                level = 1
            elif re.fullmatch(r"[Tt]itle|[Ss]ubtitle", name):
                # Level 0 marks the document's own title so it is never imported
                # as one of the review's sections.
                level = 0
        if level is None:
            outline = re.search(r'<w:outlineLvl[^>]*w:val="(\d+)"', chunk)
            if outline:
                level = int(outline.group(1)) + 1
        pieces = re.findall(r"<w:t[^>]*>(.*?)</w:t>", chunk, re.DOTALL)
        text = _clean_title("".join(pieces).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
        if text:
            paragraphs.append((level, text))
    return paragraphs


def headings_from_docx(raw: bytes) -> list[Heading]:
    """Read Word heading styles (and outline levels) from a DOCX body."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    return [
        Heading(level=level, title=text)
        for level, text in _docx_paragraphs(xml)
        if level is not None
    ]


def pdf_flat_text(raw: bytes, *, max_pages: int | None = None) -> str:
    """Read a PDF's text without layout information.

    Only used for the language hint and as the fallback when MinerU is not
    available; it cannot recover heading levels, which is why the primary PDF
    path goes through MinerU's Markdown instead.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a declared dependency
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = reader.pages[:max_pages] if max_pages else reader.pages
        return "\n".join(page.extract_text() or "" for page in pages)
    except Exception:
        return ""


def pdf_language_hint(text: str) -> str:
    """Return MinerU's ``ch`` hint when the text is mostly Chinese, else ``""``."""
    han = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return "ch" if han >= 50 and han * 4 >= latin else ""


def extract_outline(
    headings: Iterable[Heading],
    *,
    confidence: str = "high",
    max_sections: int = MAX_SECTIONS,
) -> ExtractionResult:
    """Normalize found headings into the outline the editor can load."""
    kept: list[Heading] = []
    skipped: list[str] = []
    dropped_duplicates: list[str] = []
    seen: set[str] = set()
    for heading in headings:
        title = _clean_title(heading.title)
        if not title:
            continue
        if is_skipped_title(title):
            skipped.append(title)
            continue
        fingerprint = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", title).casefold()
        if fingerprint in seen:
            # Running heads and repeated section titles are not new sections.
            dropped_duplicates.append(title)
            continue
        seen.add(fingerprint)
        kept.append(Heading(level=max(0, int(heading.level)), title=title))
    if not kept:
        return ExtractionResult([], skipped, dropped_duplicates, False, 0, confidence)

    # A leading heading that stands alone at the document's top level is the
    # document's title, not one of its sections; importing it would push every
    # real section one level down.
    top_level = min(heading.level for heading in kept)
    if (
        len(kept) > 1
        and kept[0].level == top_level
        and sum(1 for heading in kept if heading.level == top_level) == 1
        and not INTRODUCTION_TITLE_PATTERN.match(_match_without_number(kept[0].title))
    ):
        # It comes first in the document, so it stays first in the report.
        skipped.insert(0, kept[0].title)
        kept = kept[1:]

    # Compress the document's own levels onto consecutive outline levels so the
    # editor contract holds: the first section is level 2 and no level is skipped.
    rank = {level: 2 + index for index, level in enumerate(sorted({h.level for h in kept}))}
    sections = [OutlineSection(level=rank[h.level], title=h.title) for h in kept]

    truncated = 0
    if len(sections) > max_sections:
        truncated = len(sections) - max_sections
        sections = sections[:max_sections]

    introduced = False
    if not INTRODUCTION_TITLE_PATTERN.match(_match_without_number(sections[0].title)):
        sections.insert(0, OutlineSection(level=2, title="Introduction"))
        introduced = True

    sections = [
        OutlineSection(
            level=section.level,
            title=section.title,
            role=_role_for(section.title, index),
            unverified=section.unverified,
        )
        for index, section in enumerate(sections)
    ]
    return ExtractionResult(sections, skipped, dropped_duplicates, introduced, truncated, confidence)


def render_outline_markdown(sections: Iterable[OutlineSection]) -> str:
    """Render sections as the Markdown the outline editor loads."""
    blocks: list[str] = []
    for section in sections:
        lines = [f"{'#' * max(2, min(6, section.level))} {section.title}"]
        if section.role and section.role != "body":
            lines.append(f"Section role: {section.role}")
        if section.unverified:
            lines.append(UNVERIFIED_NOTE)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def title_present_verbatim(title: str, source_text: str) -> bool:
    """Whether a proposed title can be located in the document text.

    Used to keep any model-assisted step honest: it may select or re-level the
    headings a document has, but a title it cannot point at is not trusted.
    """
    needle = re.sub(r"[\s\u00a0]+", " ", _clean_title(title)).casefold()
    haystack = re.sub(r"[\s\u00a0]+", " ", str(source_text or "")).casefold()
    return bool(needle) and needle in haystack
