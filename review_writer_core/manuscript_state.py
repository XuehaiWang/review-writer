"""Language-neutral Final manuscript state derived from the released Markdown."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from review_writer_core.markdown_images import (
    malformed_markdown_image_lines,
    parse_markdown_image,
)
from review_writer_core.publication_caption import (
    infer_figure_role,
    repair_publication_ocr_splits,
)


SCHEMA_VERSION = 1
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
ARTIFACT_IMAGE = re.compile(r"/api/v1/artifacts/([0-9a-fA-F-]{36})/content")
INSERTED_FIGURE_METADATA = re.compile(
    r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.DOTALL
)
REFERENCE = re.compile(r"^\s*\[(\d+)\]\s*\.?\s+(.+?)\s*$")
CITATION = re.compile(r"\[([0-9][0-9,;\s\-–—]*)\]")
HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_COMMENT_DELIMITER = re.compile(r"<!--|-->")
PLACEHOLDER = re.compile(r"(?:\{\{[^{}]+\}\}|\b(?:TODO|TBD)\b)", re.IGNORECASE)
CJK = re.compile(r"[\u3400-\u9fff]")
ABSTRACT_HEADINGS = frozenset({"abstract", "summary", "摘要", "内容摘要"})
KEYWORD_HEADINGS = frozenset({"keywords", "keyword", "key words", "关键词", "关键字"})
EMPHASIZED_FRONT_MATTER_LABEL = re.compile(
    r"(\*\*|__|\*|_)\s*"
    r"((?:authors?|作者|affiliations?|机构|单位|date|日期|"
    r"keywords?|key words|关键词|关键字)\s*[:：])\s*\1",
    re.IGNORECASE,
)
TABLE_CAPTION = re.compile(r"^\*{0,2}(?:table|表)\s*\d+\s*[.:：]?\s*.+?\*{0,2}$", re.IGNORECASE)
FIGURE_LAYOUT_SPANS = frozenset({"auto", "single", "double"})
WIDE_FIGURE_ROLES = frozenset(
    {"workflow", "scope_samples", "comparison_ablation", "conceptual_overview"}
)
COMPACT_FIGURE_ROLES = frozenset({"quantitative_results", "structure_image"})
SINGLE_COLUMN_MAX_ASPECT_RATIO = 1.35
DOUBLE_COLUMN_MIN_ASPECT_RATIO = 1.55


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _citation_numbers(text: str) -> list[int]:
    values: set[int] = set()
    for match in CITATION.finditer(text):
        group = match.group(1).replace("–", "-").replace("—", "-")
        for start_raw, end_raw in re.findall(r"(\d+)\s*-\s*(\d+)", group):
            start, end = int(start_raw), int(end_raw)
            if start <= end and end - start <= 1000:
                values.update(range(start, end + 1))
        group = re.sub(r"\d+\s*-\s*\d+", " ", group)
        for token in re.findall(r"\d+", group):
            values.add(int(token))
    return sorted(values)


def _citation_text(block: dict[str, Any]) -> str:
    kind = str(block.get("kind") or "")
    if kind in {"heading", "paragraph"}:
        return str(block.get("text") or "")
    if kind == "list":
        return "\n".join(str(item) for item in block.get("items") or [])
    if kind == "table":
        cells = [*(block.get("header") or [])]
        cells.extend(cell for row in block.get("rows") or [] for cell in row)
        return "\n".join(str(cell) for cell in cells)
    if kind == "image":
        return "\n".join(
            (str(block.get("alt") or ""), str(block.get("caption") or ""))
        )
    return ""


def _semantic_text(block: dict[str, Any]) -> str:
    if block.get("kind") == "reference":
        return str(block.get("text") or "")
    values = [_citation_text(block)]
    if block.get("kind") == "table":
        values.append(str(block.get("caption") or ""))
    return "\n".join(value for value in values if value)


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _caption_text(line: str) -> str:
    stripped = line.strip()
    if len(stripped) >= 2 and stripped.startswith("*") and stripped.endswith("*"):
        return repair_publication_ocr_splits(stripped.strip("*").strip())
    return ""


def _asset_dimensions(raw_path: Any) -> tuple[float, float]:
    """Read only enough asset geometry to choose a PDF column span."""

    try:
        path = Path(str(raw_path or ""))
        if not path.is_file():
            return 0.0, 0.0
        if path.suffix.casefold() == ".pdf":
            reader = PdfReader(str(path))
            if not reader.pages:
                return 0.0, 0.0
            box = reader.pages[0].mediabox
            return float(box.width), float(box.height)
        with PILImage.open(path) as image:
            width, height = image.size
            return float(width), float(height)
    except (OSError, ValueError, TypeError, PyPdfError):
        # Geometry is a layout hint, never a publication gate. The existing
        # asset validation remains responsible for unreadable image files.
        return 0.0, 0.0


def choose_figure_layout(
    *,
    representative_role: Any = "unknown",
    width: Any = 0,
    height: Any = 0,
    requested_span: Any = "auto",
    review_overview: bool = False,
) -> dict[str, Any]:
    """Choose a stable one- or two-column figure layout without image AI."""

    requested = str(requested_span or "auto").strip().casefold()
    if requested not in FIGURE_LAYOUT_SPANS:
        requested = "auto"
    role = str(representative_role or "unknown").strip().casefold()
    try:
        numeric_width = float(width or 0)
        numeric_height = float(height or 0)
    except (TypeError, ValueError):
        numeric_width, numeric_height = 0.0, 0.0
    aspect_ratio = (
        round(numeric_width / numeric_height, 4)
        if numeric_width > 0 and numeric_height > 0
        else None
    )
    if review_overview:
        span, reason = "double", "review_overview_required"
    elif requested in {"single", "double"}:
        span, reason = requested, "explicit_override"
    elif aspect_ratio is not None and aspect_ratio >= DOUBLE_COLUMN_MIN_ASPECT_RATIO:
        span, reason = "double", "wide_aspect_ratio"
    elif aspect_ratio is not None and aspect_ratio <= SINGLE_COLUMN_MAX_ASPECT_RATIO:
        span, reason = "single", "compact_aspect_ratio"
    elif role in WIDE_FIGURE_ROLES:
        span, reason = "double", "wide_semantic_role"
    elif role in COMPACT_FIGURE_ROLES:
        span, reason = "single", "compact_semantic_role"
    else:
        span, reason = "single", "conservative_default"
    return {"span": span, "reason": reason, "aspect_ratio": aspect_ratio}


def _is_review_overview(alt: Any, caption: Any) -> bool:
    """Identify the manuscript-level overview inserted by the Final stage."""

    normalized_alt = re.sub(r"\s+", " ", str(alt or "")).strip().casefold()
    if normalized_alt in {"overview figure", "review overview figure", "综述总览图", "总览图"}:
        return True
    normalized_caption = re.sub(
        r"^\s*(?:figure|fig\.?|图)\s*\d+\s*[.:：\-]?\s*",
        "",
        str(caption or ""),
        flags=re.IGNORECASE,
    ).strip().casefold()
    return normalized_caption.startswith("review overview") or normalized_caption.startswith(
        "综述总览"
    )


def _split_values(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;，；]", str(value or ""))
        if item.strip()
    ]


def _normalize_front_matter_label_markup(value: Any) -> str:
    """Expose known metadata labels wrapped in Markdown emphasis.

    Final manuscripts conventionally use forms such as ``**Keywords:**``.
    The emphasis belongs to presentation, not to the field identity, so remove
    it before front-matter matching while leaving all other prose markup alone.
    """

    return EMPHASIZED_FRONT_MATTER_LABEL.sub(
        lambda match: str(match.group(2) or ""), str(value or "")
    )


def _front_matter(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract explicit journal metadata without inventing authors or claims."""

    consumed: set[int] = set()
    abstract_parts: list[str] = []
    keywords: list[str] = []
    authors: list[str] = []
    affiliations: list[str] = []
    released_date = ""
    first_heading = next(
        (index for index, block in enumerate(blocks) if block.get("kind") == "heading"),
        len(blocks),
    )
    metadata_patterns = (
        (re.compile(r"^(?:authors?|作者)\s*[:：]\s*(.+)$", re.IGNORECASE), authors, True),
        (re.compile(r"^(?:affiliations?|机构|单位)\s*[:：]\s*(.+)$", re.IGNORECASE), affiliations, False),
    )
    for index in range(first_heading):
        block = blocks[index]
        if block.get("kind") != "paragraph":
            continue
        text = _normalize_front_matter_label_markup(block.get("text")).strip()
        matched = False
        for pattern, target, split in metadata_patterns:
            match = pattern.match(text)
            if match:
                target.extend(_split_values(match.group(1)) if split else [match.group(1).strip()])
                consumed.add(index)
                matched = True
                break
        if matched:
            continue
        match = re.match(r"^(?:date|日期)\s*[:：]\s*(.+)$", text, re.IGNORECASE)
        if match:
            released_date = match.group(1).strip()
            consumed.add(index)
            continue
        match = re.match(r"^(?:keywords?|key words|关键词|关键字)\s*[:：]\s*(.+)$", text, re.IGNORECASE)
        if match:
            keywords.extend(_split_values(match.group(1)))
            consumed.add(index)

    index = first_heading
    while index < len(blocks):
        block = blocks[index]
        if block.get("kind") != "heading":
            index += 1
            continue
        heading = re.sub(
            r"^\s*\d+(?:\.\d+)*[.)、：:\-]?\s*", "", str(block.get("text") or "")
        ).strip().casefold()
        if heading not in ABSTRACT_HEADINGS and heading not in KEYWORD_HEADINGS:
            break
        consumed.add(index)
        cursor = index + 1
        while cursor < len(blocks) and blocks[cursor].get("kind") != "heading":
            candidate = blocks[cursor]
            if candidate.get("kind") == "paragraph":
                text = _normalize_front_matter_label_markup(
                    candidate.get("text")
                ).strip()
                # Final Markdown from an editor can place ``Keywords:`` on
                # the next source line without a blank Markdown paragraph.
                # The block parser then joins it to the abstract sentence, so
                # detect the labelled tail anywhere in this front-matter
                # paragraph and split the two semantic fields deterministically.
                keyword_match = re.search(
                    r"(?:^|\s)(?:keywords?|key words|关键词|关键字)\s*[:：]\s*(.+)$",
                    text,
                    re.IGNORECASE,
                )
                if heading in KEYWORD_HEADINGS:
                    keywords.extend(_split_values(text))
                elif keyword_match:
                    abstract_prefix = text[: keyword_match.start()].strip()
                    if abstract_prefix:
                        abstract_parts.append(abstract_prefix)
                    keywords.extend(_split_values(keyword_match.group(1)))
                elif text:
                    abstract_parts.append(text)
                consumed.add(cursor)
            elif candidate.get("kind") == "list" and heading in KEYWORD_HEADINGS:
                keywords.extend(str(item).strip() for item in candidate.get("items") or [])
                consumed.add(cursor)
            cursor += 1
        index = cursor
    return {
        "abstract": " ".join(abstract_parts).strip(),
        "keywords": list(dict.fromkeys(item for item in keywords if item)),
        "authors": list(dict.fromkeys(item for item in authors if item)),
        "affiliations": list(dict.fromkeys(item for item in affiliations if item)),
        "date": released_date,
        "consumed_block_indexes": sorted(consumed),
    }


