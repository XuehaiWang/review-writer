"""Build stable retrieval chunks from existing MinerU layout blocks.

This module deliberately performs no OCR and no PDF parsing.  It consumes the
immutable MinerU content list already admitted to Library, with Markdown only
as a compatibility fallback for older Library records.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable


CHUNKER_VERSION = "mineru-layout-v1"
_REFERENCE_HEADING = re.compile(
    r"^(references|bibliography|works\s+cited|参考文献|引用文献)\s*[:：]?$",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")
_ASSET_KEYS = (
    "img_path",
    "image_path",
    "table_path",
    "asset_path",
)


@dataclass(frozen=True)
class _Block:
    ordinal: int
    page: int
    block_type: str
    text: str
    section_path: tuple[str, ...]
    asset_refs: tuple[str, ...]
    is_reference: bool


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    ordinal: int
    content: str
    normalized_content: str
    content_type: str
    section_path: tuple[str, ...]
    page_start: int | None
    page_end: int | None
    block_start: int
    block_end: int
    asset_refs: tuple[str, ...]
    is_reference: bool
    previous_chunk_id: str = ""
    next_chunk_id: str = ""


def _normalize_text(value: str) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()


def _token_spans(text: str) -> list[tuple[int, int]]:
    """Return lightweight word/CJK spans without adding a tokenizer dependency."""

    return [match.span() for match in re.finditer(r"[\u3400-\u9fff]|[\w]+(?:[-'][\w]+)*|[^\s]", text)]


def _token_count(text: str) -> int:
    return len(_token_spans(text))


def _nested_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        normalized = _normalize_text(value)
        if normalized:
            yield normalized
        return
    if isinstance(value, list):
        for item in value:
            yield from _nested_strings(item)
        return
    if not isinstance(value, dict):
        return
    # MinerU v2 leaves contain {"type": "text", "content": "..."}.
    if isinstance(value.get("content"), str):
        yield from _nested_strings(value["content"])
        return
    for key, child in value.items():
        if key in {"bbox", "page_idx", "page_no", "level", "type"}:
            continue
        yield from _nested_strings(child)


def _block_text(raw: dict[str, Any]) -> str:
    direct = raw.get("text")
    if isinstance(direct, str) and _normalize_text(direct):
        return _normalize_text(direct)
    parts: list[str] = []
    for key in (
        "content",
        "image_caption",
        "image_footnote",
        "table_caption",
        "table_footnote",
        "table_body",
        "ocr_text",
    ):
        if key in raw:
            parts.extend(_nested_strings(raw[key]))
    # Preserve order while removing duplicate caption representations.
    return " ".join(dict.fromkeys(part for part in parts if part))


def _asset_refs(raw: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in _ASSET_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.replace("\\", "/").strip())
    return tuple(dict.fromkeys(values))


def _heading_level(raw: dict[str, Any]) -> int | None:
    value = raw.get("text_level")
    if value is None and isinstance(raw.get("content"), dict):
        value = raw["content"].get("level")
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(level, 12))


def _flatten_blocks(content_list: Any) -> list[tuple[int, dict[str, Any]]]:
    if not isinstance(content_list, list):
        return []
    flattened: list[tuple[int, dict[str, Any]]] = []
    if content_list and all(isinstance(page, list) for page in content_list):
        for page_index, page in enumerate(content_list):
            for raw in page:
                if isinstance(raw, dict):
                    flattened.append((page_index, raw))
        return flattened
    for raw in content_list:
        if not isinstance(raw, dict):
            continue
        try:
            page = int(raw.get("page_idx", raw.get("page_no", 0)))
        except (TypeError, ValueError):
            page = 0
        flattened.append((max(0, page), raw))
    return flattened


def _source_blocks(content_list: Any) -> list[_Block]:
    headings: list[str] = []
    references_level: int | None = None
    blocks: list[_Block] = []
    for ordinal, (page, raw) in enumerate(_flatten_blocks(content_list)):
        text = _block_text(raw)
        assets = _asset_refs(raw)
        if not text and not assets:
            continue
        block_type = str(raw.get("type") or "text").strip().casefold() or "text"
        level = _heading_level(raw)
        is_heading = level is not None or block_type in {"title", "heading"}
        if is_heading and text:
            level = level or 1
            headings = headings[: level - 1]
            headings.append(text)
            if _REFERENCE_HEADING.match(text):
                references_level = level
            elif references_level is not None and level <= references_level:
                references_level = None
        blocks.append(
            _Block(
                ordinal=ordinal,
                page=page,
                block_type=block_type,
                text=text,
                section_path=tuple(headings),
                asset_refs=assets,
                is_reference=references_level is not None,
            )
        )
    return blocks


def _split_oversized(block: _Block, *, max_tokens: int, overlap_tokens: int) -> list[_Block]:
    spans = _token_spans(block.text)
    if len(spans) <= max_tokens:
        return [block]
    parts: list[_Block] = []
    start_token = 0
    while start_token < len(spans):
        end_token = min(len(spans), start_token + max_tokens)
        start_char = spans[start_token][0]
        end_char = spans[end_token - 1][1]
        parts.append(replace(block, text=_normalize_text(block.text[start_char:end_char])))
        if end_token >= len(spans):
            break
        start_token = max(start_token + 1, end_token - overlap_tokens)
    return parts


def _merge_short_blocks(blocks: list[_Block], *, min_tokens: int, max_tokens: int) -> list[list[_Block]]:
    groups: list[list[_Block]] = []
    for block in blocks:
        if not groups:
            groups.append([block])
            continue
        previous = groups[-1]
        previous_last = previous[-1]
        combined_tokens = sum(_token_count(item.text) for item in previous) + _token_count(block.text)
        compatible = (
            block.page == previous_last.page
            and block.section_path == previous_last.section_path
            and block.is_reference == previous_last.is_reference
            and not block.asset_refs
            and not any(item.asset_refs for item in previous)
            and block.block_type not in {"title", "heading", "image", "table"}
            and previous_last.block_type not in {"title", "heading", "image", "table"}
        )
        previous_short = sum(_token_count(item.text) for item in previous) < min_tokens
        current_short = _token_count(block.text) < min_tokens
        if compatible and (previous_short or current_short) and combined_tokens <= max_tokens:
            previous.append(block)
        else:
            groups.append([block])
    return groups


def _stable_chunk_id(
    paper_id: str,
    document_version: str,
    *,
    block_start: int,
    block_end: int,
    part: int,
    content: str,
) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    seed = f"{paper_id}:{document_version}:{block_start}:{block_end}:{part}:{digest}"
    return "chk_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def build_document_chunks(
    paper_id: str,
    document_version: str,
    content_list: Any,
    *,
    markdown_fallback: str = "",
    min_tokens: int = 70,
    max_tokens: int = 360,
    overlap_tokens: int = 100,
) -> list[DocumentChunk]:
    """Create stable chunks and neighbor pointers from one immutable document version."""

    source = _source_blocks(content_list)
    fallback = _normalize_text(markdown_fallback)
    substantive_tokens = sum(
        _token_count(block.text)
        for block in source
        if block.block_type not in {"image", "table"}
    )
    if fallback and substantive_tokens < 20:
        next_ordinal = max((block.ordinal for block in source), default=-1) + 1
        source.append(
            _Block(
                ordinal=next_ordinal,
                page=max((block.page for block in source), default=0),
                block_type="markdown",
                text=fallback,
                section_path=(),
                asset_refs=(),
                is_reference=False,
            )
        )
    expanded: list[_Block] = []
    for block in source:
        expanded.extend(
            _split_oversized(
                block,
                max_tokens=max(40, max_tokens),
                overlap_tokens=max(0, min(overlap_tokens, max_tokens - 1)),
            )
        )
    groups = _merge_short_blocks(
        expanded,
        min_tokens=max(1, min_tokens),
        max_tokens=max(40, max_tokens),
    )
    chunks: list[DocumentChunk] = []
    same_block_parts: dict[int, int] = {}
    for ordinal, group in enumerate(groups):
        content = "\n\n".join(item.text for item in group if item.text).strip()
        assets = tuple(dict.fromkeys(ref for item in group for ref in item.asset_refs))
        if not content:
            content = " ".join(assets)
        block_start = min(item.ordinal for item in group)
        block_end = max(item.ordinal for item in group)
        part = same_block_parts.get(block_start, 0)
        same_block_parts[block_start] = part + 1
        chunks.append(
            DocumentChunk(
                chunk_id=_stable_chunk_id(
                    paper_id,
                    document_version,
                    block_start=block_start,
                    block_end=block_end,
                    part=part,
                    content=content,
                ),
                ordinal=ordinal,
                content=content,
                normalized_content=_normalize_text(content).casefold(),
                content_type=(group[0].block_type if len(group) == 1 else "merged_text"),
                section_path=group[-1].section_path,
                page_start=min(item.page for item in group) + 1,
                page_end=max(item.page for item in group) + 1,
                block_start=block_start,
                block_end=block_end,
                asset_refs=assets,
                is_reference=any(item.is_reference for item in group),
            )
        )
    return [
        replace(
            chunk,
            previous_chunk_id=(chunks[index - 1].chunk_id if index else ""),
            next_chunk_id=(chunks[index + 1].chunk_id if index + 1 < len(chunks) else ""),
        )
        for index, chunk in enumerate(chunks)
    ]
