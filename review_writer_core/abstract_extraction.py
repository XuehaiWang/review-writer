"""Labelled abstract extraction and source-bound, optional opening recovery."""

from __future__ import annotations

import re
from collections.abc import Iterable
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
# Publishers print a rights/licence line directly after the abstract.  Without it
# the capture runs into the article body, because MinerU does not always emit a
# heading for the following section.
_POST_ABSTRACT = re.compile(
    r"^\s*(?:©|\(c\)\s*\d{4}\b|all rights reserved|published by|this journal is|"
    r"downloaded from|doi:\s*10\.|https?://(?:dx\.)?doi\.org/|"
    r"elsevier|wiley|springer|royal society of chemistry|american chemical society)",
    re.I,
)
_MIN_ABSTRACT_CHARS = 60
_MAX_ABSTRACT_CHARS = 6_000
_IMAGE_ONLY = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# A labelled abstract region normally holds one or two paragraphs.  Anything much
# longer means the region never found its end and swallowed the article body.
_ABSTRACT_REGION_LIMIT = 1_500
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


def abstract_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get('value')
    text = ' '.join(str(value or '').split())
    return '' if 'unavailable or unreliable' in text.casefold() else text


OPENING_ABSTRACT_INSTRUCTION = """
When the metadata abstract is missing, opening_candidate windows are the paper's
opening text, NOT automatically its abstract. In this same response optionally
return abstract_recovery:{decision:abstract|not_abstract|uncertain,
spans:[{evidence_key,quote}]}. Use decision=abstract only for an actual abstract
region summarizing this paper's question/approach/findings (including unlabelled
abstracts), not because it is the first paragraph. Introduction, prior-work
background, author/affiliation/copyright text and graphical captions are not an
abstract. Do not write a new summary or infer this paper's contribution from
previous authors' work. Copy complete contiguous original abstract passages into
quote, in reading order, from at most three opening_candidate windows. Preserve
qualifiers and do not join unrelated material. If absent, ambiguous or incomplete,
return not_abstract or uncertain with empty spans; still read the supplied opening
as ordinary context without claiming it is an abstract. This optional identification
must not prevent the normal reading result or initiate another model request.
"""


def build_opening_candidates(
    markdown: str,
    pdf_first_page_text: str = "",
    *,
    limit: int = 6,
    chars: int = 4_000,
    budget: int = 4_000,
) -> list[dict[str, Any]]:
    """Deterministic opening windows a model may quote an abstract from.

    The model never writes an abstract; it may only point at spans that exist in
    these windows, and `recover_opening_abstract` re-checks that the quotes really
    occur in the stored source.  Windows are built from local text only, so the
    library stage does not need a full-text index.  The budget keeps the windows
    inside the paper's opening, where an abstract can actually live.
    """

    windows: list[dict[str, Any]] = []
    blocks = [part.strip() for part in re.split(r"\n\s*\n", str(markdown or "")[:120_000]) if part.strip()]
    remaining = max(0, int(budget))
    for index, block in enumerate(blocks):
        if len(block) < 40 or _IMAGE_ONLY.fullmatch(block):
            continue
        windows.append(
            {
                "evidence_key": f"opening:{index}",
                "content_type": "paragraph",
                "page_start": 1,
                "page_end": 1,
                "section_path": [],
                "content": block[:chars],
                "opening_candidate": True,
            }
        )
        remaining -= len(block)
        if len(windows) >= limit or remaining <= 0:
            break
    first_page = str(pdf_first_page_text or "").strip()
    if first_page:
        windows.append(
            {
                "evidence_key": "opening:pdf_page_1",
                "content_type": "pdf_page",
                "page_start": 1,
                "page_end": 1,
                "section_path": [],
                "content": first_page[:chars],
                "opening_candidate": True,
            }
        )
    return windows


def recover_opening_abstract(raw: Any, sources: list[dict[str, Any]], existing: Any = '') -> dict[str, Any]:
    """Validate locations, not scientific truth; never replace an existing abstract.

    Missing/invalid optional output is a normal empty result, not a repair gate.
    Re-run at the persistence boundary against the frozen owned source registry.
    """
    if abstract_value(existing) or not isinstance(raw, dict) or raw.get('decision') != 'abstract':
        return {}
    spans = raw.get('spans')
    if not isinstance(spans, list) or not 1 <= len(spans) <= 3:
        return {}
    registry = {s['evidence_key']: s for s in sources if s.get('opening_candidate') is True and s.get('evidence_key')}
    parts, refs, seen = [], [], set()
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get('evidence_key'), str) or not isinstance(span.get('quote'), str):
            return {}
        key = span['evidence_key']
        source = registry.get(key)
        if not source or key in seen or any(_body_heading(str(h)) for h in source.get('section_path') or []):
            return {}
        quote = ' '.join(span['quote'].split())
        content = ' '.join(str(source.get('content') or '').split())
        if not quote or quote not in content:
            return {}
        seen.add(key)
        parts.append(quote)
        refs.append({'evidence_key': key, 'quote': quote})
    value = ' '.join(parts)
    if not _valid(value):
        return {}
    return {'decision': 'abstract', 'value': value, 'spans': refs,
            'source': 'model_identified_opening_abstract', 'human_checked': False}


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


def _abstract_paragraphs(lines: Iterable[str]) -> list[str]:
    """Collect the paragraphs of a labelled abstract region.

    The region ends at the next section heading, at the publisher's rights line,
    or once the capture has clearly run into the article body.
    """

    paragraphs: list[str] = []
    current: list[str] = []
    for raw in lines:
        line = raw.strip()
        if _section_start(line, markdown=True) or _POST_ABSTRACT.match(line):
            break
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
        collected = sum(map(len, current)) + sum(len(part) for part in paragraphs)
        if collected > _MAX_ABSTRACT_CHARS:
            break
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _region_value(paragraphs: list[str]) -> str:
    """Join an abstract region, discarding a runaway capture into the body.

    Image-only paragraphs are dropped first: MinerU emits the abstract's graphic
    between the label and the text, and it must never become the abstract itself.
    """

    cleaned = [part for part in (_clean(item) for item in paragraphs if item) if part]
    if not cleaned:
        return ""
    joined = " ".join(cleaned)
    if len(joined) <= _ABSTRACT_REGION_LIMIT:
        return joined
    first = cleaned[0]
    if len(first) <= _ABSTRACT_REGION_LIMIT:
        return first
    # One over-long paragraph: PDF text extraction has no blank lines, so keep
    # whole sentences up to the limit instead of the entire page.
    window = first[:_ABSTRACT_REGION_LIMIT]
    cut = max(window.rfind(mark) for mark in (".", "。", "?", "！", "!", "?"))
    return window[: cut + 1].strip() if cut >= _MIN_ABSTRACT_CHARS else window.strip()


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
        paragraphs = _abstract_paragraphs(lines[position + 1 :])
        if inline:
            paragraphs = [inline.group(1), *paragraphs]
        value = _trim_keyword_tail(_region_value(paragraphs))
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
        parts: list[str] = [inline.group(1)] if inline else []
        for _next_index, next_raw, next_is_heading, _next_page in front[position + 1 :]:
            next_text = _clean(next_raw)
            if (
                _section_start(next_text)
                or _POST_ABSTRACT.match(next_text)
                or (next_is_heading and len(next_text) < 120)
            ):
                break
            parts.append(next_text)
            if sum(map(len, parts)) > _MAX_ABSTRACT_CHARS:
                break
        value = _trim_keyword_tail(_region_value(parts))
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
