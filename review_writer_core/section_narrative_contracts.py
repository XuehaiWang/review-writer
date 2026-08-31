"""Pure narrative contracts derived from Blueprint and Matrix evidence.

The functions in this module are deliberately provider- and storage-agnostic.
They extend the existing Blueprint rather than publishing another mutable
workflow state: thesis quality, target depth, paragraph-role coverage, and
comparison coverage are all derived from current inputs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping


CANONICAL_PARAGRAPH_ROLES: tuple[str, ...] = (
    "section_frame",
    "anchor_case",
    "method_extension",
    "cross_study_comparison",
    "mechanism_boundary",
    "scope_limitation",
    "section_synthesis_exit",
)

_ROLE_ALIASES = {
    "definition": "section_frame",
    "foundation": "anchor_case",
    "reported_evidence": "anchor_case",
    "extension": "method_extension",
    "comparison": "cross_study_comparison",
    "mechanism": "mechanism_boundary",
    "limitation": "scope_limitation",
    "synthesis": "section_synthesis_exit",
    "transition": "section_synthesis_exit",
}

_OBJECT_FIELDS = {
    "object_input",
    "research_object",
    "input",
    "substrate",
    "population",
    "material",
}
_METHOD_FIELDS = {
    "method_conditions",
    "method",
    "intervention",
    "transformation",
    "catalyst",
    "strategy",
}
_OUTCOME_FIELDS = {
    "quantitative_results",
    "outcome",
    "result",
    "scope",
    "selectivity",
    "performance",
}
_LIMIT_FIELDS = {"limitations", "limitation", "boundary", "constraints"}
_COMPARISON_FIELDS = (
    "method_conditions",
    "quantitative_results",
    "scope",
    "limitations",
    "mechanism",
)


def _compact(value: Any, *, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _unique(values: Iterable[Any], *, limit: int = 4) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact(value)
        key = text.casefold()
        if not text or key in seen:
            continue
        result.append(text)
        seen.add(key)
        if len(result) >= limit:
            break
    return result


def _paper_ids(section: Mapping[str, Any]) -> list[str]:
    return _unique(
        [
            *(section.get("primary_papers") or []),
            *(section.get("major_papers") or []),
            *(section.get("paper_ids") or []),
        ],
        limit=10000,
    )


def _source_backed_facts(
    paper_ids: Iterable[str], rows_by_id: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, list[str]], set[str]]:
    values: dict[str, list[str]] = defaultdict(list)
    supported_papers: set[str] = set()
    for paper_id in paper_ids:
        row = rows_by_id.get(str(paper_id)) or {}
        for fact in row.get("scientific_facts") or []:
            if not isinstance(fact, dict):
                continue
            field_id = _compact(fact.get("field_id"), limit=80).casefold()
            value = _compact(fact.get("value"))
            if not field_id or not value or not fact.get("evidence_refs"):
                continue
            if str(fact.get("support_level") or "").casefold() in {
                "coverage_only",
                "neighbor_context",
                "context_only",
            }:
                continue
            values[field_id].append(value)
            supported_papers.add(str(paper_id))
    return dict(values), supported_papers


def derive_scientific_thesis(
    section: Mapping[str, Any],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    classification_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive a bounded section thesis from source-addressable Matrix facts.

    The result states what the selected evidence contains and what comparison
    may establish. It never turns a retrieval miss into a scientific absence.
    """

    role = _compact(section.get("section_role"), limit=40).casefold() or "body"
    fallback = _compact(section.get("purpose") or section.get("section_thesis"))
    papers = _paper_ids(section)
    if role != "body":
        return {
            "text": fallback,
            "status": "structural_synthesis",
            "evidence_scope": papers,
            "source": "blueprint_structure",
            "components": {},
            "missing_components": [],
        }

    facts, supported_papers = _source_backed_facts(papers, rows_by_id)
    object_values = _unique(
        value
        for field, values in facts.items()
        if field in _OBJECT_FIELDS
        for value in values
    )
    method_values = _unique(
        value
        for field, values in facts.items()
        if field in _METHOD_FIELDS
        for value in values
    )
    outcome_values = _unique(
        value
        for field, values in facts.items()
        if field in _OUTCOME_FIELDS
        for value in values
    )
    limit_values = _unique(
        value
        for field, values in facts.items()
        if field in _LIMIT_FIELDS
        for value in values
    )

    contract = classification_contract or {}
    axis_values: list[str] = []
    primary_axis = _compact(
        contract.get("primary_axis_label")
        or contract.get("primary_axis_id")
        or contract.get("primary_axis")
    )
    if primary_axis:
        axis_values.append(primary_axis.replace("_", " "))
    secondary_axes = list(contract.get("secondary_axes") or [])
    secondary_axes.extend(
        axis
        for axis in contract.get("axes") or []
        if isinstance(axis, dict)
        and str(axis.get("axis_id") or "") != str(contract.get("primary_axis_id") or "")
    )
    for axis in secondary_axes:
        if isinstance(axis, dict):
            axis = axis.get("label") or axis.get("axis_id")
        axis_text = _compact(axis)
        if axis_text:
            axis_values.append(axis_text.replace("_", " "))
    axis_values.extend(field.replace("_", " ") for field in _COMPARISON_FIELDS if facts.get(field))
    comparison_axes = _unique(axis_values, limit=5)

    missing: list[str] = []
    if not object_values:
        missing.append("research_object_or_input")
    if not method_values:
        missing.append("shared_method_or_problem")
    if not comparison_axes:
        missing.append("comparison_axis")
    if not outcome_values:
        missing.append("supported_shared_understanding")

    subject = "; ".join(object_values[:2]) or _compact(section.get("title"))
    method = "; ".join(method_values[:2]) or "the methods represented in the selected evidence"
    outcomes = "; ".join(outcome_values[:2])
    axes = ", ".join(comparison_axes[:3]) or "the available source-backed dimensions"
    boundary = (
        "; ".join(limit_values[:2])
        if limit_values
        else "conditions, objects, or outcomes not represented by source-backed facts"
    )
    if outcomes:
        text = (
            f"For {subject}, the selected evidence links {method} with reported findings "
            f"including {outcomes}. Comparison across {axes} can establish shared patterns "
            f"only within the reported evidence; conclusions beyond {boundary} remain provisional."
        )
    else:
        text = (
            f"For {subject}, the selected evidence documents {method}. The section should test "
            f"comparability across {axes}, while conclusions beyond {boundary} remain provisional "
            "until the missing outcome evidence is retrieved."
        )
    status = (
        "evidence_grounded"
        if len(supported_papers) >= min(2, max(1, len(papers))) and not missing
        else "provisional"
    )
    return {
        "text": text,
        "status": status,
        "evidence_scope": papers,
        "source": "matrix_source_backed_facts",
        "components": {
            "research_objects": object_values,
            "shared_methods_or_problems": method_values,
            "comparison_axes": comparison_axes,
            "supported_findings": outcome_values,
            "unsupported_boundary": boundary,
            "source_backed_paper_ids": sorted(supported_papers),
        },
        "missing_components": missing,
    }


