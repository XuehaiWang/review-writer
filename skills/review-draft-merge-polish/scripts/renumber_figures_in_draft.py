#!/usr/bin/env python3
"""Apply the manuscript-order figure numbering pass to an existing draft."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from insert_figures_into_draft import read_text, renumber_figures_in_manuscript, write_text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _review_runtime.paths import resolve_review_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Renumber inserted visuals in manuscript order.")
    parser.add_argument("--review-root", default=None)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--stage",
        choices=("04_first_draft", "05_final_audit"),
        default="04_first_draft",
        help="Draft stage containing the Markdown manuscript.",
    )
    parser.add_argument("--filename", default="", help="Override the stage's default Markdown filename.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_root = resolve_review_root(args.review_root, anchor=Path(__file__))
    stage = review_root / "review-projects" / args.project_id / args.stage
    filename = args.filename or ("first_draft.md" if args.stage == "04_first_draft" else "final_draft.md")
    draft_path = stage / filename
    if not draft_path.is_file():
        raise SystemExit(f"Missing draft: {draft_path}")
    updated, entries = renumber_figures_in_manuscript(read_text(draft_path))
    write_text(draft_path, updated)
    report_path = stage / "figure_numbering_report.json"
    report_path.write_text(
        json.dumps(
            {
                "project_id": args.project_id,
                "stage": args.stage,
                "strategy": "manuscript_order_per_kind",
                "entries": entries,
                "reference_rewrite_scope": "selected anchor paragraph only",
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Renumbered {len(entries)} visuals in {draft_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
