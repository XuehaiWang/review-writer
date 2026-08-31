"""Shared figure-candidate qualification and output-state semantics."""

from __future__ import annotations

import re
from typing import Any


MINIMUM_AUTOMATIC_FIGURE_SCORE = 4


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _lower(value: object) -> str:
    return _norm(value).casefold()


def candidate_exclusion_reasons(candidate: dict[str, Any]) -> list[str]:
    """Return hard exclusions that ranking must never override."""

    caption = _lower(candidate.get("source_caption_text"))
    label = _lower(candidate.get("source_label"))
    source_type = _lower(candidate.get("source_type"))
    searchable = f"{label} {caption}"
    reasons: list[str] = []
    if source_type == "table" or re.search(r"\btable\s+\d+", label):
        reasons.append("table_or_optimization_screenshot")
    if re.search(
        r"\b(?:apparatus|experimental setup|reaction vessel|reflux setup|photograph|photo of)\b",
        searchable,
    ):
        reasons.append("apparatus_or_decorative_photo")
    if re.search(
        r"\b(?:condition screening|optimization table|screening results)\b",
        searchable,
    ):
        reasons.append("optimization_screenshot")
    if len(caption) > 900 or re.search(
        r"(?:article recommendations?|read online|cite this|\\mathrm|\\textit|�)",
        caption,
    ):
        reasons.append("caption_corrupt_or_unresolved")
    if not _norm(candidate.get("paper_id")):
        reasons.append("source_identity_unresolved")
    if not _norm(
        candidate.get("source_image_artifact_id")
        or candidate.get("source_image_path")
        or candidate.get("source_path")
        or candidate.get("image_path")
    ):
        reasons.append("source_artifact_unresolved")
    return list(dict.fromkeys(reasons))


def argument_role(candidate: dict[str, Any]) -> str:
    text = _lower(
        " ".join(
            [
                str(candidate.get("source_label") or ""),
                str(candidate.get("source_caption_text") or ""),
            ]
        )
    )
    if "mechanism" in text or "catalytic cycle" in text:
        return "mechanism_evidence"
    if "scope" in text or "substrate" in text or "population" in text:
        return "representative_scope"
    if "overview" in text or "classification" in text:
        return "overview_classification"
    return "core_transformation_or_framework"


def representative_role(candidate: dict[str, Any]) -> str:
    return {
        "mechanism_evidence": "mechanism_model",
        "representative_scope": "scope_samples",
        "overview_classification": "conceptual_overview",
        "core_transformation_or_framework": "core_transformation",
    }[argument_role(candidate)]


def figure_score(
    candidate: dict[str, Any], section: dict[str, Any] | None = None
) -> int:
    """Compute the deterministic scientific-utility score.

    Hard exclusions are intentionally not encoded as a large negative number;
    callers must check ``candidate_qualification`` so an excluded item can
    never win merely because every other candidate is worse.
    """

    caption = _lower(candidate.get("source_caption_text"))
    label = _lower(candidate.get("source_label"))
    score = int(candidate.get("inventory_score") or candidate.get("score") or 0)
    if "scheme" in label or "scheme" in caption:
        score += 8
    if "mechanism" in caption or "catalytic cycle" in caption:
        score += 10
    if "scope" in caption:
        score += 4
    if "optimization" in caption:
        score -= 5
    if "gram-scale" in caption or "control experiment" in caption:
        score -= 2
    if section:
        section_text = _lower(
            " ".join(
                [str(section.get("heading") or ""), str(section.get("core_argument") or "")]
            )
        )
        if "radical" in section_text and (
            "radical" in caption or "photoredox" in caption
        ):
            score += 6
        if "stereo" in section_text and any(
            value in caption for value in ("stereo", "enantio", "chiral")
        ):
            score += 5
        if "carbonates" in section_text and "carbonate" in caption:
            score += 4
        if "mechan" in section_text and "mechanism" in caption:
            score += 4
    if _norm(
        candidate.get("source_image_artifact_id")
        or candidate.get("source_image_path")
        or candidate.get("source_path")
        or candidate.get("image_path")
    ):
        score += 4
    return score


def candidate_qualification(
    candidate: dict[str, Any],
    *,
    section: dict[str, Any] | None = None,
    minimum_score: int = MINIMUM_AUTOMATIC_FIGURE_SCORE,
) -> dict[str, Any]:
    reasons = candidate_exclusion_reasons(candidate)
    score = figure_score(candidate, section)
    if score < int(minimum_score):
        reasons.append("below_minimum_score")
    eligible = not reasons
    return {
        "eligible": eligible,
        "score": score,
        "minimum_score": int(minimum_score),
        "reasons": list(dict.fromkeys(reasons)),
        "selection_policy": "minimum_scientific_figure_candidate_v1",
    }


def figure_output_state(row: dict[str, Any]) -> str:
    """Return a truthful semantic state without changing legacy storage flags."""

    approval = row.get("human_approval")
    approval = approval if isinstance(approval, dict) else {}
    approved = str(approval.get("status") or "") == "approved"
    render_mode = _lower(row.get("render_mode") or row.get("mode"))
    manual = bool(
        render_mode in {"manual-svg", "manual-arrow-edit"}
        or row.get("manual_edit")
        or row.get("manual_arrow_edit")
    )
    source_original = bool(
        row.get("source_preserved")
        or render_mode == "source-original"
        or (
            row.get("source_artifact_id")
            and row.get("source_artifact_id") == row.get("output_artifact_id")
        )
    )
    has_output = bool(
        row.get("output_artifact_id")
        or row.get("redrawn_image")
        or row.get("redrawn_image_url")
    )
    ai_redrawn = bool(row.get("ai_redraw_performed")) or bool(
        has_output and not source_original and not manual
    )
    if manual:
        state = "manually_edited"
    elif source_original:
        state = "source_original"
    elif ai_redrawn:
        state = "ai_redrawn"
    else:
        return "failed"
    return f"approved_{state}" if approved else state


def figure_source_kind(row: dict[str, Any]) -> str:
    if _norm(row.get("overview_role")) or _norm(row.get("review_generated")):
        return "review_generated"
    supporting = [
        str(value)
        for value in row.get("supporting_paper_ids") or []
        if str(value).strip()
    ]
    if len(set(supporting)) > 1:
        return "multi_paper_synthesis"
    return "source_paper"

