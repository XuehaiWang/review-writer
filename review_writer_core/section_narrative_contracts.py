"""Pure narrative contracts derived from Blueprint and Matrix evidence.

The functions in this module are deliberately provider- and storage-agnostic.
They extend the existing Blueprint rather than publishing another mutable
workflow state: thesis quality, target depth, paragraph-role coverage, and
comparison coverage are all derived from current inputs.
"""

from __future__ import annotations

import re
import hashlib
import json
from collections import defaultdict
from copy import deepcopy
from typing import Any, Iterable, Mapping
from review_writer_core.scientific_facts import fact_is_usable
from review_writer_core.paragraph_markers import parse_marked_paragraphs


def build_argument_execution(blueprint, writing_plan, section_index, matrix, *, draft_text=None):
    """Derive downstream inputs from realized prose, never provisional theses.

    This is an observable binding check, not a semantic quality score. A manual
    edit invalidates old sentence bindings without changing the author's text.
    The result belongs inside existing synthesis/quality artifacts.
    """
    from review_writer_core.claim_contracts import argument_projection, claim_is_executable
    from review_writer_core.stages.sections.source_writing import CONTRACT as SOURCE_CONTRACT, source_check_current

    def rows(value, key):
        return [row for row in (value or {}).get(key) or [] if isinstance(row, dict)]

    def normalized(value):
        return re.sub(r"\s+", " ", str(value or "")).strip()

    facts = {(str(row.get("paper_id")), str(fact.get("fact_id"))): fact
             for row in rows(matrix, "rows") or rows(matrix, "papers")
             for fact in row.get("scientific_facts") or [] if isinstance(fact, dict)}
    plans = {str(row.get("section_id")): row for row in rows(writing_plan, "sections")}
    bodies = {str(row.get("section_id")): row for row in rows(section_index, "sections")}
    current = {row["paragraph_id"]: row["text"] for row in parse_marked_paragraphs(draft_text or "")}
    findings, output, seen = [], [], {}
    for section in rows(blueprint, "sections"):
        sid = str(section.get("section_id") or "")
        plan, body = plans.get(sid, {}), bodies.get(sid, {})
        claims = {str(row.get("claim_id")): row for row in rows(plan, "claims")}
        declared = {str(row.get("claim_id")): row for row in rows(section, "scientific_claims")}
        realized = []
        for paragraph in rows(body, "paragraphs"):
            pid = str(paragraph.get("paragraph_id") or "")
            prose = normalized(current.get(pid, "") if draft_text is not None else paragraph.get("text"))
            for item in rows(paragraph, "claim_realizations"):
                cid, sentence = str(item.get("claim_id") or ""), normalized(item.get("text"))
                if not sentence or sentence not in prose:
                    findings.append({"code": "claim_binding_not_current", "section_id": sid,
                                     "paragraph_id": pid, "claim_id": cid})
                    continue
                claim = claims.get(cid, {})
                source_argument = declared.get(cid, {})
                if plan.get("evidence_mode") != SOURCE_CONTRACT and source_argument.get("argument_basis") and (not claim_is_executable(source_argument)
                        or argument_projection(claim) != argument_projection(source_argument)):
                    findings.append({"code": "argument_version_not_current", "section_id": sid,
                                     "paragraph_id": pid, "claim_id": cid})
                    continue
                papers = list(dict.fromkeys(str(v) for v in item.get("citation_group") or claim.get("citation_group") or []))
                ids = list(dict.fromkeys(str(v) for v in item.get("fact_ids") or claim.get("fact_ids") or []))
                invalid = [fid for fid in ids if not any(fact_is_usable(facts.get((paper, fid), {})) for paper in papers)]
                if invalid:
                    findings.append({"code": "claim_fact_not_current", "section_id": sid,
                                     "paragraph_id": pid, "claim_id": cid, "fact_ids": invalid})
                    continue
                refs = item.get("evidence_refs") or claim.get("evidence_refs") or []
                if not papers or not refs:
                    continue
                source_bound = plan.get("evidence_mode") == SOURCE_CONTRACT
                if source_bound:
                    if not source_check_current(claim, text=sentence):
                        findings.append({"code": "source_claim_check_not_current", "section_id": sid,
                                         "paragraph_id": pid, "claim_id": cid})
                        continue
                record = {"section_id": sid, "section_title": str(section.get("title") or sid),
                          "paragraph_id": pid, "claim_id": cid, "claim": sentence,
                          "paper_ids": papers, "fact_ids": ids, "evidence_refs": refs,
                          "binding_level": "source_passage" if source_bound else "fact" if ids else "legacy_source",
                          "source_verification": claim.get("source_verification"),
                          "result_context": claim.get("result_context") or [],
                          "claim_kind": str(claim.get("claim_kind") or ""),
                          "claim_revision": claim.get("claim_revision", 1),
                          "argument_basis": claim.get("argument_basis"),
                          "assertion_ceiling": str(claim.get("assertion_ceiling") or "")}
                realized.append(record)
                # A repeated paper or even a repeated fact is legitimate when
                # its analytical contribution differs. Only identical source
                # sets AND identical claims produce this conservative finding.
                identity = (tuple(sorted(papers)), tuple(sorted(ids)), sentence.casefold())
                previous = seen.get(identity)
                if previous and previous["section_id"] != sid:
                    findings.append({"code": "repeated_claim_across_sections", "section_id": sid,
                                     "paragraph_id": pid, "other_paragraph_id": previous["paragraph_id"],
                                     "claim_id": cid})
                seen[identity] = record
        realized_ids = {item["claim_id"] for item in realized}
        for claim in declared.values():
            if plan.get("evidence_mode") != SOURCE_CONTRACT and claim.get("argument_basis") and claim.get("required_for_section") and claim["claim_id"] not in realized_ids:
                findings.append({"code": "core_argument_not_realized", "section_id": sid, "claim_id": claim["claim_id"],
                    "paragraph_id": next((p.get("paragraph_id", "") for p in rows(plan, "paragraphs")
                                          if claim["claim_id"] in (p.get("claim_ids") or [])), "")})
        output.append({"section_id": sid, "title": str(section.get("title") or sid),
                       "section_role": str(section.get("section_role") or "body"),
                       "scientific_question": section.get("review_problem") or section.get("scientific_question") or "",
                       "provisional_thesis": section.get("scientific_thesis") or section.get("section_thesis") or "",
                       "claims": realized,
                       "status": "realized_bindings" if realized else "no_current_claim_bindings"})
    value = {"contract": "argument-execution/1", "sections": output, "findings": findings,
             "semantic_quality_verified": False}
    used = {(paper, fid) for section in output for claim in section["claims"]
            for paper in claim["paper_ids"] for fid in claim["fact_ids"] if (paper, fid) in facts}
    fact_inputs = [{key: facts[identity].get(key) for key in (
        "fact_id", "value", "subject", "experiment_id", "qualifiers", "evidence_refs", "support_level", "assertion_ceiling")}
        for identity in sorted(used)]
    value["input_fingerprint"] = hashlib.sha256(json.dumps(
        [value, fact_inputs],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return value


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


def apply_single_paper_policy(section: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a sparse body category into a bounded case analysis without rerouting evidence."""
    result = deepcopy(dict(section))
    papers = _paper_ids(section)
    if str(section.get("section_role") or "body").casefold() != "body" or len(papers) != 1:
        return result
    instruction = (
        f"Analyze the source-verified question, method, findings and limitations of {papers[0]} "
        "as a bounded research case. Compare experiments within the study only where evidence permits; "
        "cross-study comparisons require separately cited supporting evidence. Do not describe this "
        "single primary study as independent replication, field-wide consensus or general applicability. "
        "Missing facts remain evidence gaps, not evidence that the source reported no result. "
        "These source limits take precedence over generic synthesis or comparison requests."
    )
    justification = str(section.get("single_paper_justification") or "").strip() or (
        f"The selected category {section.get('title') or section.get('section_id') or ''} has one "
        f"primary study ({papers[0]}). Retain its selected scientific scope as a bounded case "
        "analysis instead of inferring equivalence to an adjacent category from paper count."
    )
    result["single_paper_justification"] = justification
    previous_instruction = (section.get("single_paper_policy") or {}).get("instruction")
    policy_instructions = {instruction, previous_instruction} - {None, ""}
    requirement_id = f"WR-{section.get('section_id') or 'section'}-single-source"
    result["single_paper_policy"] = {
        "mode": "source_bounded_case_analysis", "primary_paper_id": papers[0],
        "requires_user_action": False, "requirement_id": requirement_id,
    }
    requirements = []
    for item in result.get("writing_requirements") or []:
        if isinstance(item, dict):
            if item.get("source") == "single_paper_policy":
                continue
            if item.get("source") in {"native_blueprint", "legacy_blueprint_script"}:
                if item.get("type") == "cross_study_synthesis" and not section.get("supporting_papers"):
                    continue
                if item.get("type") == "source_bounded_case_analysis" and item.get("instruction") in policy_instructions:
                    continue
        requirements.append(item)
    requirements.append({
        "requirement_id": requirement_id,
        "type": "evidence_boundary", "instruction": instruction, "source": "single_paper_policy",
    })
    result["writing_requirements"] = requirements
    # Clean copies written by the old policy without replacing the chapter's synthesis objective.
    if "avoid_patterns" in result:
        result["avoid_patterns"] = [item for item in result["avoid_patterns"] or [] if item not in policy_instructions]
    contract = result.get("academic_contract")
    if isinstance(contract, dict) and contract.get("expected_synthesis") in policy_instructions:
        from review_writer_core.academic_contracts import section_academic_contract

        contract["expected_synthesis"] = section_academic_contract(result)["expected_synthesis"]
    return result


def _source_backed_facts(
    paper_ids: Iterable[str], rows_by_id: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, list[str]], set[str]]:
    values: dict[str, list[str]] = defaultdict(list)
    supported_papers: set[str] = set()
    for paper_id in paper_ids:
        row = rows_by_id.get(str(paper_id)) or {}
        for fact in row.get("scientific_facts") or []:
            if not isinstance(fact, dict) or not fact_is_usable(fact, purpose="detail"):
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
    if len(papers) == 1:
        text = (
            f"For {subject}, analyze the reported methods ({method}) and findings of {papers[0]} as a bounded "
            f"research case. Verified findings include {outcomes}. "
            if outcomes else
            f"Examine what the source evidence from {papers[0]} establishes about {subject}; "
            "conclusions remain provisional until the missing outcome evidence is retrieved. "
        ) + (
            "Compare reported experiments only where evidence permits, and limit conclusions to verified source contexts. "
            f"Evidence boundaries to check: {boundary}. "
            "A single primary study does not establish independent replication or field-wide consensus."
        )
    elif outcomes:
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


def resolve_section_depth_contract(section: Mapping[str, Any]) -> dict[str, Any]:
    """Repair stale automatic budgets after paper ownership changes.

    Explicit/custom contracts are preserved. A derived contract that asks for
    more than 350 words per planned paragraph is recalculated from current roles.
    """
    saved = dict(section.get("depth_contract") or {})
    paragraphs = int(saved.get("target_paragraph_count") or 0)
    if (saved.get("diagnostic_policy") == "derived_not_hard_word_quota" and paragraphs
            and int(saved.get("target_word_min") or 0) > paragraphs * 350):
        current = dict(section)
        current.pop("target_words", None)
        return derive_section_depth_contract(current)
    return saved or derive_section_depth_contract(section)


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
    # Current argument plans require realization of their core claims. Paper counts
    # alone do not establish a scientific need for comparison paragraphs.
    if "argument_order" in section:
        minimum_comparisons = 0
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
    semantic_review = writing_section.get("section_review")
    if isinstance(semantic_review, dict):
        complete = semantic_review.get("status") == "coherent"
        issues = list(semantic_review.get("issues") or [])
        # Processing notes and partial audits do not establish a structural flaw.
        incomplete = semantic_review.get("status") == "needs_revision"
        return {"status": "complete" if complete else "shallow" if incomplete else "not_reviewed",
                "paragraph_count": len(writing_section.get("paragraphs") or []),
                "missing_requirements": ["section_thread_review"] if incomplete else [],
                "issues": issues,
                "review_status": semantic_review.get("status"),
                "diagnostic_policy": "semantic_thread_not_role_quota"}
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
