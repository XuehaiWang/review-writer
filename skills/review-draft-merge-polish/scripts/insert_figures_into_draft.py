#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _review_runtime.paths import resolve_review_root


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def has_saved_redrawn_output(
    project: Path,
    figure: dict[str, Any],
    current_candidate: dict[str, Any] | None = None,
) -> bool:
    """Accept a completed output, requiring human approval after a failed gate."""
    if figure.get("status") != "redrawn" or not figure.get("redrawn_image"):
        return False
    integrity_status = str((figure.get("chemistry_integrity") or {}).get("status") or "")
    requires_approval = bool(
        integrity_status in {"failed", "needs_human_arrow_check"}
        or str(figure.get("output_disposition") or "") == "saved_with_integrity_warning"
        or figure.get("requires_human_chemistry_approval")
    )
    approval = figure.get("human_approval") or {}
    if requires_approval and not (
        approval.get("status") == "approved"
        and approval.get("current_source_match", True)
        and approval.get("current_output_match", True)
    ):
        return False
    output = Path(str(figure["redrawn_image"]))
    if not output.is_absolute():
        output = project / output
    if not output.is_file():
        return False
    if requires_approval:
        source_value = str(
            (current_candidate or {}).get("source_image_path")
            or (current_candidate or {}).get("source_path")
            or (current_candidate or {}).get("image_path")
            or ""
        )
        source = Path(source_value)
        if not source.is_absolute():
            source = project / source
        approval_source = str(approval.get("source_image") or "")
        if (
            not source.is_file()
            or not approval_source
            or os.path.normcase(os.path.abspath(source))
            != os.path.normcase(os.path.abspath(approval_source))
            or file_sha256(source) != str(approval.get("source_sha256") or "")
            or file_sha256(output) != str(approval.get("output_sha256") or "")
        ):
            return False
    return True


def load_available_figures(project: Path) -> tuple[str, list[dict[str, Any]]]:
    candidates_path = project / "02_section_drafting" / "figure_candidates.json"
    candidate_data = read_json(candidates_path) if candidates_path.exists() else []
    candidate_rows = candidate_data.get("figures") if isinstance(candidate_data, dict) else candidate_data
    candidate_by_id = {
        str(row.get("figure_id")): row
        for row in candidate_rows or []
        if isinstance(row, dict) and row.get("figure_id")
    }
    redrawn_path = project / "03_figure_redraw" / "redrawn_figure_manifest.json"
    if redrawn_path.exists():
        data = read_json(redrawn_path)
        figures = data.get("figures") if isinstance(data, dict) else None
        if isinstance(figures, list):
            # The Stage 7 manifest is the ownership boundary. Do not enumerate
            # render modes here: doing so silently loses figures whenever a new
            # safe rendering profile is added. A completed row with an existing
            # output file is the single inclusion criterion.
            redrawn = [
                f
                for f in figures
                if isinstance(f, dict)
                and has_saved_redrawn_output(project, f, candidate_by_id.get(str(f.get("figure_id") or "")))
            ]
            if redrawn:
                # The redraw manifest can retain only a label ("Scheme 2") as
                # its caption. Recover the descriptive MinerU caption from the
                # selected candidate for the manuscript figure caption.
                for figure in redrawn:
                    current = str(figure.get("source_caption_text") or "").strip()
                    if re.fullmatch(r"(?:figure|fig\.?|scheme|table)\s*\d+\.?", current, re.I):
                        candidate = candidate_by_id.get(str(figure.get("figure_id") or ""))
                        recovered = str((candidate or {}).get("source_caption_text") or "").strip()
                        if recovered:
                            figure["source_caption_text"] = recovered
                return "redrawn", redrawn
    figures = candidate_rows
    source = [f for f in figures if isinstance(f, dict) and f.get("source_image_path")]
    return "source_candidates", source


def section_number(section_id: str) -> str:
    match = re.search(r"(\d+)", str(section_id or ""))
    return match.group(1) if match else ""


def copy_figure(project: Path, figure: dict[str, Any], index: int, mode: str) -> str | None:
    src = figure.get("redrawn_image") if mode == "redrawn" else figure.get("source_image_path")
    if not src:
        return None
    src_path = Path(str(src))
    if not src_path.exists():
        return None
    out_dir = project / "04_first_draft" / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = src_path.suffix.lower() or ".png"
    out_path = out_dir / f"figure_{index:02d}{suffix}"
    shutil.copy2(src_path, out_path)
    # Markdown URLs are forward-slash paths even on Windows; this keeps Stage 8
    # previews and file serving consistent across platforms.
    return f"figures/{out_path.name}"