def build_manuscript_state(
    markdown: str,
    *,
    artifact_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Parse released Markdown once so DOCX/PDF consistency can be audited."""

    source = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    comments = HTML_COMMENT.findall(source)
    inserted_figure_artifacts: list[str] = []
    inserted_figure_metadata: dict[str, list[dict[str, Any]]] = {}
    invalid_inserted_figure_metadata: list[str] = []
    for match in INSERTED_FIGURE_METADATA.finditer(source):
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError:
            invalid_inserted_figure_metadata.append(match.group(1)[:240])
            continue
        if not isinstance(metadata, dict):
            invalid_inserted_figure_metadata.append(match.group(1)[:240])
            continue
        artifact_id = str(metadata.get("output_artifact_id") or "").strip()
        if artifact_id:
            inserted_figure_artifacts.append(artifact_id)
            inserted_figure_metadata.setdefault(artifact_id, []).append(metadata)
    # Draft comments contain stable paragraph and figure-routing identifiers.
    # Keep them in the released Markdown for editing lineage, but never expose
    # them as semantic manuscript blocks or printable PDF text.
    render_source = HTML_COMMENT.sub("", source)
    paths = {str(key): str(value) for key, value in (artifact_paths or {}).items()}
    lines = render_source.splitlines()
    blocks: list[dict[str, Any]] = []
    title = "Scientific Review"
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            if level == 1 and title == "Scientific Review":
                title = text
            else:
                blocks.append({"kind": "heading", "level": level, "text": text})
            index += 1
            continue
        image = parse_markdown_image(stripped)
        if image:
            alt, source_url = image.alt, image.source
            artifact_match = ARTIFACT_IMAGE.search(source_url)
            artifact_id = artifact_match.group(1) if artifact_match else ""
            resolved = paths.get(artifact_id, source_url if not artifact_id else "")
            caption = ""
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index < len(lines):
                caption = _caption_text(lines[next_index])
                if caption:
                    index = next_index
            if artifact_id and not resolved:
                blockers.append(
                    {
                        "type": "unresolved_artifact_image",
                        "artifact_id": artifact_id,
                        "message": "A manuscript image could not be resolved inside the render bundle.",
                    }
                )
            if not caption:
                warnings.append(
                    {
                        "type": "caption_not_self_contained",
                        "message": f"Image `{alt or artifact_id or source_url}` has no explicit caption.",
                    }
                )
            metadata_queue = inserted_figure_metadata.get(artifact_id) or []
            metadata = metadata_queue.pop(0) if metadata_queue else {}
            representative_role = infer_figure_role(
                alt,
                caption,
                preferred=metadata.get("representative_role"),
            )
            asset_width, asset_height = _asset_dimensions(resolved)
            review_overview = _is_review_overview(alt, caption)
            layout = choose_figure_layout(
                representative_role=representative_role,
                width=asset_width,
                height=asset_height,
                requested_span=metadata.get("layout_span", "auto"),
                review_overview=review_overview,
            )
            blocks.append(
                {
                    "kind": "image",
                    "alt": alt,
                    "source": source_url,
                    "artifact_id": artifact_id,
                    "resolved_path": resolved,
                    "caption": caption,
                    "representative_role": representative_role,
                    "review_overview": review_overview,
                    "layout_span": layout["span"],
                    "layout_reason": layout["reason"],
                    "aspect_ratio": layout["aspect_ratio"],
                }
            )
            index += 1
            continue
        if "|" in line and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            caption = ""
            if blocks and blocks[-1].get("kind") == "paragraph":
                candidate = str(blocks[-1].get("text") or "").strip()
                if TABLE_CAPTION.match(candidate):
                    caption = candidate.strip("*").strip()
                    blocks.pop()
            header = _table_cells(line)
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index]))
                index += 1
            next_index = index
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index < len(lines):
                candidate = _caption_text(lines[next_index])
                if candidate and TABLE_CAPTION.match(candidate):
                    caption = candidate.strip("*").strip()
                    index = next_index + 1
            blocks.append(
                {"kind": "table", "header": header, "rows": rows, "caption": caption}
            )
            continue
        if re.match(r"^\s*(?:[-*+] |\d+[.)] )", line):
            items: list[str] = []
            ordered = bool(re.match(r"^\s*\d+[.)] ", line))
            while index < len(lines):
                match = re.match(r"^\s*(?:[-*+] |\d+[.)] )(.+)$", lines[index])
                if not match:
                    break
                items.append(match.group(1).strip())
                index += 1
            blocks.append({"kind": "list", "ordered": ordered, "items": items})
            continue
        reference = REFERENCE.match(line)
        if reference:
            blocks.append(
                {
                    "kind": "reference",
                    "number": int(reference.group(1)),
                    "text": reference.group(2).strip(),
                }
            )
            index += 1
            continue
        paragraph_lines = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index]
            if (
                HEADING.match(candidate)
                or parse_markdown_image(candidate.strip())
                or REFERENCE.match(candidate)
                or re.match(r"^\s*(?:[-*+] |\d+[.)] )", candidate)
                or (
                    "|" in candidate
                    and index + 1 < len(lines)
                    and _is_table_separator(lines[index + 1])
                )
            ):
                break
            paragraph_lines.append(candidate.strip())
            index += 1
        blocks.append({"kind": "paragraph", "text": " ".join(paragraph_lines)})

    front_matter = _front_matter(blocks)
    malformed_comment_delimiters = sorted(
        set(HTML_COMMENT_DELIMITER.findall(render_source))
    )
    if malformed_comment_delimiters:
        blockers.append(
            {
                "type": "malformed_html_comment",
                "message": "Unclosed or unmatched HTML comments must be repaired before PDF publication.",
                "examples": malformed_comment_delimiters,
            }
        )
    malformed_images = malformed_markdown_image_lines(render_source)
    if malformed_images:
        blockers.append(
            {
                "type": "malformed_markdown_image",
                "message": "Image-like Markdown could not be parsed and would be printed as body text.",
                "examples": malformed_images[:8],
            }
        )
    if invalid_inserted_figure_metadata:
        blockers.append(
            {
                "type": "invalid_inserted_figure_metadata",
                "message": "Inserted-figure routing metadata is invalid.",
                "examples": invalid_inserted_figure_metadata[:8],
            }
        )
    parsed_artifact_counts = Counter(
        str(block.get("artifact_id") or "")
        for block in blocks
        if block.get("kind") == "image" and block.get("artifact_id")
    )
    expected_artifact_counts = Counter(inserted_figure_artifacts)
    mismatched_figure_artifacts = [
        {
            "artifact_id": artifact_id,
            "expected_images": expected_count,
            "parsed_images": parsed_artifact_counts.get(artifact_id, 0),
        }
        for artifact_id, expected_count in sorted(expected_artifact_counts.items())
        if parsed_artifact_counts.get(artifact_id, 0) != expected_count
    ]
    if mismatched_figure_artifacts:
        blockers.append(
            {
                "type": "inserted_figure_image_mismatch",
                "message": "Inserted-figure metadata does not match the parsed manuscript images.",
                "examples": mismatched_figure_artifacts[:8],
            }
        )
    html_values = sorted(set(HTML_TAG.findall(render_source)))
    if html_values:
        blockers.append(
            {
                "type": "html_residue",
                "message": "Raw HTML tags must be removed before PDF publication.",
                "examples": html_values[:8],
            }
        )
    placeholders = sorted(set(PLACEHOLDER.findall(render_source)))
    if placeholders:
        blockers.append(
            {
                "type": "unresolved_placeholder",
                "message": "Unresolved placeholders must be removed before PDF publication.",
                "examples": placeholders[:8],
            }
        )
    references = [block["number"] for block in blocks if block["kind"] == "reference"]
    citation_source = "\n".join(
        _citation_text(block) for block in blocks if block["kind"] != "reference"
    )
    citations = _citation_numbers(citation_source)
    duplicate_references = sorted(
        number for number, count in Counter(references).items() if count > 1
    )
    if duplicate_references:
        blockers.append(
            {
                "type": "duplicate_reference_number",
                "message": "Reference numbers must be unique.",
                "reference_numbers": duplicate_references,
            }
        )
    missing_references = sorted(set(citations) - set(references))
    if missing_references:
        blockers.append(
            {
                "type": "undefined_citation",
                "message": "Citation callouts are missing from the reference list.",
                "reference_numbers": missing_references,
            }
        )
    uncited_references = sorted(set(references) - set(citations))
    if uncited_references:
        warnings.append(
            {
                "type": "uncited_reference",
                "message": "Reference-list entries are not cited in manuscript content.",
                "reference_numbers": uncited_references,
            }
        )
    if not front_matter["abstract"]:
        warnings.append(
            {
                "type": "abstract_missing",
                "message": "No explicit Abstract section was available for the journal-style title panel.",
            }
        )
    if not front_matter["keywords"]:
        warnings.append(
            {
                "type": "keywords_missing",
                "message": "No explicit Keywords field was available for the journal-style title panel.",
            }
        )
    for block_index, block in enumerate(blocks):
        if block.get("kind") == "table" and not str(block.get("caption") or "").strip():
            warnings.append(
                {
                    "type": "table_caption_missing",
                    "message": "A comparison table has no explicit self-contained caption.",
                    "block_index": block_index,
                }
            )
    cjk_count = len(CJK.findall(render_source))
    letter_count = max(1, len(re.findall(r"[A-Za-z\u3400-\u9fff]", render_source)))
    detected_language = "zh-CN" if cjk_count / letter_count >= 0.08 else "en"
    semantic_text = "\n".join(_semantic_text(block) for block in blocks)
    return {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "detected_language_profile": detected_language,
        "front_matter": front_matter,
        "source_markdown_sha256": _sha256(source),
        "semantic_sha256": _sha256(semantic_text),
        "blocks": blocks,
        "counts": {
            "blocks": len(blocks),
            "headings": sum(block["kind"] == "heading" for block in blocks),
            "paragraphs": sum(block["kind"] == "paragraph" for block in blocks),
            "images": sum(block["kind"] == "image" for block in blocks),
            "tables": sum(block["kind"] == "table" for block in blocks),
            "citations": len(citations),
            "references": len(references),
            "comments_ignored": len(comments),
            "inserted_figure_markers": len(inserted_figure_artifacts),
            "markdown_image_lines": sum(
                parse_markdown_image(line) is not None for line in lines
            ),
        },
        "citation_numbers": citations,
        "reference_numbers": references,
        "validation": {
            "valid": not blockers,
            "blocking_issues": blockers,
            "warning_issues": warnings,
        },
    }
