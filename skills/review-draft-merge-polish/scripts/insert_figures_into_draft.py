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
    redrawn_path = project / "04_figure_redraw" / "redrawn_figure_manifest.json"
    if redrawn_path.exists():
        data = read_json(redrawn_path)
        figures = data.get("figures") if isinstance(data, dict) else None
        if isinstance(figures, list):
            redrawn = [f for f in figures if isinstance(f, dict) and f.get("status") == "redrawn" and f.get("redrawn_image")]
            if redrawn:
                return "redrawn", redrawn
    candidates_path = project / "03_section_drafting" / "figure_candidates.json"
    data = read_json(candidates_path)
    figures = data.get("figures") if isinstance(data, dict) else data
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
    out_dir = project / "05_first_draft" / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = src_path.suffix.lower() or ".png"
    out_path = out_dir / f"figure_{index:02d}{suffix}"
    shutil.copy2(src_path, out_path)
    # Markdown image paths must use forward slashes regardless of host OS.
    return f"figures/{out_path.name}"


def figure_markdown(figure: dict[str, Any], rel_path: str, index: int, mode: str) -> str:
    label = figure.get("source_label") or f"Figure {index}"
    caption = figure.get("source_caption_text") or figure.get("what_it_shows") or ""
    paper_id = figure.get("paper_id") or "source paper"
    note = "Redrawn unified-style figure" if mode == "redrawn" else "Source figure placeholder; redraw still required before final release"
    return (
        f"\n\n![{label}]({rel_path})\n\n"
        f"**Figure {index}.** {caption} Source: {paper_id}, {label}. {note}.\n\n"
    )


def heading_aliases(section_id: str, section_heading: str) -> list[str]:
    # section_id is intentionally unused for alias lookup: there is no fixed
    # section skeleton to assume across topics. Matching relies solely on the
    # actual section_heading text (from figure_candidates.json); if it can't be
    # found in the draft, insert_after_section() falls back to appending the
    # figure at the end of the document rather than guessing a wrong heading.
    return [a for a in [str(section_heading or "").strip()] if a]


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
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--max-per-section", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
    draft_path = project / "05_first_draft" / "first_draft.md"
    if not draft_path.exists():
        raise SystemExit(f"Missing first draft: {draft_path}")
    mode, figures = load_available_figures(project)
    if not figures:
        raise SystemExit("No available figures to insert.")
    selected: list[dict[str, Any]] = []
    section_counts: dict[str, int] = {}
    for figure in figures:
        section_id = str(figure.get("section_id") or "")
        if section_counts.get(section_id, 0) >= args.max_per_section:
            continue
        section_counts[section_id] = section_counts.get(section_id, 0) + 1
        selected.append(figure)
    text = read_text(draft_path)
    inserted = []
    for index, figure in enumerate(selected, start=1):
        rel = copy_figure(project, figure, index, mode)
        if not rel:
            continue
        heading = figure.get("section_heading") or ""
        block = figure_markdown(figure, rel, index, mode)
        text, matched = insert_after_section(text, str(figure.get("section_id") or ""), heading, block)
        inserted.append(
            {
                "figure_number": index,
                "section_id": figure.get("section_id"),
                "paper_id": figure.get("paper_id"),
                "source_label": figure.get("source_label"),
                "inserted_path": rel,
                "mode": mode,
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
    (project / "05_first_draft" / "figure_insertion_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Inserted {len(inserted)} figures into {draft_path} using mode={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
