#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def lower(text: Any) -> str:
    return norm(text).lower()


STOPWORDS = {"and", "the", "from", "with", "for", "into", "via", "section", "review", "this"}


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", lower(text)) if t not in STOPWORDS}


def figure_score(candidate: dict[str, Any], section: dict[str, Any] | None = None) -> int:
    caption = lower(candidate.get("source_caption_text"))
    label = lower(candidate.get("source_label"))
    score = int(candidate.get("inventory_score") or 0)
    if "scheme" in label or "scheme" in caption or "overview" in caption:
        score += 8
    if "mechanism" in caption:
        score += 10
    if "scope" in caption:
        score += 4
    if "optimization" in caption:
        score -= 5
    if "control experiment" in caption:
        score -= 2
    if section:
        # Generic keyword-overlap bonus: no fixed subject-matter vocabulary —
        # reward captions that share substantive terms with this section's own
        # heading/argument, whatever those terms happen to be for this topic.
        section_tokens = tokens(" ".join([section.get("heading", ""), section.get("core_argument", "")]))
        caption_tokens = tokens(caption)
        overlap = len(section_tokens & caption_tokens)
        if overlap:
            score += min(overlap * 2, 6)
    if candidate.get("source_image_path"):
        score += 4
    return score


def inventory_by_paper(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(paper.get("paper_id")): paper
        for paper in inventory.get("papers", [])
        if isinstance(paper, dict) and paper.get("paper_id")
    }


def best_candidate_for_paper(paper: dict[str, Any]) -> dict[str, Any]:
    candidates = [c for c in paper.get("top_candidates", []) if isinstance(c, dict)]
    candidates.sort(key=lambda c: figure_score(c), reverse=True)
    if not candidates:
        return {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "status": "no_useful_figure",
            "no_useful_figure_reason": "No image/table candidates were found in MinerU content_list.",
        }
    best = dict(candidates[0])
    best.update(
        {
            "status": "selected_best_paper_level_candidate",
            "why_selected": "Highest-ranked overview, mechanism, scope, or diagram/scheme candidate from the MinerU inventory.",
            "manuscript_selected": False,
            "resolution_status": "ready" if best.get("source_image_path") else "needs_source_resolution",
            "needs_human_check": True,
        }
    )
    return best


def build_outputs(project: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inventory = read_json(project / "03_section_drafting" / "paper_figure_inventory.json")
    tasks = read_json(project / "03_section_drafting" / "section_tasks.json")
    by_paper = inventory_by_paper(inventory)

    paper_level: list[dict[str, Any]] = []
    for paper in inventory.get("papers", []):
        if isinstance(paper, dict):
            paper_level.append(best_candidate_for_paper(paper))

    manuscript: list[dict[str, Any]] = []
    used_keys: set[tuple[str, str]] = set()
    for section in tasks if isinstance(tasks, list) else []:
        if not isinstance(section, dict):
            continue
        figure_need = lower(section.get("figure_need"))
        if figure_need in {"no", "none", "optional"}:
            continue
        allowed = [str(pid) for pid in section.get("allowed_papers", [])]
        pool: list[dict[str, Any]] = []
        for paper_id in allowed:
            paper = by_paper.get(paper_id)
            if not paper:
                continue
            for candidate in paper.get("top_candidates", []):
                if isinstance(candidate, dict):
                    row = dict(candidate)
                    row["_score"] = figure_score(row, section)
                    pool.append(row)
        pool.sort(key=lambda c: c.get("_score", 0), reverse=True)
        section_selected = 0
        for candidate in pool:
            key = (str(candidate.get("paper_id")), str(candidate.get("source_image_path") or candidate.get("source_label")))
            if key in used_keys:
                continue
            used_keys.add(key)
            section_selected += 1
            candidate.pop("_score", None)
            candidate.update(
                {
                    "section_id": section.get("section_id"),
                    "section_heading": section.get("heading"),
                    "why_selected": (
                        "Selected as a section-level scheme/figure because it directly supports the section argument "
                        "and has a resolvable MinerU source image."
                    ),
                    "what_it_shows": candidate.get("source_caption_text") or candidate.get("source_label"),
                    "fits_paragraph_or_claim": section.get("core_argument"),
                    "recommended_action": "redraw" if candidate.get("source_type") != "table" else "retable",
                    "manuscript_selected": True,
                    "resolution_status": "ready" if candidate.get("source_image_path") else "needs_source_resolution",
                    "needs_human_check": True,
                }
            )
            manuscript.append(candidate)
            if section_selected >= 2:
                break
    return paper_level, manuscript


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select initial paper-level and manuscript figure candidates.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    paper_level, manuscript = build_outputs(project)
    out_dir = project / "03_section_drafting"
    write_json(out_dir / "paper_figure_candidates.json", paper_level)
    write_json(out_dir / "figure_candidates.json", manuscript)
    print(f"Wrote {out_dir / 'paper_figure_candidates.json'} ({len(paper_level)} records)")
    print(f"Wrote {out_dir / 'figure_candidates.json'} ({len(manuscript)} records)")
    if not manuscript:
        raise SystemExit("No manuscript figure candidates selected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
