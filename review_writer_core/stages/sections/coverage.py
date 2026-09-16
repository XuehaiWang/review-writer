"""Shared primary-paper coverage contract for generation, resume and publication."""
from __future__ import annotations

from review_writer_core.evidence_integrity import normalize_retrieval_mode
from review_writer_core.claim_contracts import (
    claim_is_executable,
    scientific_claim_evidence_state,
)
from review_writer_core.scientific_facts import fact_usage, registered_fact_bindings
from review_writer_core.stages.sections.source_writing import CONTRACT as SOURCE_CONTRACT, valid_source_claim, fingerprint
from review_writer_core.stages.sections.evidence_resolution import has_evidence_resolution, valid_pending_output


def section_input_fingerprints(tasks, evidence_sections, matrix, blueprint, shared):
    """Section-local authoring inputs; conclusions also depend on all body inputs.

    Citation numbering is global: changing paper order invalidates every section.
    Shared scope, rules and outline remain global by design, not best-effort guesses.
    """
    rows = matrix.get("rows", []) if isinstance(matrix, dict) else matrix
    by_paper = {str(row.get("paper_id")): row for row in rows or [] if isinstance(row, dict)}
    specs = {str(s.get("section_id")): s for s in blueprint.get("sections") or []}
    common = fingerprint({"contract": "section-input/1", "shared": shared,
        "blueprint": {k: v for k, v in blueprint.items() if k != "sections"},
        "paper_order": list(by_paper)})
    signatures = {}
    for task in tasks:
        sid = str(task.get("section_id"))
        papers = task.get("allowed_papers", [*(task.get("primary_papers") or []), *(task.get("supporting_papers") or [])])
        signatures[sid] = fingerprint({"common": common, "task": task,
            "section": specs.get(sid), "evidence": evidence_sections.get(sid),
            "papers": {pid: by_paper.get(pid) for pid in papers}})
    body = {str(t.get("section_id")): signatures[str(t.get("section_id"))]
            for t in tasks if t.get("section_role", "body") == "body"}
    for task in tasks:
        if task.get("section_role") == "conclusion":
            sid = str(task.get("section_id"))
            signatures[sid] = fingerprint({"section": signatures[sid], "body": body})
    return signatures


def matching_section_inputs(entries, signatures, *, legacy_validated=False):
    """An old caller validation may admit legacy entries, never override a new mismatch."""
    accepted, rejected = {}, {}
    for sid, entry in entries.items():
        if not isinstance(entry, dict) or sid not in signatures:
            continue
        saved = entry.get("input_fingerprint")
        if saved == signatures[sid] or (not saved and legacy_validated):
            accepted[sid] = entry
        else:
            rejected[sid] = "Section authoring inputs changed."
    return accepted, rejected


def direct_claim_papers(claim, sources, facts):
    """A writable background Claim cannot fulfil a primary-paper obligation."""
    keys = {str(ref.get("evidence_key")) for ref in claim.get("evidence_refs") or [] if isinstance(ref, dict)}
    cited = set(str(pid) for pid in claim.get("citation_group") or [])
    if claim.get("fact_ids"):
        return {str(fact.get("paper_id")) for fid in claim["fact_ids"]
                if (fact := facts.get(str(fid))) and fact_usage(fact) == "direct"
                and str(fact.get("paper_id")) in cited
                and any(str(ref.get("evidence_key")) in keys for ref in fact.get("evidence_refs") or [])}
    return {str(sources[key].get("paper_id")) for key in keys if key in sources
            and sources[key].get("claim_eligible", True)
            and str(sources[key].get("paper_id")) in cited}

def required_primary_papers(task, package):
    values = (package.get("writeable_primary_papers") if "writeable_primary_papers" in package
              else task.get("primary_papers"))
    return list(dict.fromkeys(str(value) for value in values or [] if value))


def supported_scientific_claim_ids(task, package, *, required_only=False):
    """Return the executable Blueprint Claims supported by this exact package."""

    evidence = [
        row for row in package.get("hits") or [] if isinstance(row, dict)
    ]
    return {
        str(claim.get("claim_id") or "")
        for claim in task.get("scientific_claims") or []
        if isinstance(claim, dict)
        and str(claim.get("claim_id") or "")
        and claim_is_executable(claim)
        and (not required_only or claim.get("required_for_section", True))
        and scientific_claim_evidence_state(claim, evidence).get("status")
        == "evidence_supported"
    }


def claim_fact_identity_gaps(claim, sources):
    """Return selected fact IDs absent from the Claim's exact evidence keys."""

    keys = {
        str(ref.get("evidence_key") or "")
        for ref in claim.get("evidence_refs") or []
        if isinstance(ref, dict) and str(ref.get("evidence_key") or "")
    }
    available = {
        str(fact_id)
        for key in keys
        for fact_id in (sources.get(key) or {}).get("fact_ids") or []
        if str(fact_id)
    }
    return {
        str(fact_id)
        for fact_id in claim.get("fact_ids") or []
        if str(fact_id)
    } - available


def missing_primary_papers(required, paragraphs, *, require_evidence, source_evidence=None):
    covered = set()
    sources = {str(row.get("evidence_key")): row for row in source_evidence or [] if row.get("evidence_key")}
    facts = registered_fact_bindings(sources.values(), {str(row.get("paper_id")) for row in sources.values()})
    direct_chunks = {(str(row.get("paper_id")), str(row.get("chunk_id")))
                     for row in source_evidence or [] if row.get("claim_eligible", True)}
    for paragraph in paragraphs or []:
        if not isinstance(paragraph, dict) or not str(paragraph.get("text") or "").strip():
            continue
        if require_evidence and source_evidence is not None and paragraph.get("claim_realizations"):
            for claim in paragraph["claim_realizations"]:
                covered.update(direct_claim_papers(claim, sources, facts))
            continue
        covered.update(str(item["paper_id"]) for item in paragraph.get("evidence") or []
                       if isinstance(item, dict) and item.get("paper_id")
                       and item.get("chunk_ids") and str(item.get("claim") or "").strip()
                       and (source_evidence is None or not require_evidence or any(
                           (str(item["paper_id"]), str(chunk)) in direct_chunks for chunk in item["chunk_ids"])))
        if not require_evidence:
            covered.update(str(value) for value in (paragraph.get("cited_paper_ids")
                           or [paragraph.get("paper_id")]) if value)
    return [str(paper) for paper in required if str(paper) not in covered]


