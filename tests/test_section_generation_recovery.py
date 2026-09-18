from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_core.stages.sections.coverage import (
    missing_primary_papers, required_primary_papers, reusable_section_entries,
)
from review_writer_core.scientific_facts import attach_fact_to_evidence
from review_writer_core.stages.sections.evidence_resolution import pending_markdown, resolution_record


def test_pending_checkpoint_reuses_only_canonical_notice_with_unchanged_evidence():
    package = {"retrieval_mode": "insufficient_evidence", "hits": []}
    output = {"section_id": "s", "heading": "Results", "generation_mode": "pending_evidence",
              "paragraphs": [], "draft_md": pending_markdown("s", "Results"),
              "evidence_resolution": resolution_record(package, pending=True, reason="No usable source")}
    entry = {"output": output, "writing": {"claims": [], "paragraphs": []}, "synthesis": {"components": []}}
    tasks = [{"section_id": "s", "primary_papers": ["paper-a"]}]
    kept, rejected = reusable_section_entries({"s": entry}, tasks, {"s": package})
    assert set(kept) == {"s"} and not rejected
    changed = {**package, "hits": [{"evidence_key": "new-key"}]}
    assert not reusable_section_entries({"s": entry}, tasks, {"s": changed})[0]
    output["draft_md"] += "Unsupported conclusion."
    assert not reusable_section_entries({"s": entry}, tasks, {"s": package})[0]


def checkpoint_entry(paper="paper-a"):
    return {"output": {"draft_md": "A supported result.", "paragraphs": [{
        "text": "A supported result.", "cited_paper_ids": [paper],
        "evidence": [{"paper_id": paper, "chunk_ids": ["chunk-a"], "claim": "A supported result."}],
    }]}, "synthesis": {}, "writing": {}}


def test_explicit_empty_writeable_set_is_not_replaced_by_assigned_papers():
    assert required_primary_papers({"primary_papers": ["unresolved"]}, {"writeable_primary_papers": []}) == []


def test_declared_citations_and_empty_prose_do_not_count_as_validated_coverage():
    paragraph = {"text": "Study comparison.", "cited_paper_ids": ["a", "b"],
                 "evidence": [{"paper_id": "a", "chunk_ids": ["c"], "claim": "Result."}]}
    assert missing_primary_papers(["a", "b"], [paragraph], require_evidence=True) == ["b"]
    assert missing_primary_papers(["a", "b"], [paragraph], require_evidence=False) == []
    assert missing_primary_papers(["a"], [{**paragraph, "text": ""}], require_evidence=True) == ["a"]


def test_retry_preserves_good_sections_but_invalidates_incomplete_body_and_dependent_conclusion():
    tasks = [{"section_id": "intro", "section_role": "introduction"},
             {"section_id": "body", "primary_papers": ["paper-a", "paper-b"]},
             {"section_id": "other", "primary_papers": ["paper-a"]},
             {"section_id": "end", "section_role": "conclusion"}]
    entries = {t["section_id"]: checkpoint_entry() for t in tasks}
    before = deepcopy(entries)
    kept, rejected = reusable_section_entries(entries, tasks, {"body": {"retrieval_mode": "lexical"}})
    assert set(kept) == {"intro", "other"}
    assert set(rejected) == {"body", "end"}
    assert "paper-b" in rejected["body"]
    assert entries == before


def test_complete_checkpoint_is_reused_without_discarding_conclusion():
    tasks = [{"section_id": "body", "primary_papers": ["paper-a"]},
             {"section_id": "end", "section_role": "conclusion"}]
    entries = {t["section_id"]: checkpoint_entry() for t in tasks}
    assert reusable_section_entries(entries, tasks, {}) == (entries, {})


def test_checkpoint_is_invalidated_when_supported_blueprint_claim_set_changes():
    source = {
        "evidence_key": "key",
        "paper_id": "paper-a",
        "chunk_id": "chunk-a",
        "claim_eligible": True,
        "content": "The study reports a supported result.",
    }
    fact = {
        "fact_id": "F1",
        "paper_id": "paper-a",
        "value": source["content"],
        "support_level": "direct",
        "verification": {"status": "supported"},
        "evidence_refs": [{"evidence_key": "key"}],
    }
    attach_fact_to_evidence(source, fact)
    task = {
        "section_id": "body",
        "section_role": "body",
        "primary_papers": ["paper-a"],
        "scientific_claims": [
            {
                "claim_id": "C1",
                "proposition": source["content"],
                "primary_papers": ["paper-a"],
                "fact_ids": ["F1"],
                "evidence_refs": [{"evidence_key": "key"}],
                "support_status": "supported",
                "allowed_assertion": source["content"],
                "assertion_ceiling": "direct_source_report",
            }
        ],
    }
    entry = checkpoint_entry()
    entry["writing"] = {"claims": []}
    package = {
        "retrieval_mode": "lexical",
        "hits": [source],
        "scientific_claim_states": [
            {"claim_id": "C1", "status": "evidence_supported"}
        ],
    }

    kept, rejected = reusable_section_entries(
        {"body": entry}, [task], {"body": package}
    )

    assert kept == {}
    assert "Blueprint Claim coverage changed" in rejected["body"]


