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


def resolve_section_depth_contract(section: Mapping[str, Any]) -> dict[str, Any]:
    """Project legacy contracts without resurrecting automatic writing quotas."""
    return derive_section_depth_contract(section)


def derive_section_depth_contract(section: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility-shaped diagnostics; scientific depth is not word count."""
    return {
        "target_paragraph_count": 0, "target_word_min": 0, "target_word_max": 0,
        "minimum_comparison_paragraphs": 0, "requires_section_synthesis_exit": False,
        "required_paragraph_roles": [], "paper_count": len(_paper_ids(section)),
        "diagnostic_policy": "evidence_adaptive_no_quota",
    }


def build_section_writing_task(section: Mapping[str, Any], *, current_evidence=None) -> dict[str, Any]:
    """Project scientific instructions, not retrieval-stage work orders.

    Never rewrite free-text instructions by matching domain words. Remove only
    exact repetitions, preserving explicit scope, exceptions and user wording.
    """
    result = {key: deepcopy(section.get(key)) for key in (
        "heading", "section_role", "writing_objective", "questions_to_answer",
        "core_argument", "avoid_points", "primary_papers", "supporting_papers")}
    normalized = lambda text: " ".join(str(text or "").split())
    if normalized(result["core_argument"]) == normalized(result["writing_objective"]):
        result.pop("core_argument")
    for key in ("questions_to_answer", "avoid_points"):
        if isinstance(result[key], list):
            unique = []
            for item in result[key]:
                if item not in unique:
                    unique.append(item)
            result[key] = unique
    # Retrieval still consumes the original task. The writer receives checks
    # as coverage context, not as a second mandatory paragraph outline.
    result["argument_question"] = deepcopy(section.get("review_problem") or section.get("writing_objective"))
    result["evidence_checks"] = {
        "questions": result.pop("questions_to_answer"),
        "usage": "coverage_context_not_a_prose_checklist; preserve explicit user requests; "
                 "use relevant answers to develop the argument, not one answer per paragraph",
    }
    if current_evidence is not None:
        # Keep scientific scope verbatim, but stop presenting an old planning
        # work order as the current execution objective. No keyword deletion.
        result["planning_snapshot"] = {
            "original_objective": result.pop("writing_objective"),
            "usage": "Preserve scientific scope, exclusions and explicit user requirements. "
                     "Availability/retrieval statements describe planning time, not today's sources. "
                     "Do not write a retrieval plan or repeat availability disclaimers in the manuscript.",
        }
        result["argument_question"] = deepcopy(section.get("review_problem") or section.get("heading"))
        observed = {}
        for row in current_evidence:
            if not isinstance(row, Mapping):
                continue
            pid = str(row.get("paper_id") or "")
            state = observed.setdefault(pid, {"paper_id": pid, "source_channels": [], "assertion_ceilings": []})
            for field, target in (("source_channel", "source_channels"), ("assertion_ceiling", "assertion_ceilings")):
                if row.get(field) and row[field] not in state[target]:
                    state[target].append(row[field])
        result["current_evidence_scope"] = {
            "papers": list(observed.values()), "coverage": "supplied_excerpts_only_not_complete_full_text",
            "usage": "Use each supplied passage's channel and assertion ceiling; unknown metadata is not proof of full-text coverage. "
                     "Do not globally promote abstract-only papers because another paper has body text.",
        }
    # Body method sections carry a "shopping list": which facts to keep and which
    # per-compound detail to express as a category or range instead of writing it
    # out. Introduction/Conclusion keep their own narrative guidance untouched.
    if str(section.get("section_role") or "").strip().casefold() == "body":
        required = [
            "transformation", "key_catalyst", "outcome", "scope_class", "decisive_boundary",
        ]
        # Only ask for the synthetic-application item when this chapter actually
        # describes one; never force a chapter to invent an application.
        if _declares_application(section):
            required.append("synthetic_application")
        result["required_evidence"] = required
        result["excluded_by_default"] = [
            "per_compound_id", "individual_yield", "screening_steps",
            "routine_ops", "unrelated_mechanism", "closing_summary",
        ]
        # The reader table is projected from result_context alone, so prose
        # compression must not also shrink the records: a method summarized as a
        # category in the prose still needs its own row, because a body section
        # with fewer than two records from two papers loses its table entirely.
        result["table_record_policy"] = (
            "prose_may_compress_to_category_or_range_but_result_context_records_stay_complete; "
            "one_record_per_reported_method; fewer_than_two_records_from_two_papers_drops_the_table"
        )
    return result


# An application is only requested when the chapter itself declares one; the
# terms are intentionally narrow so an ordinary synthetic method does not turn
# into a mandatory application sentence.
_APPLICATION_TERMS = re.compile(
    r"\b(?:synthetic\s+applications?|applications?\s+in|total\s+synthesis|multi[- ]gram|gram[- ]scale|"
    r"scale[- ]up|derivatization|derivatisation|practical\s+(?:use|synthesis)|late[- ]stage)"
    r"|合成应用|全合成|多克|克级|放大|衍生化",
    re.I,
)


def _declares_application(section: Mapping[str, Any]) -> bool:
    """True when the chapter's own text/reasons describe a synthetic application."""
    parts = [str(section.get(key) or "") for key in
             ("heading", "title", "writing_objective", "review_problem", "core_argument")]
    for role in section.get("paper_roles") or []:
        if isinstance(role, Mapping):
            parts.append(str(role.get("reason") or ""))
    return bool(_APPLICATION_TERMS.search(" ".join(parts)))


def build_question_evidence_navigation(section, sources):
    """Request-local question → source pointers, never new evidence or a filter.

    Use retrieval memberships first. Lexical overlap is only a navigation hint
    for legacy/direct-key facts; retain every unmatched source separately.
    Source IDs here must be the aliases actually displayed to this request.
    """
    from .evidence_queries import query_terms

    questions = [("section_focus", str(section.get("review_problem") or section.get("heading") or "").strip())]
    legacy = list(dict.fromkeys([*(section.get("questions_to_answer") or []),
                                *(section.get("retrieval_directions") or [])]))
    questions.extend((f"writing_question_{index}", str(question).strip())
                     for index, question in enumerate(legacy, 1) if str(question).strip())
    groups, assigned = [], set()
    source_terms = [set(query_terms(source.get("content") or "", limit=1000)) for source in sources]
    for qid, question in questions:
        if not question:
            continue
        terms = set(query_terms(question, limit=12))
        ranked = []
        for index, source in enumerate(sources):
            membership = qid in (source.get("question_ids") or [])
            overlap = len(terms & source_terms[index])
            if membership or overlap:
                ranked.append((not membership, -overlap, index, source["evidence_key"]))
        refs = [item[-1] for item in sorted(ranked)]
        assigned.update(refs)
        groups.append({"question_id": qid, "question": question, "source_refs": refs})
    return {"questions": groups,
            "other_source_refs": [row["evidence_key"] for row in sources if row["evidence_key"] not in assigned],
            "source_ownership": {row["evidence_key"]: row["study_ownership"]
                                 for row in sources if row.get("study_ownership")},
            "usage": "navigation_not_entailment_or_paragraph_checklist; inspect contrary evidence and conditions; "
                     "empty matches mean unlocated_support_not_absence; all supplied sources remain available; "
                     "adapt the argument, not the user's scope; a key study may deserve a whole paragraph"}


def build_chapter_navigation(responsibilities, *, section_id):
    """Other chapters define boundaries, not another set of paragraph instructions."""
    result = []
    for row in responsibilities or []:
        if not isinstance(row, Mapping) or str(row.get("section_id")) == str(section_id):
            continue
        item = {key: deepcopy(row.get(key)) for key in (
            "section_id", "heading", "review_problem", "avoid_points", "primary_papers", "supporting_papers")}
        item["planning_snapshot"] = {"original_objective": deepcopy(row.get("writing_objective")),
                                     "usage": "scientific_boundary_not_current_retrieval_status"}
        result.append(item)
    return result


def build_section_writing_guidance(section: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only navigation; no generated budgets or presumed scientific conclusions."""
    result = {key: deepcopy(section[key]) for key in (
        "review_problem", "organizing_thread", "paper_roles", "notes"
    ) if section.get(key)}
    result["mergeable_points"] = list(dict.fromkeys(
        str(value).strip() for value in section.get("paragraph_tasks") or [] if str(value).strip()))
    if section.get("requested_length"):
        result["requested_length"] = deepcopy(section["requested_length"])
    elif section.get("target_words_source") in {"user", "explicit_user"}:
        result["requested_length"] = section.get("target_words")
    elif (section.get("depth_contract") or {}).get("diagnostic_policy") == "user_defined":
        result["requested_length"] = {key: section["depth_contract"][key] for key in
                                      ("target_word_min", "target_word_max")
                                      if section["depth_contract"].get(key)}
    result["usage"] = (
        "planning_snapshot_not_current_retrieval_status; navigation_not_evidence; "
        "no mandatory paragraph count or closing summary"
    )
    return result


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


_BLUEPRINT_CLAIM_ROLES = {
    "foundation": "section_frame",
    "extension": "method_extension",
    "contrast": "cross_study_comparison",
    "comparison": "cross_study_comparison",
    "mechanism": "mechanism_boundary",
    "limitation": "scope_limitation",
    "synthesis": "section_synthesis_exit",
}


def build_paragraph_plan(section: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Turn Blueprint review_claims into a hard per-paragraph skeleton.

    Returns None when the section has no review_claims so the caller keeps the
    existing free-form path. Each entry carries a canonical argument role, the
    papers allowed in that paragraph, the claim's purpose (planning text, never
    prose to copy), and its comparison axes.
    """
    claims = section.get("review_claims")
    if not isinstance(claims, list) or not claims:
        return None
    plans: list[dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        papers = _unique(
            (
                str(item.get("paper_id") or "")
                for item in claim.get("supporting_papers") or []
                if isinstance(item, dict) and str(item.get("paper_id") or "").strip()
            ),
            limit=10000,
        )
        if not papers:
            continue
        claim_type = _compact(claim.get("claim_type"), limit=40).casefold().replace("-", "_")
        role = _BLUEPRINT_CLAIM_ROLES.get(
            claim_type,
            canonical_argument_role(claim_type, paper_count=len(papers)),
        )
        plans.append(
            {
                "role": role,
                "papers": papers,
                "purpose": _compact(claim.get("claim")),
                "comparison_axes": _unique(
                    str(value) for value in (claim.get("comparison_axes") or [])
                ),
                "wording_constraints": _unique(
                    (
                        str(value)
                        for value in (claim.get("wording_constraints") or [])
                        if str(value).strip()
                    ),
                    limit=20,
                ),
            }
        )
    return plans or None


def derive_narrative_diagnostics(
    writing_section: Mapping[str, Any],
    depth_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure paragraph-role and comparison coverage from a Writing Plan."""

    semantic_review = writing_section.get("section_review")
    if isinstance(semantic_review, dict):
        complete = semantic_review.get("status") == "coherent"
        issues = list(semantic_review.get("issues") or [])
        # Processing notes and partial audits do not establish a structural flaw.
        incomplete = semantic_review.get("status") == "needs_revision"
        return {"status": "complete" if complete else "advisory" if incomplete else "not_reviewed",
                "paragraph_count": len(writing_section.get("paragraphs") or []),
                "missing_requirements": [],
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
    comparison_count = roles.count("cross_study_comparison")
    return {
        "status": "not_reviewed",
        "paragraph_count": len(paragraphs),
        "target_paragraph_count": 0,
        "paragraph_roles": roles,
        "comparison_paragraph_count": comparison_count,
        "minimum_comparison_paragraphs": 0,
        "missing_requirements": [],
    }