def figure_kind(label: object) -> str:
    """Return the publication-facing figure class from the source label."""
    value = str(label or "")
    if re.match(r"^\s*scheme\b", value, re.I):
        return "Scheme"
    if re.match(r"^\s*table\b", value, re.I):
        return "Table"
    return "Figure"


def figure_markdown(figure: dict[str, Any], rel_path: str, index: int, mode: str) -> str:
    label = figure.get("source_label") or f"Figure {index}"
    caption = figure.get("source_caption_text") or figure.get("what_it_shows") or ""
    kind = figure_kind(label)
    # Source labels and redraw state are workflow metadata. Keep just the
    # descriptive source caption in the publication-facing manuscript.
    caption = re.sub(r"<[^>]+>", "", str(caption))
    caption = re.sub(r"^\s*(?:figure|fig\.?|scheme|table)\s*\d+\s*[.:]?\s*", "", caption, flags=re.I)
    caption = re.sub(r"\s+", " ", caption).strip().rstrip(".")
    published_caption = f"{kind} {index}."
    if caption:
        published_caption += f" {caption}."
    # Keep an invisible, stable anchor with the source label and target
    # paragraph.  The numbering pass uses this only to update references in
    # the associated paragraph; readers never see the marker in Markdown.
    metadata = json.dumps(
        {
            "figure_id": str(figure.get("figure_id") or ""),
            "target_paragraph_id": str(figure.get("target_paragraph_id") or ""),
            "source_label": str(label),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"\n\n<!-- inserted_figure: {metadata} -->\n\n"
        f"![{kind} {index}]({rel_path})\n\n"
        f"{published_caption}\n\n"
    )


def heading_aliases(section_id: str, section_heading: str) -> list[str]:
    """Return only aliases justified by the current project Blueprint.

    Older versions guessed topic-specific headings from ``sec2``/``sec3``.
    That could insert a figure into the wrong section after the project topic
    or outline changed. The stable section id remains available to the caller;
    heading fallback is therefore limited to the actual current heading and a
    generic Introduction alias for the conventional first section.
    """
    aliases = [str(section_heading or "").strip()]
    if str(section_id or "").strip().casefold() == "sec1":
        aliases.append("Introduction")
    return list(dict.fromkeys(alias for alias in aliases if alias))


PARAGRAPH_ID_RE = re.compile(r"<!--\s*paragraph_id:\s*([A-Za-z0-9_\-:.]+)\s*-->")
INSERTED_FIGURE_RE = re.compile(r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.S)
MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)")
PUBLISHED_CAPTION_RE = re.compile(
    r"^(?P<kind>Scheme|Figure|Table)\s+(?P<number>\d+)\.(?P<suffix>.*)$",
    re.M,
)


def source_label_parts(label: object) -> tuple[str, int] | None:
    match = re.match(r"^\s*(scheme|figure|fig\.?|table)\s*(\d+)\b", str(label or ""), re.I)
    if not match:
        return None
    source_kind = match.group(1).lower()
    kind = "Scheme" if source_kind == "scheme" else "Table" if source_kind == "table" else "Figure"
    return kind, int(match.group(2))


def replace_paragraph_figure_references(
    paragraph: str,
    source_label: object,
    previous_published_label: object,
    new_label: str,
) -> tuple[str, int]:
    """Rewrite only the anchor paragraph, never ambiguous document-wide labels."""
    updated = paragraph
    replacement_count = 0
    # On the first pass the paragraph has the source-paper number. On a
    # subsequent final-audit pass it already has the previous manuscript
    # number, which must be remapped if a template moved the visual.
    labels = [source_label, previous_published_label]
    seen: set[tuple[str, int]] = set()
    for label in labels:
        parts = source_label_parts(label)
        if not parts or parts in seen:
            continue
        seen.add(parts)
        kind, old_number = parts
        aliases = {
            "Scheme": r"(?:Scheme)",
            "Figure": r"(?:Figure|Fig\.?)",
            "Table": r"(?:Table)",
        }[kind]
        pattern = re.compile(rf"(?<![A-Za-z0-9]){aliases}\s*{old_number}(?![A-Za-z0-9])", re.I)
        updated, count = pattern.subn(new_label, updated)
        replacement_count += count
    return updated, replacement_count


