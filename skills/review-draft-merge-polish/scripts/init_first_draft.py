#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def load_figures(project: Path) -> dict[str, Any] | None:
    redrawn_path = project / "03_figure_redraw" / "redrawn_figure_manifest.json"
    candidates_path = project / "02_section_drafting" / "figure_candidates.json"
    redrawn = read_json(redrawn_path) if redrawn_path.exists() else None
    if isinstance(redrawn, dict):
        figures = redrawn.get("figures")
        if isinstance(figures, list) and any(isinstance(f, dict) and f.get("status") == "redrawn" for f in figures):
            return {"source": str(redrawn_path), "mode": "redrawn", "figures": figures}
    if candidates_path.exists():
        candidates = read_json(candidates_path)
        figures = candidates.get("figures") if isinstance(candidates, dict) else candidates
        if isinstance(figures, list) and figures:
            return {"source": str(candidates_path), "mode": "source_candidates", "figures": figures}
    return redrawn if isinstance(redrawn, dict) else None


def infer_topic(project: Path) -> str:
    topic_input = project / "00_discovery" / "topic_input.md"
    if topic_input.exists():
        for line in topic_input.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
    combined = project / "00_discovery" / "combined_results_by_keyword.json"
    if combined.exists():
        try:
            data = read_json(combined)
            if isinstance(data, dict) and data.get("topic"):
                return str(data.get("topic"))
        except Exception:
            pass
    return ""


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    section_json = project / "02_section_drafting" / "section_drafts.json"
    if not section_json.exists():
        raise SystemExit(f"Missing section drafts: {section_json}")
    out_dir = project / "04_first_draft"
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = {
        "project_id": args.project_id,
        "topic": infer_topic(project),
        "selected_outline_md": read_text(project / "01_matrix_outline" / "selected_outline.md"),
        "matrix_outline_report_md": read_text(project / "01_matrix_outline" / "matrix_outline_report.md"),
        "section_tasks": read_json(project / "02_section_drafting" / "section_tasks.json") if (project / "02_section_drafting" / "section_tasks.json").exists() else None,
        "section_drafts": read_json(section_json),
        "section_drafts_md": read_text(project / "02_section_drafting" / "section_drafts.md"),
        "section_drafting_report_md": read_text(project / "02_section_drafting" / "section_drafting_report.md"),
        "redrawn_figure_manifest": read_json(project / "03_figure_redraw" / "redrawn_figure_manifest.json") if (project / "03_figure_redraw" / "redrawn_figure_manifest.json").exists() else None,
        "available_figures": load_figures(project),
        "figure_redraw_report_md": read_text(project / "03_figure_redraw" / "figure_redraw_report.md"),
    }
    write_json(out_dir / "draft_bundle.json", bundle)
    for name in ["first_draft.md", "merge_report.md", "remaining_issues.md"]:
        path = out_dir / name
        if not path.exists():
            path.write_text("", encoding="utf-8")
    print(f"Initialized first draft bundle in {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize first-draft merge bundle for a review project.")
    parser.add_argument("--review-root", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
