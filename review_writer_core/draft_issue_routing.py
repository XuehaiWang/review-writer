"""Deterministic repair routing for Draft quality findings.

The router deliberately does not execute repairs.  It identifies the earliest
workflow owner and whether a paragraph rewrite is scientifically safe.  API
services can then keep using their existing targeted endpoints instead of
growing a second, generic repair service.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .quality_rules import finding_category
from .review_fact_readiness import evidence_problem_type as classify_evidence_problem
from .review_fact_readiness import is_strong_negative_claim
from .evidence_queries import COMPARISON_FIELD_IDS, fact_request_identity, normalize_fact_request
from .writing_contracts import paragraph_finding_is_blocking
from .source_attribution import source_grounded_repair

REPAIR_ROUTING_VERSION = 10


def requires_user_decision(issue):
    """Only a genuine decision owner asks the user; failed automation does not."""
    return bool(issue.get("repair_class") in {"human_confirmation", "planning_adjustment"}
                or issue.get("repair_route") in {"manual_online_retrieval_decision", "planning_revision"})


def planning_adjustments(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grouped argument requests for joint revision in the current Draft."""
    groups: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if issue.get("repair_class") != "planning_adjustment":
            continue
        section = str(issue.get("section_id") or "")
        row = groups.setdefault(section, {"section_id": section, "paragraph_ids": [],
            "claim_ids": [], "paper_ids": [], "issues": [], "claims": [],
            "proposed_action": "Reassess the affected argument against registered evidence. Preserve the research question and scientific categories; narrow the proposed conclusion or retain an open question where support is insufficient."})
        for key, values in (("paragraph_ids", [issue.get("paragraph_id")]),
                            ("claim_ids", issue.get("claim_ids") or issue.get("missing_core_claim_ids") or []),
                            ("paper_ids", issue.get("paper_ids") or [])):
            row[key] = sorted(set(row[key]) | {str(value) for value in values if value})
        row["issues"].append({key: issue.get(key) for key in
            ("issue_fingerprint", "diagnosis", "message", "source_check_status", "source_evidence_refs", "evidence_rescue_status")})
        known = {claim["claim_id"] for claim in row["claims"]}
        row["claims"].extend(claim for claim in issue.get("planning_claims") or [] if claim["claim_id"] not in known)
    return list(groups.values())


