#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from review_writer_core.figure_qualification import (
    argument_role,
    candidate_exclusion_reasons,
    candidate_qualification,
    figure_score,
    representative_role,
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def lower(text: Any) -> str:
    return norm(text).lower()


def inventory_by_paper(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(paper.get("paper_id")): paper
        for paper in inventory.get("papers", [])
        if isinstance(paper, dict) and paper.get("paper_id")
    }


def paragraph_anchors(drafts: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map papers actually cited in the generated manuscript to insertion anchors."""
    anchors: dict[str, dict[str, str]] = {}
    for section in drafts.get("sections", []) if isinstance(drafts, dict) else []:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs", []):
            if not isinstance(paragraph, dict):
                continue
            paragraph_id = str(paragraph.get("paragraph_id") or "").strip()
            if not paragraph_id:
                continue
            paper_ids = paragraph.get("cited_paper_ids") or [paragraph.get("paper_id")]
            if not isinstance(paper_ids, list):
                paper_ids = [paper_ids]
            for raw_paper_id in paper_ids:
                paper_id = str(raw_paper_id or "").strip()
                if paper_id:
                    anchors.setdefault(
                        paper_id,
                        {
                            "paragraph_id": paragraph_id,
                            "target_paragraph_id": paragraph_id,
                            "section_id": str(section.get("section_id") or ""),
                            "section_heading": str(section.get("heading") or ""),
                        },
                    )
    return anchors


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
            "why_selected": "Highest-ranked overview, mechanism, scope, or reaction scheme candidate from the MinerU inventory.",
            "manuscript_selected": False,
            "resolution_status": "ready" if best.get("source_image_path") else "needs_source_resolution",
            "needs_human_check": True,
        }
    )
    return best


def best_redrawable_candidate_index(candidates: list[dict[str, Any]]) -> int | None:
    """Choose the best candidate that passes the hard minimum qualification."""
    redrawable = [
        candidate
        for candidate in candidates
        if candidate.get("source_image_path")
        and bool(
            (
                candidate.get("candidate_qualification")
                if isinstance(candidate.get("candidate_qualification"), dict)
                else candidate_qualification(candidate)
            ).get("eligible")
        )
    ]
    if not redrawable:
        return None
    return int(max(redrawable, key=lambda candidate: (candidate.get("score", 0), -candidate.get("candidate_index", 0))).get("candidate_index"))


def build_default_figure_reviews(paper_level: dict[str, Any], reviewed_at: str) -> dict[str, Any]:
    """Record deterministic defaults so Figure Review is ready before a user changes one."""
    reviews: dict[str, dict[str, Any]] = {}
    for paper in paper_level.get("papers", []) if isinstance(paper_level, dict) else []:
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("paper_id") or "")
        selected_index = paper.get("selected_candidate_index")
        if not paper_id or not isinstance(selected_index, int):
            continue
        candidate = next(
            (item for item in paper.get("candidates", []) if isinstance(item, dict) and item.get("candidate_index") == selected_index),
            None,
        )
        if not isinstance(candidate, dict):
            continue
        reviews[paper_id] = {
            "selected_candidate_index": selected_index,
            "selected_source_image_path": str(candidate.get("source_image_path") or ""),
            "review_note": "Automatically selected as the highest-scoring redrawable candidate.",
            "selection_source": "automatic_top_score",
            "reviewed_at": reviewed_at,
        }
    return {"source": "automatic_top_score", "generated_at": reviewed_at, "papers": reviews}


def build_outputs(project: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inventory = read_json(project / "02_section_drafting" / "paper_figure_inventory.json")
    tasks = read_json(project / "02_section_drafting" / "section_tasks.json")
    anchors = paragraph_anchors(read_json(project / "02_section_drafting" / "section_drafts.json"))
    by_paper = inventory_by_paper(inventory)

    paper_rows: list[dict[str, Any]] = []
    for paper in inventory.get("papers", []):
        if isinstance(paper, dict):
            paper_id = str(paper.get("paper_id") or "")
            if paper_id not in anchors:
                continue
            choices = [dict(candidate) for candidate in paper.get("top_candidates", []) if isinstance(candidate, dict)]
            for index, candidate in enumerate(choices):
                candidate["candidate_index"] = index
                qualification = candidate_qualification(candidate)
                candidate["score"] = int(qualification.get("score") or 0)
                candidate["resolution_status"] = "ready" if candidate.get("source_image_path") else "needs_source_resolution"
                candidate["needs_human_check"] = True
                candidate["exclusion_reasons"] = candidate_exclusion_reasons(candidate)
                candidate["argument_role"] = argument_role(candidate)
                candidate["representative_role"] = representative_role(candidate)
                candidate["candidate_qualification"] = qualification
                candidate["automatic_selection_eligible"] = bool(
                    candidate["candidate_qualification"].get("eligible")
                )
                candidate.update(anchors[paper_id])
            selected_index = best_redrawable_candidate_index(choices)
            paper_rows.append({
                "paper_id": paper.get("paper_id"),
                "title": paper.get("title"),
                "candidates": choices,
                "selected_candidate_index": selected_index,
                "status": "auto_selected" if selected_index is not None else ("no_qualified_figure" if choices else "no_useful_figure"),
                "no_useful_figure_reason": "No candidate passed the minimum scientific figure qualification; the paper is allowed to remain without a figure." if choices and selected_index is None else ("No image/table candidates were found in MinerU content_list." if not choices else ""),
            })

    manuscript: list[dict[str, Any]] = []
    used_keys: set[tuple[str, str]] = set()
    task_rows = tasks if isinstance(tasks, list) else []
    eligible_sections = [
        section
        for section in task_rows
        if isinstance(section, dict)
        and lower(section.get("figure_need")) not in {"no", "none", "optional"}
    ]
    global_budget = min(8, max(3, len(eligible_sections)))
    for section in eligible_sections:
        allowed = [str(pid) for pid in section.get("allowed_papers", [])]
        pool: list[dict[str, Any]] = []
        for paper_id in allowed:
            if paper_id not in anchors:
                continue
            paper = by_paper.get(paper_id)
            if not paper:
                continue
            for candidate in paper.get("top_candidates", []):
                if isinstance(candidate, dict):
                    row = dict(candidate)
                    qualification = candidate_qualification(row, section=section)
                    if not qualification.get("eligible"):
                        continue
                    row["_score"] = int(qualification.get("score") or 0)
                    row["candidate_qualification"] = qualification
                    row["automatic_selection_eligible"] = True
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
                    "recommended_action": (
                        "redraw"
                        if not (candidate.get("source_type") == "table" and not candidate.get("source_image_path"))
                        else "retable"
                    ),
                    "manuscript_selected": True,
                    "resolution_status": "ready" if candidate.get("source_image_path") else "needs_source_resolution",
                    "needs_human_check": True,
                    "argument_role": argument_role(candidate),
                    "representative_role": representative_role(candidate),
                    "exclusion_reasons": [],
                    "candidate_qualification": candidate.get(
                        "candidate_qualification"
                    )
                    or candidate_qualification(candidate, section=section),
                    "automatic_selection_eligible": True,
                }
            )
            candidate.update(anchors.get(str(candidate.get("paper_id") or ""), {}))
            manuscript.append(candidate)
            if section_selected >= 1 or len(manuscript) >= global_budget:
                break
        if len(manuscript) >= global_budget:
            break
    return {"project_id": project.name, "papers": paper_rows}, manuscript


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select initial paper-level and manuscript figure candidates.")
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    paper_level, manuscript = build_outputs(project)
    out_dir = project / "02_section_drafting"
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    write_json(out_dir / "paper_figure_candidates.json", paper_level)
    write_json(out_dir / "figure_candidates.json", manuscript)
    write_json(out_dir / "human_figure_review.json", build_default_figure_reviews(paper_level, generated_at))
    print(f"Wrote {out_dir / 'paper_figure_candidates.json'} ({len(paper_level['papers'])} papers)")
    print(f"Wrote {out_dir / 'figure_candidates.json'} ({len(manuscript)} records)")
    if not manuscript:
        print("No argument-relevant manuscript figure candidates were selected; continuing without source figures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
