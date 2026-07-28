#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
            redrawn = [
                f for f in figures
                if isinstance(f, dict)
                and f.get("status") == "redrawn"
                and f.get("redrawn_image")
                and (
                    f.get("render_mode") == "source-faithful-bw"
                    or (
                        f.get("render_mode") == "ocr-hollow-ai"
                        and (f.get("content_fidelity") or {}).get("status") == "pass"
                        and (f.get("structural_fidelity") or {}).get("status") == "pass"
                        and (
                            not f.get("chemistry_integrity")
                            or (f.get("chemistry_integrity") or {}).get("status") == "pass"
                        )
                    )
                )
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
    return str(Path("figures") / out_path.name)


def figure_markdown(figure: dict[str, Any], rel_path: str, index: int, mode: str) -> str:
    label = figure.get("source_label") or f"Figure {index}"
    caption = figure.get("source_caption_text") or figure.get("what_it_shows") or ""
    kind = "Scheme" if re.match(r"^\s*scheme\b", str(label), re.I) else "Figure"
    # Source labels and redraw state are workflow metadata. Keep just the
    # descriptive source caption in the publication-facing manuscript.
    caption = re.sub(r"<[^>]+>", "", str(caption))
    caption = re.sub(r"^\s*(?:figure|fig\.?|scheme|table)\s*\d+\s*[.:]?\s*", "", caption, flags=re.I)
    caption = re.sub(r"\s+", " ", caption).strip().rstrip(".")
    published_caption = f"{kind} {index}."
    if caption:
        published_caption += f" {caption}."
    return (
        f"\n\n![{label}]({rel_path})\n\n"
        f"{published_caption}\n\n"
    )


def heading_aliases(section_id: str, section_heading: str) -> list[str]:
    aliases = [str(section_heading or "").strip()]
    fallback = {
        "sec1": ["Introduction"],
        "sec2": ["Foundational methods", "activated propargylic", "Copper-catalyzed substitution"],
        "sec3": ["Carbonates", "esters"],
        "sec4": ["Radical", "one-electron", "photoredox"],
        "sec5": ["Direct transformations", "free propargylic alcohols"],
        "sec6": ["Organoboron", "organosilicon", "Stereochemical control", "mechanistic comparison"],
    }
    aliases.extend(fallback.get(str(section_id or ""), []))
    return [a for a in aliases if a]


PARAGRAPH_ID_RE = re.compile(r"<!--\s*paragraph_id:\s*([A-Za-z0-9_\-:.]+)\s*-->")

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Insert available figures into the first draft.")
    parser.add_argument("--review-root", default="/home/ps/review-writer")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--max-per-section", type=int, default=0,
                        help="Maximum figures per section; 0 keeps every human-selected figure.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
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
    write_text(draft_path, text)
    report = {
        "project_id": args.project_id,
        "mode": mode,
        "inserted_count": len(inserted),
        "inserted": inserted,
        "note": "source_candidates mode means source images were inserted as placeholders and should be redrawn/permission-checked before final release.",
    }
    (project / "04_first_draft" / "figure_insertion_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Inserted {len(inserted)} figures into {draft_path} using mode={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