def repair_capability(issue: dict[str, Any], repair: dict[str, Any], *, source_status: str,
                      source_ready: bool) -> dict[str, Any]:
    """Project executable capabilities; model-authored action flags are ignored.

    Existing non-prose owners retain their specific action names. They are
    never described as executable by the paragraph optimizer merely because
    another stage happens to implement a repair.
    """
    target = repair.get("repair_target") or {}
    concrete = bool(issue.get("rule") or issue.get("rule_id") or issue.get("failed_dimensions")
                    or issue.get("unsupported_claims") or issue.get("missing_core_claim_ids")
                    or issue.get("observed_problem"))
    located = bool(target.get("paragraph_id") or target.get("section_id") or target.get("paper_ids")
                   or issue.get("rule") or issue.get("rule_id"))
    state, required, criteria = "open", ["current_paragraph"], ["target_issue_resolved", "protected_facts_preserved"]
    kind = "draft_rewrite"
    auto, eligible = bool(repair.get("auto_repairable")), bool(repair.get("rewrite_eligible"))
    rescue = bool(repair.get("evidence_rescue_eligible"))
    status = issue.get("evidence_rescue_status") or source_status
    length_only = (set(issue.get("failed_dimensions") or []) == {"P01"}
                   and not issue.get("unsupported_claims") and not issue.get("missing_core_claim_ids")
                   and source_status in {"verified", "not_applicable"})
    if length_only:
        kind, state, auto, eligible, rescue = "advisory", "advisory", False, False, False
        required, criteria = [], []
    elif source_status == "contradicted" and issue.get('source_corrections'):
        kind, state, auto, eligible, rescue = 'draft_rewrite', 'candidate_confirmation_required', True, True, False
        required = ['current_paragraph', 'local_source']
    elif repair.get("repair_action") == "correct_source_grounded_prose":
        kind, required = "draft_rewrite", ["current_paragraph", "local_source"]
        auto = eligible = bool(source_ready and concrete and located)
        rescue = False
    elif source_status == "contradicted" or repair.get("evidence_problem_type") == "conflict":
        kind, state, auto, eligible, rescue = "human_confirmation", "awaiting_human_confirmation", False, False, False
    elif repair.get("repair_stage") == "evidence_package":
        kind, required = "evidence_rescue_then_rewrite", ["current_paragraph", "local_source", "claim_trace"]
        if not concrete and source_status == "partially_supported" and source_ready:
            kind, state, auto, eligible, rescue = "advisory", "advisory", False, False, False
            required, criteria = [], []
        elif (repair.get("evidence_problem_type") == "unqualified_negative_claim"
                and not issue.get("core_claim_ids") and not issue.get("missing_core_claim_ids")):
            kind, auto, eligible, rescue = "claim_narrowing", source_ready, source_ready, False
        elif status == "provider_deferred":
            state, auto, eligible, rescue = "provider_deferred", False, False, True
        elif issue.get("evidence_rescue_status") == "not_found_in_checked_scope":
            rescue = False
            if issue.get("missing_core_claim_ids") or issue.get("core_claim_ids"):
                kind, state, auto, eligible = "planning_adjustment", "awaiting_plan_confirmation", False, False
            elif issue.get("unsupported_claims"):
                kind, auto, eligible = "claim_narrowing", True, True
            else:
                kind, state, auto, eligible = "advisory", "advisory", False, False
                required, criteria = [], []
        else:
            # Recovery is eligible even if retrieval has not established a
            # usable source yet. Prose generation requires supplied passages.
            rescue = True
            auto = eligible = source_ready and source_status not in {"needs_human_review", "not_found_in_checked_scope"}
            if issue.get("core_claim_ids") and issue.get("unsupported_claims"):
                # Source availability does not authorize removing a core
                # argument whose support has not been established.
                auto = eligible = False
    elif repair.get("repair_stage") not in {"draft", "evidence_package"}:
        kind, state, auto, eligible = "stage_repair", "awaiting_stage_repair", False, False
        required, criteria = ["current_stage_artifact"], ["stage_validation_passed"]
        if repair.get("repair_stage") in {"planning", "writing_plan"}:
            kind, state = "planning_adjustment", "awaiting_plan_confirmation"
    elif not concrete or not located:
        kind, state, auto, eligible = "advisory", "advisory", False, False
        required, criteria = [], []
    blocking = False if length_only else paragraph_finding_is_blocking({**issue, "source_check_status": source_status})
    if not located and not blocking:
        kind, state, auto, eligible, rescue = "advisory", "advisory", False, False, False
        required, criteria = [], []
    return {"repair_class": kind, "resolution_state": state,
            "auto_repairable": auto, "rewrite_eligible": eligible,
            "interactive_rewrite_eligible": bool(located and kind in {"draft_rewrite", "claim_narrowing", "advisory"}
                                                  and repair.get("repair_stage") == "draft") or eligible,
            "evidence_rescue_eligible": rescue, "required_inputs": required,
            "required_inputs_ready": bool(located and (source_ready if "local_source" in required else True)),
            "success_criteria": criteria, "blocking": blocking}