def test_checkpoint_is_invalidated_when_claim_id_survives_but_fact_binding_changed():
    source = {
        "evidence_key": "current-key",
        "paper_id": "paper-a",
        "chunk_id": "chunk-a",
        "claim_eligible": True,
        "content": "Current source evidence.",
        "fact_ids": ["CURRENT-FACT"],
    }
    task = {
        "section_id": "body",
        "section_role": "body",
        "primary_papers": ["paper-a"],
    }
    entry = checkpoint_entry()
    entry["writing"] = {
        "claims": [
            {
                "claim_id": "C1",
                "fact_ids": ["OLD-FACT"],
                "evidence_refs": [{"evidence_key": "current-key"}],
            }
        ]
    }
    package = {"retrieval_mode": "lexical", "hits": [source]}

    kept, rejected = reusable_section_entries(
        {"body": entry}, [task], {"body": package}
    )

    assert kept == {}
    assert "Claim fact/evidence binding changed" in rejected["body"]


@pytest.mark.parametrize("mode", ["lexical", "lexical+draft_targeted_source_recheck"])
def test_repaired_mode_checkpoint_still_requires_source_coverage(mode):
    entry = checkpoint_entry()
    entry["output"]["paragraphs"][0]["evidence"] = []
    kept, rejected = reusable_section_entries({"s": entry},
        [{"section_id": "s", "primary_papers": ["paper-a"]}], {"s": {"retrieval_mode": mode}})
    assert kept == {}
    assert "s" in rejected


def test_unknown_mode_cannot_reuse_a_checkpoint_without_source_checks():
    kept, rejected = reusable_section_entries({"s": checkpoint_entry()}, [{"section_id": "s"}],
                                              {"s": {"retrieval_mode": "lexical+unknown"}})
    assert kept == {}
    assert "Unsupported section retrieval mode" in rejected["s"]


def test_background_cannot_replace_direct_paper_coverage_even_with_a_shared_chunk():
    source = {"evidence_key": "key", "paper_id": "paper-a", "chunk_id": "chunk-a",
              "claim_eligible": True, "content": "The study reports an approach."}
    for fid, support in (("background", "abstract_limited"), ("direct", "direct")):
        attach_fact_to_evidence(source, {"fact_id": fid, "paper_id": "paper-a", "value": source["content"],
            "support_level": support, "evidence_refs": [{"evidence_key": "key"}]})
    paragraph = {"text": source["content"], "cited_paper_ids": ["paper-a"], "claim_realizations": [{
        "claim_id": "c", "fact_ids": ["background"], "citation_group": ["paper-a"],
        "evidence_refs": [{"evidence_key": "key"}]}]}
    assert missing_primary_papers(["paper-a"], [paragraph], require_evidence=True, source_evidence=[source]) == ["paper-a"]
    paragraph["claim_realizations"][0]["fact_ids"] = ["direct"]
    assert missing_primary_papers(["paper-a"], [paragraph], require_evidence=True, source_evidence=[source]) == []


def test_stale_queued_job_never_enters_scientific_builder():
    from review_writer_api.errors import WorkflowConflict
    from review_writer_api.job_handlers.stage_execution import register_sections_handler
    handlers = {}
    jobs = SimpleNamespace(register_handler=lambda name, handler: handlers.update({name: handler}))
    service, builder = Mock(), Mock()
    service.validate_generation_inputs.side_effect = WorkflowConflict("Planning changed")
    context = Mock(user_id="user", project_id="project", retry_of_job_id=None)
    register_sections_handler(service, jobs, {"sections.generate": builder})
    with pytest.raises(WorkflowConflict):
        handlers["sections.generate"](context, {"tasks": []})
    builder.assert_not_called()
    service.publish_generation.assert_not_called()


def test_retry_defers_standalone_conclusion_without_changing_body_checkpoint():
    from review_writer_api.job_handlers.stage_execution import register_sections_handler

    handlers = {}
    jobs = SimpleNamespace(register_handler=lambda name, handler: handlers.update({name: handler}))
    service, builder = Mock(), Mock(return_value={})
    comparison = {"section_id": "compare", "section_role": "body", "heading": "Comparison and outlook"}
    tasks = [comparison, {"section_id": "end", "section_role": "conclusion"}]
    checkpoint = {"entries": {"compare": {"input_fingerprint": "unchanged", "output": {}}}}
    previous = SimpleNamespace(result={"section_checkpoint": checkpoint}, retry_of_job_id=None)
    context = Mock(user_id="user", project_id="project", retry_of_job_id="old")
    context.repository.get_job.return_value = previous
    register_sections_handler(service, jobs, {"sections.generate": builder})
    handlers["sections.generate"](context, {"tasks": tasks})
    payload = builder.call_args.args[1]
    assert payload["tasks"] == [comparison]
    assert payload["resume_checkpoint"]["entries"] == checkpoint["entries"]
    assert service.publish_generation.call_args.args[2]["tasks"] == [comparison]
    assert len(tasks) == 2  # Historical input snapshot is not mutated.