def reusable_section_entries(entries, tasks, evidence_sections):
    """Reuse validated prose and explicit evidence notices for unchanged inputs.

    The caller must also verify the existing generation fingerprint. This does
    not replace publication's source/claim validation.
    """
    retained = {}
    rejected = {}
    for task in tasks:
        section_id = str(task.get("section_id") or "")
        entry = entries.get(section_id)
        if entry is None:
            continue
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), dict)
                                                  for key in ("output", "synthesis", "writing")):
            rejected[section_id] = "Incomplete section checkpoint."
            continue
        output = entry["output"]
        from review_writer_core.stages.sections.source_writing import BINDING_CONTRACT
        source_review = entry["synthesis"].get("source_review") or {}
        if (source_review.get("binding_contract") != BINDING_CONTRACT
                and any(item.get("reason") == "missing_or_invalid_source_span"
                        for item in source_review.get("omitted") or [] if isinstance(item, dict))):
            rejected[section_id] = "Regenerate legacy source-binding failures with the corrected binding contract."
            continue
        package = evidence_sections.get(section_id, {})
        source_by_key = {
            str(row.get("evidence_key") or ""): row
            for row in package.get("hits") or []
            if isinstance(row, dict) and str(row.get("evidence_key") or "")
        }
        mode = normalize_retrieval_mode(package.get("retrieval_mode"))
        if mode == "unsupported_retrieval_mode":
            rejected[section_id] = f"Unsupported section retrieval mode: {package.get('retrieval_mode')}"
            continue
        degraded = has_evidence_resolution(output, package)
        if valid_pending_output(output, package) and not entry["writing"].get("claims") and not entry["writing"].get("paragraphs") and not entry["synthesis"].get("components"):
            retained[section_id] = entry
            continue
        missing = missing_primary_papers(required_primary_papers(task, package), output.get("paragraphs"),
                                         require_evidence=mode == "lexical", source_evidence=package.get("hits"))
        if not str(output.get("draft_md") or "").strip() or not output.get("paragraphs"):
            rejected[section_id] = "Section checkpoint has no usable prose."
        elif missing and not degraded:
            rejected[section_id] = "Primary papers missing validated evidence: " + ", ".join(missing)
        elif str(task.get("section_role") or "body").casefold() == "body" and (
            invalid_claims := {
                str(claim.get("claim_id") or ""): sorted(
                    claim_fact_identity_gaps(claim, source_by_key)
                )
                for claim in entry["writing"].get("claims") or []
                if isinstance(claim, dict)
                and claim_fact_identity_gaps(claim, source_by_key)
            }
        ):
            rejected[section_id] = (
                "Claim fact/evidence binding changed: "
                + "; ".join(
                    f"{claim_id}={','.join(fact_ids)}"
                    for claim_id, fact_ids in invalid_claims.items()
                )
            )
        elif entry["writing"].get("evidence_mode") == SOURCE_CONTRACT:
            if task.get("section_role") == "conclusion":
                for body_task in tasks:
                    if body_task.get("section_role", "body") == "body":
                        for row in (evidence_sections.get(body_task["section_id"]) or {}).get("hits") or []:
                            if row.get("evidence_key"):
                                source_by_key.setdefault(str(row["evidence_key"]), row)
            realizations = {c.get("claim_id"): c for p in output.get("paragraphs") or [] for c in p.get("claim_realizations") or []}
            if any(not valid_source_claim(c, source_by_key, text=(realizations.get(c.get("claim_id")) or {}).get("text", ""))
                   for c in entry["writing"].get("claims") or []):
                rejected[section_id] = "Source passage or checked claim changed."
            else:
                retained[section_id] = entry
        elif (
            str(task.get("section_role") or "body").casefold() == "body"
            and "scientific_claim_states" in package
        ):
            expected_claim_ids = supported_scientific_claim_ids(task, package)
            checkpoint_claim_ids = {
                str(claim.get("claim_id") or "")
                for claim in entry["writing"].get("claims") or []
                if isinstance(claim, dict) and str(claim.get("claim_id") or "")
            }
            required_claim_ids = supported_scientific_claim_ids(task, package, required_only=True)
            if not checkpoint_claim_ids <= expected_claim_ids or (not degraded and not required_claim_ids <= checkpoint_claim_ids):
                rejected[section_id] = (
                    "Blueprint Claim coverage changed: missing "
                    + ", ".join(sorted(required_claim_ids - checkpoint_claim_ids))
                    + "; obsolete "
                    + ", ".join(sorted(checkpoint_claim_ids - expected_claim_ids))
                )
                continue
            retained[section_id] = entry
        else:
            retained[section_id] = entry
    if any(str(task.get("section_role") or "body").casefold() == "body"
           and str(task.get("section_id")) not in retained for task in tasks):
        for task in tasks:
            section_id = str(task.get("section_id") or "")
            if str(task.get("section_role") or "").casefold() == "conclusion" and section_id in retained:
                retained.pop(section_id)
                rejected[section_id] = "Refresh conclusion after the incomplete body sections are repaired."
    return retained, rejected