def paragraph_repair_contract(finding: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Use the same router inside the CLI and API, with registered Claim roles."""
    claims = [row for row in evidence.get("argument_plan") or [] if isinstance(row, dict)]
    passages = [p for paper in evidence.get("evidence") or [] if isinstance(paper, dict)
                for p in paper.get("original_passages") or [] if isinstance(p, dict)]
    issue = {**finding, "paper_ids": evidence.get("paper_ids") or finding.get("paper_ids") or [],
             "claim_ids": [row.get("claim_id") for row in claims if row.get("claim_id")],
             "core_claim_ids": [row.get("claim_id") for row in claims if row.get("required_for_section")]}
    return route_draft_issue(issue, source_status=str(finding.get("source_check_status") or "not_assessed"),
        evaluator_route=str(finding.get("route") or ""), has_original_passages=bool(passages),
        source_ready=bool(passages) or any(paper.get("original_text_available") for paper in evidence.get("evidence") or []),
        source_evidence_refs=finding.get("source_evidence_refs") or [],
        evidence_texts=[p.get("text") or "" for p in passages])


def select_rewrite_mode(finding, evidence, *, interactive=False):
    """One executable decision for batch and interactive entry points."""
    capability = paragraph_repair_contract(finding, evidence)
    eligible = capability["rewrite_eligible"] and capability["required_inputs_ready"]
    route = str(finding.get("route") or "")
    status = str(finding.get("source_check_status") or "")
    unsupported = finding.get("unsupported_claims") or []
    if eligible:
        if finding.get("source_corrections"):
            return "source_correction"
        if capability.get("repair_action") == "correct_source_grounded_prose":
            return "source_recheck_cleanup"
        if capability.get("evidence_problem_type") == "unqualified_negative_claim" or (
            unsupported and all(is_strong_negative_claim(value) for value in unsupported)
            and all(policy == "scope_limited_rewrite" for policy in (finding.get("negative_claim_policies") or {}).values())
        ):
            return "negative_claim_scope_narrowing"
        if status == "not_applicable" and not evidence.get("paper_ids") and unsupported:
            return "review_synthesis_cleanup"
        source_available = any(p.get("original_passages") or p.get("original_text_available")
                               for p in evidence.get("evidence") or [] if isinstance(p, dict))
        if unsupported and status in {"partially_supported", "unsupported", "not_found_in_checked_scope"} and source_available:
            return "source_recheck_cleanup"
        if status != "needs_human_review" and route in {"section_rewrite", "final_polish"}:
            return route
    # An explicitly requested stylistic candidate may preserve an unresolved
    # source issue; it must never become an unrestricted scientific rewrite.
    if interactive and route in {"human_confirmation", "local_source_recheck"}:
        return "human_review_style_only"
    if (interactive and capability["interactive_rewrite_eligible"]
            and capability["required_inputs_ready"]
            and capability["finding_category"] == "presentation"
            and route in {"section_rewrite", "final_polish"}):
        return route
    return ""


def quality_issue_paper_ids(
    issue: dict[str, Any],
    *,
    source_entry: dict[str, Any] | None = None,
    claims_by_id: dict[str, dict[str, Any]] | None = None,
    paragraph_claim_ids: list[str] | None = None,
) -> list[str]:
    """Return every traceable paper identity for one quality issue.

    Source checks store papers as nested rows, while current Writing Plans
    store them on Claims.  Keeping this traversal here prevents the quality
    router and fact-repair worker from drifting onto different source sets.
    """

    source = source_entry if isinstance(source_entry, dict) else {}
    claim_index = claims_by_id or {}
    claim_ids = [
        *(
            issue.get("claim_ids")
            if isinstance(issue.get("claim_ids"), list)
            else [issue.get("claim_id")]
            if issue.get("claim_id")
            else []
        ),
        *(paragraph_claim_ids or []),
    ]
    values: list[Any] = [
        *(issue.get("paper_ids") or []),
        *(source.get("paper_ids") or []),
    ]
    for claim_id in claim_ids:
        claim = claim_index.get(str(claim_id), {})
        for key in (
            "primary_papers",
            "comparison_papers",
            "citation_group",
            "paper_ids",
        ):
            values.extend(claim.get(key) or [])
    values.extend(
        paper.get("paper_id")
        for paper in source.get("papers") or []
        if isinstance(paper, dict)
    )
    return list(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def quality_issue_source_evidence_refs(
    issue: dict[str, Any],
    *,
    score: dict[str, Any] | None = None,
    source_entry: dict[str, Any] | None = None,
) -> list[str]:
    """Collect canonical source references from flat and nested check output."""

    score_row = score if isinstance(score, dict) else {}
    source = source_entry if isinstance(source_entry, dict) else {}
    values: list[Any] = [
        *(score_row.get("source_evidence_refs") or []),
        *(issue.get("source_evidence_refs") or []),
        *(source.get("source_evidence_refs") or []),
    ]
    for paper in source.get("papers") or []:
        if not isinstance(paper, dict):
            continue
        values.extend(paper.get("source_evidence_refs") or [])
        for passage in paper.get("passages") or []:
            if not isinstance(passage, dict):
                continue
            values.append(
                passage.get("evidence_key")
                or passage.get("evidence_id")
                or passage.get("chunk_id")
            )
    return list(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def draft_fact_repair_checkpoint(payload):
    """Recover owned per-paper checkpoints, not old paragraph scores.

    The Worker still checks source/question fingerprints before reuse. An
    unrelated prose edit must not discard a completed source investigation.
    """
    quality = payload.get("quality") or {}
    records = [(str(quality.get("evaluated_at") or ""),
                (quality.get("feedback_status") or {}).get("fact_repair_checkpoint") or {})]
    for proposal in payload.get("optimization_proposals") or []:
        records.append((str(proposal.get("created_at") or ""),
                        (proposal.get("feedback_status") or {}).get("fact_repair_checkpoint") or {}))
    for candidate in payload.get("rewrite_candidates") or []:
        repair = (candidate.get("candidate_evaluation") or {}).get("fact_agent_repair") or {}
        records.append((str(candidate.get("created_at") or ""), repair.get("matrix_enrichment_checkpoint") or {}))
    entries = {}
    for _created, checkpoint in sorted(records, key=lambda item: item[0]):
        entries.update(checkpoint.get("entries") or {})
    return {"schema_version": 1, "entries": entries} if entries else {}


def draft_fact_repair_requests(payload):
    """Group only source-gap repairs by paper and scientific question.

    Paragraph IDs remain consumers, not cache identities. Language problems,
    manual source conflicts and structure repairs do not launch extraction.
    """
    paragraphs, claims = {}, {}
    for section in (payload.get("writing_plan") or {}).get("sections") or []:
        for claim in section.get("claims") or []:
            claims[str(claim.get("claim_id"))] = claim
        for paragraph in section.get("paragraphs") or []:
            paragraphs[str(paragraph.get("paragraph_id"))] = {**paragraph, "section_id": section.get("section_id")}
    allowed = {str(row.get("paper_id")) for row in (payload.get("matrix") or {}).get("rows") or []}
    grouped = {}

    def register(
        *,
        issue: dict[str, Any],
        paragraph_id: str,
        paragraph: dict[str, Any],
        claim_id: str,
        query: str,
        field_id: Any,
        experiment_id: Any,
        evidence_keys: list[Any],
        paper_ids: set[str],
    ) -> bool:
        request = normalize_fact_request(
            {
                "field_id": (
                    field_id if field_id in COMPARISON_FIELD_IDS else "validation_evidence"
                ),
                "query": str(query or "").strip(),
                "experiment_id": experiment_id,
                "evidence_keys": [key for key in evidence_keys if key],
            }
        )
        if not request:
            return False
        selected_papers = paper_ids & allowed
        for paper_id in selected_papers:
            key = (paper_id, fact_request_identity(request))
            root = grouped.setdefault(
                key,
                {
                    "paper_id": paper_id,
                    "request": request,
                    "paragraph_ids": [],
                    "paragraph_sections": {},
                    "claim_ids": [],
                    "evidence_rescue": not bool(issue.get("auto_repairable", True)),
                },
            )
            root["paragraph_sections"][paragraph_id] = (
                paragraph.get("section_id") or issue.get("section_id") or ""
            )
            if paragraph_id not in root["paragraph_ids"]:
                root["paragraph_ids"].append(paragraph_id)
            if claim_id and claim_id not in root["claim_ids"]:
                root["claim_ids"].append(claim_id)
        return bool(selected_papers)

    issues = payload.get("issues") or (payload.get("quality") or {}).get("issues") or []
    for issue in issues:
        if not isinstance(issue, dict) or not (
            issue.get("auto_repairable", True)
            or issue.get("evidence_rescue_eligible")
        ):
            continue
        if issue.get("repair_stage") != "evidence_package":
            continue
        pid = str(issue.get("paragraph_id") or "")
        if payload.get("paragraph_id") and pid != payload["paragraph_id"]:
            continue
        paragraph = paragraphs.get(pid, {})
        ids = issue.get("claim_ids") or paragraph.get("claim_ids") or []
        registered = False
        for cid in ids:
            claim = claims.get(str(cid), {})
            query = str(claim.get("claim") or "").strip()
            registered = register(
                issue=issue,
                paragraph_id=pid,
                paragraph=paragraph,
                claim_id=str(cid),
                query=query,
                field_id=claim.get("field_id") or issue.get("field_id"),
                experiment_id=claim.get("experiment_id"),
                evidence_keys=[
                    ref.get("evidence_key")
                    for ref in claim.get("evidence_refs") or []
                    if isinstance(ref, dict)
                ],
                paper_ids=set(map(str, claim.get("citation_group") or [])),
            ) or registered
        if registered:
            continue
        fallback_papers = {
            str(value)
            for value in [
                *(issue.get("paper_ids") or []),
                *((issue.get("repair_target") or {}).get("paper_ids") or []),
                *(paragraph.get("cited_paper_ids") or []),
                *(paragraph.get("paper_ids") or []),
            ]
            if str(value).strip()
        }
        fallback_queries = [
            str(value).strip()
            for value in issue.get("unsupported_claims") or []
            if str(value).strip()
        ]
        if not fallback_queries:
            diagnosis = str(issue.get("diagnosis") or issue.get("message") or "").strip()
            if diagnosis:
                fallback_queries = [diagnosis]
        for query in fallback_queries[:4]:
            register(
                issue=issue,
                paragraph_id=pid,
                paragraph=paragraph,
                claim_id="",
                query=query,
                field_id=issue.get("field_id"),
                experiment_id=issue.get("experiment_id"),
                evidence_keys=[],
                paper_ids=fallback_papers,
            )
    return list(grouped.values())


def _contains(text: str, terms: set[str]) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
        for term in terms
    )


DISCOVERY_TERMS = {
    "search",
    "retrieval",
    "coverage",
    "corpus",
    "missing_primary",
    "recall",
    "sampling",
    "publication_bias",
}
METADATA_TERMS = {
    "metadata conflict",
    "bibliographic identity",
    "paper identity",
    "doi conflict",
    "year conflict",
    "author conflict",
    "journal conflict",
    "fact conflict",
}
PLANNING_TERMS = {
    "taxonomy",
    "classification",
    "matrix",
    "outline",
    "organization",
    "section_structure",
    "category",
    "single-paper",
    "catch-all",
}
SYNTHESIS_TERMS = {
    "comparison coverage",
    "comparison insufficient",
    "insufficient comparison",
    "comparison_axis",
    "required_cross_study_comparison_missing",
    "section exit",
    "synthesis exit",
    "section_synthesis_exit_position",
    "cross_study_comparison_quota",
}
EVIDENCE_TERMS = {
    "required_claim",
    "no supporting evidence",
    "missing supporting evidence",
    "unsupported claim",
    "source unavailable",
    "source passage missing",
    "local source unavailable",
    "claim evidence missing",
    "evidence gap",
    "c01",
}
FIGURE_TERMS = {
    "figure callout",
    "scheme callout",
    "visible_callout",
    "figure insertion",
    "figure placement",
    "image placement",
    "figure argument",
    "caption mismatch",
}
BIBLIOGRAPHY_TERMS = {
    "bibliography",
    "reference field",
    "reference metadata",
    "missing journal",
    "missing pages",
    "article number",
    "doi_or_locator",
}
FINAL_TERMS = {
    "export",
    "docx",
    "pdf",
    "xml incompatible",
    "unresolved placeholder",
    "unsupported markup",
}


def _matched_terms(text: str, terms: set[str]) -> list[str]:
    return sorted(term for term in terms if _contains(text, {term}))


def issue_fingerprint(issue: dict[str, Any], repair: dict[str, Any]) -> str:
    """Return a wording-insensitive identity for one repair target.

    Evaluator prose is free-form and can change between runs.  A fingerprint
    therefore uses the paragraph, executable route, rule/dimension identities,
    and only recognized deterministic signal families.  It intentionally does
    not hash the diagnosis sentence itself.
    """

    searchable = " ".join(
        [
            str(issue.get("issue_type") or ""),
            str(issue.get("rule_id") or issue.get("rule") or ""),
            str(issue.get("diagnosis") or issue.get("message") or ""),
            *[str(value) for value in issue.get("failed_dimensions") or []],
        ]
    ).casefold()
    signals = _matched_terms(
        searchable,
        DISCOVERY_TERMS
        | METADATA_TERMS
        | PLANNING_TERMS
        | SYNTHESIS_TERMS
        | EVIDENCE_TERMS
        | FIGURE_TERMS
        | BIBLIOGRAPHY_TERMS
        | FINAL_TERMS,
    )
    dimensions = sorted(
        {
            str(value).strip().casefold()
            for value in issue.get("failed_dimensions") or []
            if str(value).strip()
        }
    )
    if dimensions or finding_category(issue) != "unknown":
        # Rubric/rule identifiers are the stronger stable identity. Diagnosis
        # wording and matched phrases may vary between provider calls.
        signals = []
    key = "|".join(
        [
            str(issue.get("paragraph_id") or issue.get("section_id") or "global"),
            finding_category(issue),
            ",".join(dimensions),
            ",".join(signals),
            ",".join(sorted(set(str(value) for value in issue.get("claim_ids") or []))),
            ",".join(sorted(set(str(value) for value in issue.get("paper_ids") or []))),
        ]
    )
    return "ISSUE-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper()


def repair_input_fingerprint(paragraph, issue, evidence, *, constraints):
    """A bounded repair cache identity, independent of scores, time and task IDs.

    Actual passage bytes and their source identities matter. A changed source
    size also reopens previous empty searches; network errors are never cached
    as a scientific no-progress result by the caller.
    """
    sources = []
    for paper in evidence.get("evidence") or []:
        passages = [{key: passage.get(key) for key in
                     ("ref", "text", "page", "source_lineage_hash", "source_file_id")}
                    for passage in paper.get("original_passages") or []]
        sources.append({"paper_id": paper.get("paper_id"), "source_kind": paper.get("source_kind"),
                        "source_text_chars": paper.get("source_text_chars"),
                        "source_content_hash": paper.get("source_content_hash"),
                        "passages": sorted(passages, key=lambda row: json.dumps(row, sort_keys=True))})
    value = {"contract": "draft-repair/2", "paragraph_id": paragraph.get("paragraph_id"),
             "text": paragraph.get("text"), "heading": paragraph.get("heading"),
             "issue": issue_fingerprint(issue, {"repair_route": issue.get("automatic_rewrite_mode") or issue.get("repair_route")}),
             "sources": sorted(sources, key=lambda row: str(row["paper_id"])),
             "argument_plan": evidence.get("argument_plan") or [],
             "contract_version": REPAIR_ROUTING_VERSION, "constraints": constraints}
    if evidence.get("section_context"):
        value["section_context"] = evidence["section_context"]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def route_draft_issue(
    issue: dict[str, Any],
    *,
    source_status: str = "not_assessed",
    evaluator_route: str = "",
    has_original_passages: bool = False,
    reference_map_problem: bool = False,
    source_evidence_refs: list[Any] | None = None,
    source_ready: bool = False,
    evidence_texts: list[Any] | None = None,
) -> dict[str, Any]:
    """Return stable repair metadata for one quality issue.

    ``repair_route`` remains the executable action name used by the existing
    Draft optimizer.  ``repair_stage`` and ``repair_target`` expose the actual
    workflow owner so the UI can send users to a precise target when automatic
    repair is not appropriate.
    """

    source_status = str(source_status or "not_assessed").casefold()
    evaluator_route = str(evaluator_route or "").casefold()
    # Persisted quality rows can already contain the router's former derived
    # metadata. Never feed that output back as fresh evidence for the next
    # routing decision, or an old mistake becomes self-perpetuating.
    issue_type_signal = (
        ""
        if issue.get("repair_stage") or issue.get("repair_route")
        else str(issue.get("issue_type") or "")
    )
    category = finding_category({**issue, "source_check_status": source_status})
    if category == "unknown":
        # Compatibility for old artifacts only. New evaluations carry a category.
        # Require an actual defect in the same clause; positive mentions are not routes.
        diagnosis = str(issue.get("diagnosis") or issue.get("message") or "").casefold()
        defect_clauses = " ".join(clause for clause in re.split(r"[.;。；\n]", diagnosis)
            if re.search(r"missing|mismatch|incorrect|invalid|broken|misplaced|unresolved|unsupported|absent|insufficient|poorly|conflict|缺失|缺少|错误|不匹配|不当|不足|冲突", clause))
        signals = " ".join([evaluator_route, issue_type_signal, str(issue.get("rule_id") or ""), defect_clauses])
        for owner, terms in (
            ("figure", FIGURE_TERMS | {"图示引用", "图注", "图片位置"}),
            ("bibliography", BIBLIOGRAPHY_TERMS | {"参考文献"}),
            ("export", FINAL_TERMS), ("synthesis", SYNTHESIS_TERMS),
            ("discovery", DISCOVERY_TERMS), ("metadata", METADATA_TERMS),
            ("planning", PLANNING_TERMS), ("evidence", EVIDENCE_TERMS),
        ):
            if _contains(signals, terms):
                category = owner
                break

    result: dict[str, Any] = {
        "repair_routing_version": REPAIR_ROUTING_VERSION,
        "finding_category": category,
        "issue_type": "draft_wording",
        "repair_route": "paragraph_rewrite",
        "repair_stage": "draft",
        "repair_action": "rewrite_paragraph",
        "repair_target": {
            "stage": "draft",
            "paragraph_id": str(issue.get("paragraph_id") or ""),
        },
        "auto_repairable": True,
        "rewrite_eligible": True,
        "internal_repair_stage": "draft",
        "recommended_action": (
            "Revise this paragraph without changing supported scientific claims."
        ),
        "evidence_problem_type": str(issue.get("evidence_problem_type") or "")
        or classify_evidence_problem(
            unsupported_claims=issue.get("unsupported_claims") or [],
            source_check_status=source_status,
            source_evidence_refs=source_evidence_refs or [],
            source_ready=source_ready,
            evidence_texts=evidence_texts or [],
        ),
    }

    if category == "figure":
        result.update(
            issue_type="figure_argument_or_placement",
            repair_route="figure_insertion_repair",
            repair_stage="figures",
            repair_action="rebuild_figure_insertion_plan",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="figures",
            recommended_action=(
                "Rebuild the current figure insertion decision, callout, and caption."
            ),
        )
    elif category == "bibliography":
        result.update(
            issue_type="bibliography_metadata",
            repair_route="bibliography_repair",
            repair_stage="bibliography",
            repair_action="repair_canonical_bibliography",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="bibliography",
            recommended_action=(
                "Repair the canonical bibliography record; do not rewrite prose to hide it."
            ),
        )
    elif category == "export":
        result.update(
            issue_type="final_export_integrity",
            repair_route="final_export_repair",
            repair_stage="final",
            repair_action="rebuild_final_export",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="final",
            recommended_action="Rebuild the current Final artifact and export checks.",
        )
    elif category == "synthesis":
        result.update(
            issue_type="synthesis_plan_gap",
            repair_route="synthesis_plan_repair",
            repair_stage="writing_plan",
            repair_action="rebuild_section_synthesis_and_writing_plan",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="sections",
            recommended_action=(
                "Rebuild the affected section synthesis state and writing plan."
            ),
        )
    elif category == "discovery":
        result.update(
            issue_type="literature_coverage_gap",
            repair_route="manual_online_retrieval_decision",
            repair_stage="discovery",
            repair_action="broaden_or_correct_retrieval",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="discovery",
            recommended_action=(
                "Broaden or correct the retrieval scope, then refresh Matrix evidence."
            ),
        )
    elif category == "metadata":
        result.update(
            issue_type="metadata_or_fact_conflict",
            repair_route="metadata_matrix_repair",
            repair_stage="library_matrix",
            repair_action="recheck_local_metadata_and_matrix_fact",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="matrix",
            recommended_action=(
                "Recheck the local publication metadata and Matrix fact before rewriting."
            ),
        )
    elif category == "planning":
        result.update(
            issue_type="planning_structure",
            repair_route="planning_revision",
            repair_stage="planning",
            repair_action="revise_matrix_or_outline",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="planning",
            recommended_action=(
                "Correct the Matrix classification or section structure before rewriting."
            ),
        )
    elif reference_map_problem and source_status in {
        "verified",
        "not_applicable",
        "not_assessed",
    }:
        result.update(
            issue_type="citation_reference_mapping",
            repair_route="deterministic_reference_rebuild",
            repair_stage="bibliography",
            repair_action="rebuild_citation_reference_map",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="draft",
            recommended_action=(
                "Rebuild the citation and reference map deterministically."
            ),
        )
    elif (source_grounded_repair({**issue, "source_check_status": source_status})
          and has_original_passages):
        result.update(
            issue_type="source_grounded_wording",
            repair_route="paragraph_rewrite",
            repair_stage="draft",
            repair_action="correct_source_grounded_prose",
            evidence_rescue_eligible=False,
            recommended_action="Correct the diagnosed assertion using the checked local context; preserve unrelated sentences.",
        )
    elif (
        category == "evidence"
        or category == "unknown" and source_status
        in {
            "partially_supported",
            "unsupported",
            "needs_human_review",
            "contradicted",
            "not_found_in_checked_scope",
        }
        or evaluator_route == "local_source_recheck"
    ):
        repair_route = (
            "targeted_evidence_then_paragraph_rewrite"
            if has_original_passages
            else "claim_downgrade_then_paragraph_rewrite"
        )
        result.update(
            issue_type="claim_evidence_gap",
            repair_route=repair_route,
            repair_stage="evidence_package",
            repair_action=(
                "attach_local_evidence_then_rewrite"
                if has_original_passages
                else "downgrade_unsupported_claim_then_rewrite"
            ),
            auto_repairable=source_status
            not in {"needs_human_review", "contradicted"},
            rewrite_eligible=source_status
            not in {"needs_human_review", "contradicted"},
            evidence_rescue_eligible=source_status == "needs_human_review",
            internal_repair_stage="sections",
            recommended_action=(
                "Attach matching local-source passages and rewrite only this paragraph."
                if has_original_passages
                else "Keep the Claim trace, lower unsupported detail, and rewrite only this paragraph."
            ),
        )
        if source_status == "contradicted":
            result["recommended_action"] = (
                "Resolve the explicit conflict between the claim and its registered source."
            )

    result["repair_target"] = {
        "stage": result["repair_stage"],
        "paragraph_id": str(issue.get("paragraph_id") or ""),
        "section_id": str(issue.get("section_id") or ""),
        "paper_ids": [
            str(value)
            for value in issue.get("paper_ids") or []
            if str(value).strip()
        ],
    }
    result.update(repair_capability(issue, result, source_status=source_status,
                                    source_ready=source_ready or has_original_passages))
    if result["repair_class"] == "planning_adjustment":
        result.update(execution_stage="draft", resolution_state="awaiting_draft_revision",
                      repair_action="review_draft_joint_revision",
                      recommended_return_stage="draft",
                      recommended_action="Generate a joint argument/body candidate in the current Draft; keep the upstream plan unchanged.")
    result["issue_fingerprint"] = issue_fingerprint(issue, result)
    return result
