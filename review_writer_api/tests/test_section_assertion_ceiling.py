from copy import deepcopy

import pytest

from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.errors import WorkflowValidationError
from review_writer_core.scientific_facts import attach_fact_to_evidence


def bundle(*, synthesis=False):
    source = {"evidence_key": "source-a", "paper_id": "paper-a", "claim_eligible": True,
              "content": "The authors proposed an explanation.", "assertion_ceiling": "direct_source_report"}
    attach_fact_to_evidence(source, {"fact_id": "fact-a", "paper_id": "paper-a", "value": source["content"],
        "support_level": "direct", "assertion_ceiling": "attributed_author_interpretation",
        "support_excerpt": source["content"], "evidence_refs": [{"evidence_key": "source-a"}]})
    ids = ["body", "end"] if synthesis else ["body"]
    writing = {"planning_mode": "evidence_first", "sections": [{"section_id": sid,
        "paragraphs": [{"paragraph_id": f"{sid}-p1"}], "claims": [{
            "claim_id": f"{sid}-p1-C01", "paragraph_id": f"{sid}-p1", "citation_group": ["paper-a"],
            "fact_ids": ["fact-a"], "evidence_refs": [{"evidence_key": "source-a"}],
            "fact_binding_status": "explicit_fact_selection", "assertion_ceiling": "attributed_author_interpretation",
        }]} for sid in ids]}
    return [
        {"tasks": [{"section_id": sid, "section_role": "synthesis" if sid == "end" else "body"} for sid in ids]},
        {"sections": [{"section_id": sid, "paragraphs": [{"paragraph_id": f"{sid}-p1",
            "claim_realizations": [{"claim_id": f"{sid}-p1-C01"}]}]} for sid in ids]},
        {"sections": [{"section_id": sid, "components": []} for sid in ids]}, writing,
        {"evidence_registry": [deepcopy(source)], "sections": [{"section_id": sid, "retrieval_mode": "lexical",
            "hits": [source]} for sid in ids]},
    ]


def test_synthesis_uses_fact_ceiling_from_explicit_current_evidence():
    args = bundle(synthesis=True)
    before = deepcopy(args)
    SectionsService._validate_academic_bundle(*args)
    assert args == before
    args[3]["sections"][1]["claims"][0]["assertion_ceiling"] = "direct_source_report"
    with pytest.raises(WorkflowValidationError) as caught:
        SectionsService._validate_academic_bundle(*args)
    assert caught.value.details["section_id"] == "end"


def test_explicit_fact_without_valid_source_binding_cannot_gain_direct_ceiling():
    args = bundle()
    # The ID remains in fact_ids, but the alleged support quote is no longer
    # present. Neither a matching ID nor a weaker ceiling can bypass provenance.
    args[4]["sections"][0]["hits"][0]["fact_bindings"][0]["support_excerpt"] = "An unrelated quotation."
    with pytest.raises(WorkflowValidationError, match="not validated") as caught:
        SectionsService._validate_academic_bundle(*args)
    assert caught.value.details["section_id"] == "body"


def test_body_claim_cannot_borrow_another_sections_fact_ceiling():
    args = bundle(synthesis=True)
    args[0]["tasks"][1]["section_role"] = "body"
    args[4]["sections"][1]["hits"] = []
    with pytest.raises(WorkflowValidationError, match="neighbor or coverage-only"):
        SectionsService._validate_academic_bundle(*args)


@pytest.mark.parametrize("mode", ["lexical", "lexical+draft_targeted_source_recheck"])
def test_repaired_mode_cannot_bypass_claim_references(mode):
    args = bundle()
    args[4]["sections"][0]["retrieval_mode"] = mode
    args[3]["sections"][0]["claims"][0].update(evidence_refs=[], fact_ids=[], citation_group=[])
    with pytest.raises(WorkflowValidationError, match="reference at least one"):
        SectionsService._validate_academic_bundle(*args)


def test_unknown_retrieval_mode_is_rejected_explicitly():
    args = bundle()
    args[4]["sections"][0]["retrieval_mode"] = "lexical+unknown"
    with pytest.raises(WorkflowValidationError, match="Unsupported section retrieval mode"):
        SectionsService._validate_academic_bundle(*args)


@pytest.mark.parametrize("synthesis", [False, True])
def test_verified_background_can_be_published_without_becoming_direct_evidence(synthesis):
    args = bundle(synthesis=synthesis)
    source = args[4]["sections"][0]["hits"][0]
    source.update(claim_eligible=False, support_level="abstract_limited", chunk_id="abstract")
    fact = source["fact_bindings"][0]
    fact.update(support_level="abstract_limited", source_channel="abstract", assertion_ceiling="abstract_report_only")
    for section in args[3]["sections"]:
        section["claims"][0].update(assertion_ceiling="abstract_report_only", claim="The authors proposed an explanation.")
    SectionsService._validate_academic_bundle(*args)
    sections = {row["section_id"]: row for row in args[4]["sections"]}
    tasks = {row["section_id"]: row for row in args[0]["tasks"]}
    assert ("paper-a", "abstract") in SectionsService._valid_retrieval_chunks("body", tasks["body"], sections, tasks)
    args[3]["sections"][0]["claims"][0]["claim"] = "The experiment gave 99% yield."
    with pytest.raises(WorkflowValidationError, match="background evidence use"):
        SectionsService._validate_academic_bundle(*args)


def test_rejected_background_binding_does_not_admit_neighbor_context():
    args = bundle()
    source = args[4]["sections"][0]["hits"][0]
    source["claim_eligible"] = False
    source["fact_bindings"][0]["verification"] = {"status": "rejected"}
    with pytest.raises(WorkflowValidationError, match="neighbor or coverage-only"):
        SectionsService._validate_academic_bundle(*args)
