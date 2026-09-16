"""Shared semantic attribution guidance, independent of discipline or compounds."""

from .quality_rules import finding_category

CONTRIBUTION_WRITING_POLICY = (
    "Understand each paper's research problem and actual contribution before choosing examples. "
    "A passage located in a paper does not prove that paper owns the finding. Distinguish own results, "
    "prior work and unknown ownership using context and citations, never section names alone. "
    "Contribution summaries are navigation, not independently verified evidence. Never infer priority "
    "or historical influence from publication order. Preserve counterexamples. "
    "Write new scientific prose organized around the question, not copied source sentences, translations "
    "or synonym substitution. Preserve technical names, formulas, quantities and experimental scope. "
    "Exact quotations belong only in evidence fields. Include detailed conditions only when they explain "
    "the argument; other supported records may go into comparison tables."
)

SECTION_THREAD_POLICY = (
    "Use one clear section thread: explain the question and organizing rationale, then order paragraph "
    "tasks so each example advances it, and end with a supported answer or bounded uncertainty. "
    "Chronology, structural differences, problem progression and strategy comparison are examples, "
    "not a mandatory taxonomy or paragraph quota. Explain why adjacent examples belong together; "
    "adding 'furthermore' is not a scientific transition. Do not invent mechanistic explanations, "
    "comparability or historical dependence to connect paragraphs. Single-study cases and parallel "
    "organization are legitimate. Do not repeat a limitations disclaimer at every paragraph ending."
)


def contribution_context(analysis, *, limit=900):
    """One compact navigation projection shared by planning and writing."""
    if not isinstance(analysis, dict):
        return {}
    result = {key: " ".join(str(analysis.get(key) or "").split())[:limit]
              for key in ("research_question", "contribution", "topic_relation")}
    if not any(result.values()):
        return {}
    return {**result, "usage": "navigation_only", "fact_ids": list(analysis.get("fact_ids") or [])}


def section_navigation_context(plan, paragraphs):
    """Transient writing context; current prose is navigation, never source evidence."""
    sections = {str(p.get("paragraph_id")): section
                for section in plan.get("sections") or []
                for p in section.get("paragraphs") or [] if p.get("paragraph_id")}
    result = {}
    for index, paragraph in enumerate(paragraphs):
        pid = str(paragraph.get("paragraph_id") or "")
        section = sections.get(pid)
        if not section:
            continue
        context = {key: section.get(key) for key in
                   ("section_id", "organizing_thread", "paragraph_tasks", "questions_to_answer", "paper_roles")}
        context["usage"] = "navigation_only_not_source_evidence"
        for label, position in (("previous", index - 1), ("next", index + 1)):
            if 0 <= position < len(paragraphs):
                neighbor = paragraphs[position]
                if sections.get(str(neighbor.get("paragraph_id"))) is section:
                    context[label] = {"paragraph_id": neighbor["paragraph_id"],
                                      "text": str(neighbor.get("text") or "")[:900]}
        result[pid] = context
    return result


def validated_attribution_repair(text, proposal, evidence):
    """Source-addressable rewrite guidance, not permission to bypass candidate QA."""
    if not isinstance(proposal, dict) or proposal.get("kind") != "wrong_study_attribution":
        return {}
    span = proposal.get("claim_span")
    direction = proposal.get("correction")
    if (not isinstance(span, str) or not span or text.count(span) != 1
            or not isinstance(direction, str) or not direction.strip() or len(direction) > 1200):
        return {}
    passages = {p.get("ref"): p.get("text", "")
                for paper in evidence.get("evidence") or []
                for p in paper.get("original_passages") or []}
    quotes = proposal.get("context")
    if not isinstance(quotes, list) or not 2 <= len(quotes) <= 4:
        return {}
    checked = []
    for row in quotes:
        if not isinstance(row, dict):
            return {}
        ref, quote = row.get("source_ref"), row.get("quote")
        if (not isinstance(ref, str) or not isinstance(quote, str) or len(quote.strip()) < 20
                or quote not in passages.get(ref, "")):
            return {}
        checked.append({"source_ref": ref, "quote": quote})
    if len({r["quote"] for r in checked}) < 2:
        return {}
    return {"kind": proposal["kind"], "claim_span": span,
            "correction": direction, "context": checked}


def source_grounded_repair(finding):
    """A concrete evidence defect with sufficient checked context, not missing evidence."""
    return bool(
        finding_category(finding) == "evidence"
        and (finding.get("source_check_status") == "verified"
             or bool(finding.get("source_attribution_repair")))
        and (finding.get("failed_dimensions") or finding.get("unsupported_claims"))
        and not finding.get("missing_core_claim_ids")
        and (finding.get("evidence_problem_type") in {None, "", "none", "binding_mismatch"}
             or bool(finding.get("source_attribution_repair")))
    )

SOURCE_ATTRIBUTION_POLICY = (
    "In manuscript prose, refer to the cited studies or sources, never to supplied/provided/retrieved "
    "passages, prompts, evidence packages or internal checks. Preserve scientific uncertainty and "
    "limits of comparison; do not turn a retrieval limitation into a claim that research does not exist. "
    "Track which study, method and experimental system owns each finding or limitation. "
    "Distinguish prior work discussed in a source from that source's own results; read surrounding "
    "transitions before assigning a success, failure or scope limitation. A correct citation alone "
    "does not establish correct attribution. In a multi-study summary, use a shared label only when "
    "it applies to every study being grouped; otherwise name the relevant subset or separate the "
    "systems. Do not transfer catalyst roles, conditions, outcomes or limitations between studies."
)


def attribution_repair_instruction(score, evidence, rewrite_mode):
    """Permit diagnosed relationship repairs, never an unrestricted fact rewrite."""
    if (rewrite_mode not in {"source_recheck_cleanup", "section_rewrite"}
            or not (score.get("unsupported_claims") or source_grounded_repair(score))
            or not any(p.get("original_passages") for p in evidence.get("evidence") or []
                       if isinstance(p, dict))):
        return ""
    return (
        "FACTUAL REPAIR takes precedence over preserving the diagnosed erroneous relationship. "
        "For each diagnosed evidence defect, use the diagnosis and cited local passages to correct "
        "its subject, study attribution or collective scope. Reassign a finding only when the supplied "
        "context explicitly establishes its owner; otherwise narrow or remove the unsupported assertion. "
        "Preserve unrelated supported facts and all citation callouts, images and figure metadata. "
        "Do not invent identities or numerical values, or move a result to a different source. "
        "Changing only hedging or adding an evidence disclaimer does not fix wrong attribution. "
        "Check the revised assertion against the cited context before returning the paragraph. "
        "Make the smallest sentence-level change that fixes the target. Preserve unrelated sentences; "
        "adjust an adjacent sentence only when needed to make the attribution explicit. "
        + SOURCE_ATTRIBUTION_POLICY
    )
