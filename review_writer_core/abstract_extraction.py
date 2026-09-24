"""Extract an explicitly labelled paper abstract from MinerU outputs."""

from __future__ import annotations

import re
from typing import Any


_LABEL = r"(?:a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|conspectus|summary|synopsis|内容摘要|摘要)"
_HEADING = re.compile(rf"^\s*(?:\d+(?:\.\d+)*[.)、]?\s*)?{_LABEL}\s*[.:：.—-]?\s*$", re.I)
_INLINE = re.compile(rf"^\s*{_LABEL}\s*[:：.—-]\s*(.+)$", re.I | re.S)
_BOUNDARY = re.compile(
    r"^(?:key\s*words?|关键词|关键字|introduction|background|references?|"
    r"experimental|results?(?:\s+and\s+discussion)?|methods?|conclusions?|"
    r"引言|前言|绪论|结果|讨论|实验|结论|参考文献)\b",
    re.I,
)
_NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)*[.)、]\s+\S+")
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s*(.+?)\s*#*\s*$")
_KEYWORD_TAIL = re.compile(r"\b(?:key\s*words?|关键词|关键字)\s*[:：]", re.I)
_BODY_START = re.compile(
    r"^(?:introduction|background|experimental|methods?|results?(?:\s+and\s+discussion)?|"
    r"引言|前言|绪论|实验|结果|讨论)$",
    re.I,
)


def _clean(value: str) -> str:
    value = re.sub(r"!\[[^]]*]\([^)]*\)", " ", value)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    return re.sub(r"\s+", " ", value).strip(" \t\r\n-—:：")


def _valid(value: str) -> bool:
    minimum = 35 if re.search(r"[\u3400-\u9fff]", value) else 60
    return minimum <= len(value) <= 6_000 and not _HEADING.fullmatch(value)


def _section_start(value: str, *, markdown: bool = False) -> bool:
    line = value.strip().strip("* ")
    heading = _MARKDOWN_HEADING.match(line)
    if heading:
        line = heading.group(1).strip()
    unnumbered = re.sub(r"^\d+(?:\.\d+)*[.)、]?\s+", "", line)
    boundary = _BOUNDARY.match(unnumbered)
    label_end = unnumbered[boundary.end():].strip() if boundary else ""
    return bool(
        (boundary and (not label_end or label_end.startswith((":", "："))))
        or _NUMBERED_HEADING.match(line)
        or (markdown and heading)
    )


def _trim_keyword_tail(value: str) -> str:
    match = _KEYWORD_TAIL.search(value)
    return _clean(value[: match.start()] if match else value)


def _body_heading(value: str) -> bool:
    line = value.strip().strip("*# ")
    line = re.sub(r"^\d+(?:\.\d+)*[.)、]?\s+", "", line)
    return bool(_BODY_START.fullmatch(line.strip(" .:：")))


def _field(
    value: str, source: str, confidence: float,
    page: int | None = None, block_index: int | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "value": value,
        "source": source,
        "confidence": confidence,
        "human_checked": False,
    }
    if page is not None:
        field["source_page"] = page
    if block_index is not None:
        field["source_block_index"] = block_index
    return field


def _extract_from_lines(text: str, source: str, confidence: float) -> dict[str, Any] | None:
    lines = text[:200_000].splitlines()
    for position, raw in enumerate(lines):
        line = raw.strip()
        if _body_heading(line):
            break
        heading = _MARKDOWN_HEADING.match(line)
        label = heading.group(1).strip() if heading else line.strip("* ")
        inline = _INLINE.match(label)
        if not _HEADING.fullmatch(label) and not inline:
            continue
        parts = [inline.group(1)] if inline else []
        for next_raw in lines[position + 1 :]:
            next_line = next_raw.strip()
            if _section_start(next_line, markdown=True):
                break
            if next_line:
                parts.append(next_line)
            if sum(map(len, parts)) > 6_000:
                break
        value = _trim_keyword_tail(" ".join(parts))
        if _valid(value):
            return _field(value, source, confidence)
    return None


def extract_abstract(
    blocks: list[dict[str, Any]], markdown: str, pdf_first_page_text: str = ""
) -> dict[str, Any]:
    """Prefer a labelled MinerU block, then a labelled Markdown section.

    A nearby introduction paragraph is not evidence of a paper abstract.
    """

    front: list[tuple[int, str, bool, int | None]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") not in {"text", "list"}:
            continue
        try:
            page = int(block.get("page_idx", 0))
        except (TypeError, ValueError):
            page = 0
        if page < 0 or page > 3:
            continue
        raw = str(block.get("text") or block.get("content") or "").strip()
        if not raw:
            continue
        if _body_heading(raw):
            break
        front.append((index, raw, bool(block.get("text_level")), page + 1))

    for position, (index, raw, _is_heading, page) in enumerate(front):
        line = _clean(raw)
        inline = _INLINE.match(line) or re.match(rf"^\s*{_LABEL}\s*\n\s*(.+)$", raw, re.I | re.S)
        if not _HEADING.fullmatch(line) and not inline:
            continue
        parts = [inline.group(1)] if inline else []
        for _next_index, next_raw, next_is_heading, _next_page in front[position + 1 :]:
            next_text = _clean(next_raw)
            if _section_start(next_text) or (next_is_heading and len(next_text) < 120):
                break
            parts.append(next_text)
            if sum(map(len, parts)) > 6_000:
                break
        value = _trim_keyword_tail(" ".join(parts))
        if _valid(value):
            return _field(value, "content_list_abstract_region", 0.88, page, index)

    for source, text, confidence in (
        ("mineru_markdown_abstract_region", markdown, 0.84),
        ("pdf_first_page_abstract_region", pdf_first_page_text, 0.70),
    ):
        found = _extract_from_lines(text, source, confidence)
        if found:
            if source == "pdf_first_page_abstract_region":
                found["source_page"] = 1
            return found

    return _field("", "rule_not_found", 0.0)
