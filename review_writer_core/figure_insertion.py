"""Deterministic insertion planning for a reviewed paper-level figure pool."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


ROLE_PRIORITY = {
    "core_transformation": 0,
    "mechanism_model": 1,
    "comparison_ablation": 2,
    "scope_samples": 3,
    "quantitative_results": 4,
    "conceptual_overview": 5,
    "structure_image": 6,
    "workflow": 7,
    "unknown": 8,
    # Backward-compatible role names from older candidate artifacts.
    "mechanism": 1,
    "scope": 3,
    "paper_overview": 5,
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _section_id(row: dict[str, Any]) -> str:
    explicit = _text(row.get("section_id"))
    if explicit:
        return explicit
    paragraph_id = _text(row.get("target_paragraph_id"))
    return paragraph_id.split("-p", 1)[0] if "-p" in paragraph_id else ""


def build_figure_insertion_plan(
    figures: Iterable[dict[str, Any]], *, max_per_section: int = 2
) -> list[dict[str, Any]]:
    """Choose a conservative manuscript subset without discarding pool assets.

    Every input receives an auditable decision.  At most one figure is placed
    after a paragraph, at most one per source paper is used, and each section
    receives no more than ``max_per_section`` figures.
    """

    rows = [dict(row) for row in figures if isinstance(row, dict)]
    ranked = sorted(
        rows,
        key=lambda row: (
            _section_id(row) or "~",
            ROLE_PRIORITY.get(_text(row.get("representative_role")), 8),
            _text(row.get("figure_id")),
        ),
    )
    section_counts: defaultdict[str, int] = defaultdict(int)
    used_paragraphs: set[str] = set()
    used_papers: set[str] = set()
    decisions: dict[str, dict[str, Any]] = {}

    for row in ranked:
        figure_id = _text(row.get("figure_id"))
        paper_id = _text(row.get("paper_id"))
        paragraph_id = _text(row.get("target_paragraph_id"))
        section_id = _section_id(row)
        included = False
        reason = ""
        qualification = row.get("candidate_qualification")
        qualification = qualification if isinstance(qualification, dict) else {}
        automatically_ineligible = (
            bool(row.get("qualification_enforced"))
            and bool(
                row.get("automatic_selection_eligible") is False
                or qualification.get("eligible") is False
            )
            and _text(row.get("selection_source")) != "human"
        )
        if row.get("manuscript_selected") is False:
            reason = "user_excluded"
        elif automatically_ineligible:
            reason = "candidate_below_minimum_qualification"
        elif not bool(row.get("usable")):
            reason = "output_not_usable"
        elif not paragraph_id or not section_id:
            reason = "no_supported_paragraph"
        elif paragraph_id in used_paragraphs:
            reason = "duplicate_paragraph"
        elif paper_id and paper_id in used_papers:
            reason = "duplicate_paper"
        elif section_counts[section_id] >= max(1, int(max_per_section)):
            reason = "section_figure_limit"
        else:
            included = True
            section_counts[section_id] += 1
            used_paragraphs.add(paragraph_id)
            if paper_id:
                used_papers.add(paper_id)

        decisions[figure_id] = {
            "figure_id": figure_id,
            "paper_id": paper_id,
            "section_id": section_id,
            "target_paragraph_id": paragraph_id,
            "representative_role": _text(row.get("representative_role")) or "unknown",
            "figure_source_kind": _text(row.get("figure_source_kind"))
            or "source_paper",
            "output_state": _text(row.get("output_state")),
            "placement_basis": (
                "first_supported_paper_discussion"
                if paragraph_id
                else "paper_pool_only"
            ),
            "visible_callout_required": included,
            "adjacent_interpretation_required": included,
            "candidate_qualification": qualification,
            "include": included,
            "skip_reason": reason,
        }

    return [
        decisions[_text(row.get("figure_id"))]
        for row in rows
        if _text(row.get("figure_id")) in decisions
    ]
