"""Shared semantic attribution guidance, independent of discipline or compounds."""

from .quality_rules import finding_category

CONTRIBUTION_WRITING_POLICY = (
    "Understand each paper's research problem and actual contribution before choosing examples. "
    "A passage located in a paper does not prove that paper owns the finding. Distinguish own results, "
    "prior work and unknown ownership using context and citations, never section names alone. "
    "Contribution summaries are navigation, not independently verified evidence. Never infer priority "
    "or historical influence from publication order. Preserve counterexamples. "
    "Write new scientific prose organized around the question, not copied source sentences, translations "
    "or synonym substitution. Preserve the meaning and experimental scope of details you choose to report; "
    "this is not a requirement to reproduce every technical name, formula or quantity. Exact quotations "
    "belong only in evidence fields. If a technical name or formula is visibly incomplete in the supplied "
    "text, use a clearly supported intact occurrence; otherwise use an accurate broader description or "
    "omit the nonessential detail. Never guess missing subscripts, identifiers or identities. If the "
    "missing identity is essential to a comparison, qualify that comparison rather than fabricate precision."
)

SECTION_THREAD_POLICY = (
    "Write a chemistry review around the section question, not consecutive paper summaries or a "
    "mechanical comparison checklist. Plans and paragraph tasks are provisional, mergeable navigation; "
    "when absent, reason directly from the supplied evidence. Give each important experiment one main "
    "explanatory place. Revisit it only to advance a distinct supported implication, without repeating "
    "conditions and results. Keep the smallest sufficient set of numerical examples for the argument, "
    "while retaining decisive exceptions, qualifications and explicit user-requested detail. "
    "Use structure, chronology, mechanism or cross-study comparison only where the sources support that "
    "relationship; a later date or a transition word cannot establish progress or causality. "
    "Distinguish observations, source-author interpretations and bounded review inferences. "
    "Sparse evidence may need one compact discussion rather than an introduction-comparison-limitations-recap "
    "pattern. A closing summary is optional; omit it when it repeats the opening or examples. "
    "Avoid habitual stock openings or endings such as 'Taken together' and repeated disclaimers. "
    "Never expose internal labels such as fact cards or evidence packages in manuscript prose."
)


def contribution_context(analysis, *, limit=900, review_topic=None):
    """One compact navigation projection shared by planning and writing."""
    if not isinstance(analysis, dict):
        return {}
    result = {key: " ".join(str(analysis.get(key) or "").split())[:limit]
              for key in ("research_question", "contribution", "topic_relation")}
    if review_topic is not None and " ".join(str(analysis.get("review_topic") or "").casefold().split()) != " ".join(str(review_topic).casefold().split()):
        # A source reading is reusable across projects; its former project's
        # relevance judgment is not. The planner derives that relation anew.
        result["topic_relation"] = ""
    if not any(result.values()):
        return {}
    return {**result, "usage": "navigation_only", "fact_ids": list(analysis.get("fact_ids") or []),
            "evidence_keys": list(dict.fromkeys(key for key in analysis.get("evidence_keys") or []
                                               if isinstance(key, str) and key))}


def navigation_source_refs(row):
    """Source locators for navigation, including unverified reading candidates.

    This does not assert eligibility or ownership. Callers must check the
    source belongs to this paper/user and resolve its current lineage before
    exposing any source text. Never turn these refs into a verified binding.
    """
    analysis = row.get("paper_analysis") or {}
    refs = list(analysis.get("evidence_refs") or [])
    for fact in row.get("scientific_facts") or []:
        if isinstance(fact, dict):
            refs.extend(fact.get("evidence_refs") or [])
    found = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        key = str(ref.get("evidence_key") or "")
        source_id = str(ref.get("source_file_id") or ref.get("paper_id") or row.get("paper_id") or "")
        chunk_id = str(ref.get("chunk_id") or "")
        lineage = str(ref.get("source_lineage_hash") or "")
        if key and source_id and chunk_id and lineage:
            found.setdefault(key, {**ref, "paper_id": str(row.get("paper_id") or source_id),
                                   "source_file_id": source_id, "chunk_id": chunk_id,
                                   "source_lineage_hash": lineage})
    return list(found.values())


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