def _target_range(value: Any) -> tuple[int, int]:
    if isinstance(value, (int, float)) and int(value) > 0:
        target = int(value)
        return max(300, round(target * 0.8)), max(500, round(target * 1.25))
    numbers = [int(number) for number in re.findall(r"\d+", str(value or ""))]
    if len(numbers) >= 2:
        low, high = sorted(numbers[:2])
        return max(300, low), max(low, high)
    if numbers:
        return _target_range(numbers[0])
    return 0, 0


def derive_section_depth_contract(section: Mapping[str, Any]) -> dict[str, Any]:
    """Return a measurable, non-prescriptive depth target for one section."""

    role = _compact(section.get("section_role"), limit=40).casefold() or "body"
    paper_count = len(_paper_ids(section))
    current_min, current_max = _target_range(section.get("target_words"))
    if role in {"introduction", "conclusion"}:
        paragraph_count = 5
        default_min, default_max = 700, 1150
        minimum_comparisons = 0 if role == "introduction" else 1
    elif paper_count <= 1:
        paragraph_count = 4
        default_min, default_max = 650, 1000
        minimum_comparisons = 0
    elif paper_count == 2:
        paragraph_count = 5
        default_min, default_max = 800, 1250
        minimum_comparisons = 1
    elif paper_count <= 4:
        paragraph_count = 6
        default_min, default_max = 1000, 1550
        minimum_comparisons = 2
    else:
        paragraph_count = min(9, 6 + (paper_count - 3) // 2)
        default_min = min(1800, 1100 + (paper_count - 4) * 100)
        default_max = min(2600, default_min + 650)
        minimum_comparisons = 2
    return {
        "target_paragraph_count": paragraph_count,
        "target_word_min": current_min or default_min,
        "target_word_max": current_max or default_max,
        "minimum_comparison_paragraphs": minimum_comparisons,
        "requires_section_synthesis_exit": role in {"body", "conclusion"},
        "required_paragraph_roles": (
            ["section_frame"]
            if role == "introduction"
            else ["section_synthesis_exit"]
            if role == "conclusion"
            else [
                "section_frame",
                "anchor_case",
                *(
                    ["cross_study_comparison"]
                    if minimum_comparisons
                    else []
                ),
                "section_synthesis_exit",
            ]
        ),
        "paper_count": paper_count,
        "diagnostic_policy": "derived_not_hard_word_quota",
    }


def canonical_argument_role(
    value: Any,
    *,
    claim_kinds: Iterable[Any] = (),
    paper_count: int = 0,
    paragraph_index: int = 0,
    paragraph_count: int = 0,
    section_role: str = "body",
) -> str:
    """Map legacy/model paragraph labels onto the P1 narrative vocabulary."""

    raw = _compact(value, limit=80).casefold().replace("-", "_").replace(" ", "_")
    if raw in CANONICAL_PARAGRAPH_ROLES:
        role = raw
    else:
        role = _ROLE_ALIASES.get(raw, "")
    kinds = {str(kind or "").strip() for kind in claim_kinds}
    if "mechanism_interpretation" in kinds:
        role = "mechanism_boundary"
    elif paper_count > 1 and kinds & {"cross_study_comparison", "review_synthesis"}:
        role = "cross_study_comparison"
    if paragraph_count and paragraph_index == paragraph_count - 1 and role in {
        "section_synthesis_exit",
        "",
    }:
        return "section_synthesis_exit"
    if paragraph_index == 0 and role in {"", "section_synthesis_exit"}:
        return "section_frame"
    if role:
        return role
    if str(section_role or "body") == "conclusion":
        return "section_synthesis_exit"
    return "cross_study_comparison" if paper_count > 1 else "anchor_case"


def derive_narrative_diagnostics(
    writing_section: Mapping[str, Any],
    depth_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure paragraph-role and comparison coverage from a Writing Plan."""

    contract = depth_contract or {}
    paragraphs = [
        paragraph
        for paragraph in writing_section.get("paragraphs") or []
        if isinstance(paragraph, dict)
    ]
    roles = [
        canonical_argument_role(
            paragraph.get("argument_role"),
            paragraph_index=index,
            paragraph_count=len(paragraphs),
            section_role=str(writing_section.get("section_role") or "body"),
        )
        for index, paragraph in enumerate(paragraphs)
    ]
    required = [str(role) for role in contract.get("required_paragraph_roles") or []]
    missing = [role for role in required if role not in roles]
    comparison_count = roles.count("cross_study_comparison")
    minimum_comparisons = int(contract.get("minimum_comparison_paragraphs") or 0)
    if comparison_count < minimum_comparisons:
        missing.append("cross_study_comparison_quota")
    if contract.get("requires_section_synthesis_exit") and (
        not roles or roles[-1] != "section_synthesis_exit"
    ):
        missing.append("section_synthesis_exit_position")
    return {
        "status": "complete" if not missing else "shallow",
        "paragraph_count": len(paragraphs),
        "target_paragraph_count": int(contract.get("target_paragraph_count") or 0),
        "paragraph_roles": roles,
        "comparison_paragraph_count": comparison_count,
        "minimum_comparison_paragraphs": minimum_comparisons,
        "missing_requirements": list(dict.fromkeys(missing)),
    }
