#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


PLACEHOLDER_RE = re.compile(
    r"\b(TODO|TBD|citation needed|verify|verification needed|check this|fixme|待核查|需要核查|未确认)\b",
    re.I,
)
REF_CALLOUT_RE = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")
REF_ITEM_RE = re.compile(r"^\s*(?:\[(\d+)\]|\d+\.)\s+", re.M)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def expand_ref_callouts(text: str) -> set[int]:
    refs: set[int] = set()
    for match in REF_CALLOUT_RE.finditer(text):
        raw = match.group(1)
        for part in re.split(r"\s*,\s*", raw):
            if "-" in part:
                left, right = [p.strip() for p in part.split("-", 1)]
                if left.isdigit() and right.isdigit():
                    refs.update(range(int(left), int(right) + 1))
            elif part.strip().isdigit():
                refs.add(int(part.strip()))
    return refs


def scan_draft(project: Path) -> dict[str, Any]:
    final_draft = project / "07_final_audit" / "final_draft.md"
    first_draft = project / "05_first_draft" / "first_draft.md"
    draft = final_draft if final_draft.exists() else first_draft
    text = read_text(draft) if draft.exists() else ""
    headings = [{"level": len(m.group(1)), "title": m.group(2).strip()} for m in HEADING_RE.finditer(text)]
    duplicate_headings = sorted(
        {h["title"] for h in headings if [x["title"] for x in headings].count(h["title"]) > 1}
    )
    placeholder_hits = [
        {"line": idx, "text": line.strip()}
        for idx, line in enumerate(text.splitlines(), start=1)
        if PLACEHOLDER_RE.search(line)
    ]
    called_refs = sorted(expand_ref_callouts(text))
    listed_refs = sorted({int(m.group(1)) for m in REF_ITEM_RE.finditer(text) if m.group(1)})
    missing_listed_refs = [r for r in called_refs if listed_refs and r not in listed_refs]
    uncalled_listed_refs = [r for r in listed_refs if r not in called_refs]
    reference_numbering_gaps = (
        [n for n in range(1, listed_refs[-1] + 1) if n not in listed_refs] if listed_refs else []
    )
    image_paths = [m.group(1) for m in IMAGE_RE.finditer(text)]
    broken_images = []
    for raw in image_paths:
        if re.match(r"^[a-z]+://", raw):
            continue
        candidate = (draft.parent / raw).resolve()
        if not candidate.exists():
            broken_images.append(raw)
    figure_insert_report = project / "07_final_audit" / "figure_insertion_report.json"
    if not figure_insert_report.exists():
        figure_insert_report = project / "05_first_draft" / "figure_insertion_report.json"
    source_placeholder_mode = False
    if figure_insert_report.exists():
        try:
            report = json.loads(figure_insert_report.read_text(encoding="utf-8"))
            source_placeholder_mode = report.get("mode") == "source_candidates"
        except Exception:
            source_placeholder_mode = False
    empty_heading_titles = [h for h in headings if not h["title"].strip("# ").strip()]
    heading_jumps = []
    prev = 0
    for h in headings:
        level = h["level"]
        if prev and level > prev + 1:
            heading_jumps.append({"from": prev, "to": level, "title": h["title"]})
        prev = level
    issues = []
    if not draft.exists():
        issues.append("missing_first_draft")
    if placeholder_hits:
        issues.append("placeholder_or_verification_notes_present")
    if duplicate_headings:
        issues.append("duplicate_headings")
    if empty_heading_titles:
        issues.append("empty_headings")
    if heading_jumps:
        issues.append("heading_level_jumps")
    if missing_listed_refs:
        issues.append("reference_callouts_missing_from_reference_list")
    if reference_numbering_gaps:
        issues.append("reference_numbering_has_gaps")
    if broken_images:
        issues.append("broken_markdown_image_paths")
    if source_placeholder_mode:
        issues.append("source_figure_placeholders_need_redraw_or_permission_check")
    return {
        "project_dir": str(project),
        "draft_path": str(draft),
        "draft_exists": draft.exists(),
        "word_like_count": len(re.findall(r"\b[A-Za-z][A-Za-z-]*\b", text)),
        "heading_count": len(headings),
        "headings": headings,
        "duplicate_headings": duplicate_headings,
        "heading_jumps": heading_jumps,
        "placeholder_hits": placeholder_hits,
        "reference_callouts": called_refs,
        "reference_list_items": listed_refs,
        "missing_listed_refs": missing_listed_refs,
        "uncalled_listed_refs": uncalled_listed_refs,
        "reference_numbering_gaps": reference_numbering_gaps,
        "image_paths": image_paths,
        "broken_images": broken_images,
        "source_placeholder_mode": source_placeholder_mode,
        "issues": issues,
    }


def write_reports(out_dir: Path, scan: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "format_scan.json").write_text(
        json.dumps(scan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Format Scan",
        "",
        f"- Draft exists: {scan['draft_exists']}",
        f"- Word-like count: {scan['word_like_count']}",
        f"- Heading count: {scan['heading_count']}",
        f"- Issues: {', '.join(scan['issues']) if scan['issues'] else 'none'}",
        "",
        "## Placeholder Hits",
        "",
    ]
    if scan["placeholder_hits"]:
        for hit in scan["placeholder_hits"]:
            lines.append(f"- Line {hit['line']}: {hit['text']}")
    else:
        lines.append("None.")
    lines += ["", "## Reference Check", ""]
    lines.append(f"- Referenced callouts: {scan['reference_callouts']}")
    lines.append(f"- Listed references: {scan['reference_list_items']}")
    lines.append(f"- Missing listed refs: {scan['missing_listed_refs']}")
    lines.append(f"- Uncalled listed refs: {scan['uncalled_listed_refs']}")
    lines.append(f"- Reference numbering gaps: {scan['reference_numbering_gaps']}")
    lines += ["", "## Heading Check", ""]
    lines.append(f"- Duplicate headings: {scan['duplicate_headings']}")
    lines.append(f"- Heading jumps: {scan['heading_jumps']}")
    lines += ["", "## Image Check", ""]
    lines.append(f"- Image paths: {scan['image_paths']}")
    lines.append(f"- Broken images: {scan['broken_images']}")
    lines.append(f"- Source figure placeholder mode: {scan['source_placeholder_mode']}")
    (out_dir / "format_scan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic final format scan for a review project.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
    out_dir = project / "07_final_audit"
    scan = scan_draft(project)
    write_reports(out_dir, scan)
    print(f"Wrote final audit scan to {out_dir}")
    print(f"Issues: {len(scan['issues'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