def test_publication_failure_corrects_completed_progress_and_next_retry_checkpoint():
    from review_writer_api.errors import WorkflowValidationError
    from review_writer_api.job_handlers.stage_execution import register_sections_handler

    handlers = {}
    jobs = SimpleNamespace(register_handler=lambda name, handler: handlers.update({name: handler}))
    service = Mock()
    error = WorkflowValidationError("bad source in body", details={"section_id": "body"})
    service.publish_generation.side_effect = error
    tasks = [{"section_id": "intro", "section_role": "introduction"},
             {"section_id": "body", "primary_papers": ["paper-a"]},
             {"section_id": "end", "section_role": "conclusion"}]
    result = {"section_checkpoint": {"entries": {t["section_id"]: checkpoint_entry() for t in tasks}},
              "section_progress": {"completed_sections": deepcopy(tasks)}}
    context = Mock(user_id="user", project_id="project", job_id="job", retry_of_job_id=None)
    context.repository.get_job.return_value = SimpleNamespace(result=result)
    register_sections_handler(service, jobs, {"sections.generate": lambda *args: {}})
    with pytest.raises(WorkflowValidationError, match="bad source"):
        handlers["sections.generate"](context, {"tasks": tasks})
    snapshot = context.report_partial_result.call_args.args[0]
    assert list(snapshot["section_checkpoint"]["entries"]) == ["intro"]
    assert {x["section_id"] for x in snapshot["section_progress"]["failed_sections"]} == {"body"}
    assert [x["section_id"] for x in snapshot["section_progress"]["completed_sections"]] == ["intro"]
    assert snapshot["section_progress"]["phase"] == "publication_failed"


def test_retry_merges_completed_sections_across_job_ancestry():
    from review_writer_api.job_handlers.stage_execution import (
        section_retry_checkpoints,
    )

    jobs = {
        "latest": SimpleNamespace(
            retry_of_job_id="parent",
            result={
                "section_checkpoint": {
                    "project_id": "project",
                    "entries": {
                        "S01": {"output": {"draft_md": "latest"}},
                        "S02": {"output": {"draft_md": "repaired"}},
                    },
                }
            },
        ),
        "parent": SimpleNamespace(
            retry_of_job_id=None,
            result={
                "section_checkpoint": {
                    "project_id": "project",
                    "entries": {
                        "S01": {"output": {"draft_md": "older"}},
                        "S03": {"output": {"draft_md": "preserved"}},
                    },
                },
                "matrix_enrichment_checkpoint": {"entries": {"P1": {}}},
            },
        ),
    }
    context = SimpleNamespace(
        retry_of_job_id="latest",
        user_id="user",
        repository=SimpleNamespace(
            get_job=lambda _user_id, job_id: jobs.get(job_id)
        ),
    )

    sections, facts = section_retry_checkpoints(context)

    assert list(sections["entries"]) == ["S01", "S02", "S03"]
    assert sections["entries"]["S01"]["output"]["draft_md"] == "latest"
    assert facts == {"entries": {"P1": {}}}


def test_retry_does_not_resurrect_sections_invalidated_by_nearer_job():
    from review_writer_api.job_handlers.stage_execution import (
        section_retry_checkpoints,
    )

    jobs = {
        "latest": SimpleNamespace(
            retry_of_job_id="parent",
            result={
                "section_checkpoint": {
                    "entries": {"S01": checkpoint_entry()},
                    "rejected_entries": {"S02": "publication failed"},
                },
                "section_progress": {
                    "failed_sections": [{"section_id": "S06"}]
                },
            },
        ),
        "parent": SimpleNamespace(
            retry_of_job_id=None,
            result={
                "section_checkpoint": {
                    "entries": {
                        "S02": checkpoint_entry(),
                        "S03": checkpoint_entry(),
                        "S06": checkpoint_entry(),
                    }
                }
            },
        ),
    }
    context = SimpleNamespace(
        retry_of_job_id="latest",
        user_id="user",
        repository=SimpleNamespace(
            get_job=lambda _user_id, job_id: jobs.get(job_id)
        ),
    )

    sections, _facts = section_retry_checkpoints(context)

    assert set(sections["entries"]) == {"S01", "S03"}


def test_section_handler_publishes_against_builder_refreshed_payload():
    from review_writer_api.job_handlers.stage_execution import register_sections_handler

    handlers = {}
    jobs = SimpleNamespace(
        register_handler=lambda name, handler: handlers.update({name: handler})
    )
    service = Mock()
    queued = {"tasks": [], "evidence_package": {"version": "queued"}}
    refreshed = {"tasks": [], "evidence_package": {"version": "refreshed"}}
    built = {"_publication_payload": refreshed}
    context = Mock(
        user_id="user",
        project_id="project",
        job_id="job",
        retry_of_job_id=None,
    )
    register_sections_handler(
        service, jobs, {"sections.generate": lambda *_args: built}
    )

    handlers["sections.generate"](context, queued)

    assert service.publish_generation.call_args.args[2] is refreshed
