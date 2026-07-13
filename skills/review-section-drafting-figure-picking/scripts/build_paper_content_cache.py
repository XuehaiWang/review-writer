#!/usr/bin/env python3
"""Build a shared, compact per-paper content cache for section drafting.

Without this cache, every section-drafting subagent reopens each assigned
paper's full Markdown/PDF from scratch -- and since papers are commonly
assigned to more than one section, the same paper's full text gets re-read
several times across the pipeline. `literature_matrix.json` already holds a
~1000-word `main_content` summary and other per-paper fields produced during
the matrix stage; this script just projects those fields into one small,
paper_id-keyed JSON file so every section subagent can read ONE shared file
instead of re-reading N papers' full source documents.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_matrix_papers(project: Path) -> list[dict[str, Any]]:
    matrix_path = project / "01_matrix_outline" / "literature_matrix.json"
    if not matrix_path.exists():
        raise SystemExit(f"literature_matrix.json not found: {matrix_path}")
    data = read_json(matrix_path)
    if isinstance(data, dict):
        papers = data.get("papers")
        if isinstance(papers, list):
            return papers
        return []
    if isinstance(data, list):
        return data
    return []


# Fields carried into the cache verbatim when present on a matrix row. These
# are exactly the fields review-section-drafting-figure-picking's SKILL.md
# says drafting needs (main_content, most_relevant_figure) plus the optional
# fields review-section-blueprint already relies on (output/input/method/
# process_type/limitation/selectivity), so the cache is a superset of what
# both consuming skills need without inventing new fields.
CACHE_FIELDS = [
    "paper_id",
    "title",
    "authors",
    "keywords",
    "abstract",
    "main_content",
    "most_relevant_figure",
    "output",
    "input",
    "method",
    "co_input",
    "modifier",
    "process_type",
    "document_scope",
    "limitation",
    "selectivity",
]


def build_cache(project: Path) -> dict[str, Any]:
    papers = load_matrix_papers(project)
    entries: dict[str, Any] = {}
    for row in papers:
        if not isinstance(row, dict):
            continue
        paper_id = str(row.get("paper_id") or "").strip()
        if not paper_id:
            continue
        entry = {key: row[key] for key in CACHE_FIELDS if key in row}
        entries[paper_id] = entry
    return {
        "paper_count": len(entries),
        "papers": entries,
        "usage_note": (
            "Read the entry for each assigned paper_id here instead of reopening its full "
            "Markdown/PDF. Only fall back to the paper's linked Markdown/PDF (via its "
            "metadata.json source_paths) when this cached main_content genuinely lacks a "
            "technical detail the section needs, or when verifying an exact figure/table "
            "caption before citing it."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a shared per-paper content cache from literature_matrix.json for section drafting.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    out = project / "03_section_drafting" / "paper_content_cache.json"
    cache = build_cache(project)
    write_json(out, cache)
    print(f"Wrote {out}")
    print(f"Papers cached: {cache['paper_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