def paragraph_text_bounds_before_marker(text: str, marker_start: int) -> tuple[int, int] | None:
    """Return the preceding Markdown paragraph for a paragraph-id marker."""
    prefix = text[:marker_start]
    end = len(prefix.rstrip())
    if end == 0:
        return None
    start = prefix.rfind("\n\n", 0, end)
    start = 0 if start < 0 else start + 2
    if not prefix[start:end].strip() or prefix[start:end].lstrip().startswith("#"):
        return None
    return start, end


def renumber_figures_in_manuscript(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Number inserted visuals in final manuscript order and repair local refs.

    Numbering is conventional: Scheme, Figure, and Table counters each begin
    at 1.  Source-paper labels are not globally substituted because multiple
    papers commonly contain a "Scheme 1"; only the paragraph selected as the
    figure's insertion anchor is eligible for a reference update.
    """
    counters = {"Scheme": 0, "Figure": 0, "Table": 0}
    replacements: list[tuple[int, int, str]] = []
    report: list[dict[str, Any]] = []
    for marker in INSERTED_FIGURE_RE.finditer(text):
        try:
            metadata = json.loads(marker.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, dict):
            continue
        next_marker = INSERTED_FIGURE_RE.search(text, marker.end())
        search_end = next_marker.start() if next_marker else len(text)
        image = MARKDOWN_IMAGE_RE.search(text, marker.end(), search_end)
        if not image:
            continue
        caption = PUBLISHED_CAPTION_RE.search(text, image.end(), search_end)
        if not caption:
            continue
        kind = figure_kind(metadata.get("source_label") or caption.group("kind"))
        counters[kind] += 1
        number = counters[kind]
        new_label = f"{kind} {number}"
        old_label = str(metadata.get("source_label") or caption.group("kind") + " " + caption.group("number"))
        previous_published_label = str(metadata.get("published_label") or "")
        updated_metadata = dict(metadata)
        updated_metadata["published_label"] = new_label
        replacements.append(
            (
                marker.start(),
                marker.end(),
                "<!-- inserted_figure: "
                + json.dumps(updated_metadata, ensure_ascii=False, separators=(",", ":"))
                + " -->",
            )
        )
        replacements.append((image.start(), image.end(), f"![{new_label}]({image.group('path')})"))
        replacements.append((caption.start(), caption.end(), f"{new_label}.{caption.group('suffix')}"))
        rewritten_references = 0
        target_pid = str(metadata.get("target_paragraph_id") or "")
        if target_pid:
            paragraph_marker = re.search(
                rf"<!--\s*paragraph_id:\s*{re.escape(target_pid)}\s*-->",
                text[:marker.start()],
            )
            if paragraph_marker:
                bounds = paragraph_text_bounds_before_marker(text, paragraph_marker.start())
                if bounds:
                    paragraph = text[bounds[0]:bounds[1]]
                    updated, rewritten_references = replace_paragraph_figure_references(
                        paragraph,
                        old_label,
                        previous_published_label,
                        new_label,
                    )
                    if updated != paragraph:
                        replacements.append((bounds[0], bounds[1], updated))
        report.append(
            {
                "figure_id": str(metadata.get("figure_id") or ""),
                "target_paragraph_id": target_pid,
                "source_label": old_label,
                "published_label": new_label,
                "kind": kind,
                "number": number,
                "references_rewritten": rewritten_references,
            }
        )
    for start, end, replacement in sorted(replacements, key=lambda value: value[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, report

def insert_after_paragraph(text: str, paragraph_id: str, block: str) -> tuple[str, bool]:
    if not paragraph_id:
        return text, False
    for match in PARAGRAPH_ID_RE.finditer(text):
        if match.group(1) == paragraph_id:
            insert_at = match.end()
            return text[:insert_at] + block + text[insert_at:], True
    return text, False

def insert_after_section(text: str, section_id: str, section_heading: str, block: str) -> tuple[str, bool]:
    for alias in heading_aliases(section_id, section_heading):
        escaped = re.escape(alias)
        pattern = rf"(^#{{2,3}}\s+.*{escaped}.*$)"
        match = re.search(pattern, text, re.M | re.I)
        if match:
            insert_at = match.end()
            return text[:insert_at] + block + text[insert_at:], True
    heading = re.escape(str(section_heading).strip())
    patterns = [
        rf"(^##\s+\d+\.?\s*{heading}.*$)",
        rf"(^##\s+{heading}.*$)",
        rf"(^#{{2,3}}\s+.*{heading}.*$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.M | re.I)
        if match:
            insert_at = match.end()
            return text[:insert_at] + block + text[insert_at:], True
    return text + "\n\n" + block, False


def figure_anchor_position(text: str, figure: dict[str, Any]) -> int:
    """Sort copied assets by the location where readers will encounter them."""
    paragraph_id = str(figure.get("target_paragraph_id") or "")
    if paragraph_id:
        for match in PARAGRAPH_ID_RE.finditer(text):
            if match.group(1) == paragraph_id:
                return match.start()
    section_heading = str(figure.get("section_heading") or "")
    for alias in heading_aliases(str(figure.get("section_id") or ""), section_heading):
        match = re.search(rf"^#{{2,3}}\s+.*{re.escape(alias)}.*$", text, re.M | re.I)
        if match:
            return match.start()
    return len(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Insert available figures into the first draft.")
    parser.add_argument("--review-root", default=None)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--max-per-section", type=int, default=0,
                        help="Maximum figures per section; 0 keeps every human-selected figure.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_root = resolve_review_root(args.review_root, anchor=Path(__file__))
    project = review_root / "review-projects" / args.project_id
    draft_path = project / "04_first_draft" / "first_draft.md"
    if not draft_path.exists():
        raise SystemExit(f"Missing first draft: {draft_path}")
    mode, figures = load_available_figures(project)
    if not figures:
        raise SystemExit("No available figures to insert.")
    selected_by_anchor: dict[str, dict[str, Any]] = {}
    for figure in figures:
        anchor = str(figure.get("target_paragraph_id") or figure.get("paper_id") or figure.get("figure_id") or "")
        if not anchor:
            continue
        existing = selected_by_anchor.get(anchor)
        if existing is None or (figure.get("human_selected_source") and not existing.get("human_selected_source")):
            selected_by_anchor[anchor] = figure
    selected = list(selected_by_anchor.values())
    if args.max_per_section > 0:
        limited: list[dict[str, Any]] = []
        section_counts: dict[str, int] = {}
        for figure in selected:
            section_id = str(figure.get("section_id") or "")
            if section_counts.get(section_id, 0) >= args.max_per_section:
                continue
            section_counts[section_id] = section_counts.get(section_id, 0) + 1
            limited.append(figure)
        selected = limited
    text = read_text(draft_path)
    # The manifest preserves review workflow order, not prose order.  Asset
    # file names and provisional captions therefore follow their final reading
    # order before the independent manuscript-numbering pass below.
    selected.sort(key=lambda figure: (
        figure_anchor_position(text, figure),
        str(figure.get("figure_id") or ""),
    ))
    inserted = []
    for index, figure in enumerate(selected, start=1):
        rel = copy_figure(project, figure, index, mode)
        if not rel:
            continue
        heading = figure.get("section_heading") or ""
        block = figure_markdown(figure, rel, index, mode)
        target_pid = str(figure.get("target_paragraph_id") or "")
        text, matched = insert_after_paragraph(text, target_pid, block)
        anchor_mode = "paragraph" if matched else "section"
        if not matched:
            text, matched = insert_after_section(text, str(figure.get("section_id") or ""), heading, block)
        inserted.append(
            {
                "figure_number": index,
                "section_id": figure.get("section_id"),
                "paper_id": figure.get("paper_id"),
                "source_label": figure.get("source_label"),
                "inserted_path": rel,
                "mode": mode,
                "anchor_mode": anchor_mode,
                "target_paragraph_id": target_pid,
                "matched_heading": matched,
            }
        )
    text, numbering = renumber_figures_in_manuscript(text)
    write_text(draft_path, text)
    report = {
        "project_id": args.project_id,
        "mode": mode,
        "inserted_count": len(inserted),
        "inserted": inserted,
        "numbering": {
            "strategy": "manuscript_order_per_kind",
            "entries": numbering,
            "note": "Scheme, Figure, and Table are each numbered sequentially in final manuscript order. References are rewritten only in the selected anchor paragraph to avoid collisions between source-paper labels.",
        },
        "note": "source_candidates mode means source images were inserted as placeholders and should be redrawn/permission-checked before final release.",
    }
    (project / "04_first_draft" / "figure_insertion_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "04_first_draft" / "figure_numbering_report.json").write_text(
        json.dumps(
            {
                "project_id": args.project_id,
                "strategy": "manuscript_order_per_kind",
                "entries": numbering,
                "reference_rewrite_scope": "selected anchor paragraph only",
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Inserted {len(inserted)} figures into {draft_path} using mode={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
