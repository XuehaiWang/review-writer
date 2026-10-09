"""Offline boundaries between first-reading navigation and publishable evidence."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_writer_api.domain_services.sections import SectionsService, academic_evidence_key
from review_writer_api.errors import WorkflowValidationError
from review_writer_core.review_fact_readiness import REVIEW_READING_STRATEGY
from review_writer_core.stages.sections.source_writing import valid_source_claim, write_from_sources


SOURCE_TEXT = "Catalyst A degraded 90% of the pollutant at 25 C after 60 minutes."
SOURCE_CONTEXT = SOURCE_TEXT + " The catalyst failed in the saline control."


def section_task(*, reading=True, section_id="S01"):
    task = {
        "section_id": section_id, "heading": "Catalyst performance", "section_role": "body",
        "primary_papers": ["A"], "supporting_papers": [], "allowed_papers": ["A"],
        "questions_to_answer": ["What was reported?"],
        "writing_objective": "Compare reported conditions.",
    }
    if reading:
        task["reading_strategy"] = REVIEW_READING_STRATEGY
    return task


def source_ref(*, source_id="A", lineage="current-v1", chunk_id="results-2"):
    return {
        "evidence_key": academic_evidence_key(source_id, chunk_id, lineage),
        "source_file_id": source_id, "chunk_id": chunk_id, "source_lineage_hash": lineage,
    }


def reading_row(ref, *, location="candidate"):
    row = {
        "paper_id": "A", "paper_analysis": {"contribution": "A promising pilot result."},
        "scientific_facts": [],
        "fact_enrichment": {"fact_extraction_profile": {
            "reading_strategy": REVIEW_READING_STRATEGY, "reading_status": "completed",
            "reading_source_key": "source-v1",
        }},
    }
    if location == "analysis":
        row["paper_analysis"]["evidence_refs"] = [ref]
    else:
        row["scientific_facts"] = [{
            "fact_id": "candidate-A", "field_id": "quantitative_results",
            "value": "Candidate incorrectly says 100% in every condition.",
            "support_excerpt": "Candidate excerpt omits the saline control.",
            "support_level": "direct", "study_ownership": "own_results",
            "validation_contract": "scientific-fact/1",
            "verification": {"status": "pending"}, "evidence_refs": [ref],
        }]
    return row


class IsolatedIndex:
    """No database, network, embeddings or model provider is involved."""

    enabled = True
    vector_enabled = False
    tuning = SimpleNamespace(subsection_top_k=2)

    def __init__(self):
        self.resolved_calls = []
        self.retrieval_calls = []
        self.resolved_override = None

    def summaries(self, principal, paper_ids):
        return {paper_id: {"fulltext": "ready", "chunk_count": 1,
                           "source_lineage_hash": "current-v1"} for paper_id in paper_ids}

    def retrieve(self, principal, query, **kwargs):
        self.retrieval_calls.append((query, kwargs))
        return []

    def primary_coverage_hits(self, principal, **kwargs):
        return []

    def resolve_source_chunks(self, principal, source_id, chunk_ids, *, expected_lineage):
        self.resolved_calls.append((principal, source_id, chunk_ids, expected_lineage))
        if self.resolved_override is not None:
            return deepcopy(self.resolved_override)
        return [{
            "paper_id": source_id, "chunk_id": chunk_id, "source_lineage_hash": expected_lineage,
            "content": SOURCE_CONTEXT, "page_start": 2, "page_end": 2,
            "section_path": ["Results"], "content_type": "text",
        } for chunk_id in chunk_ids]


@pytest.fixture
def isolated_service():
    service = SectionsService.__new__(SectionsService)
    service.library_index = IsolatedIndex()
    principal = object()

    def owned_project(actual_principal, project_id):
        assert actual_principal is principal and project_id == "isolated-project"
        return SimpleNamespace(topic="Catalyst performance")

    service._owned_project = owned_project
    catalog = {
        "A": SimpleNamespace(title="Catalyst pilot", metadata_json={}),
        "A-SI": SimpleNamespace(title="Pilot supporting information", metadata_json={
            "document_type": "supporting_information", "parent_paper_id": "A"}),
        "OTHER-SI": SimpleNamespace(title="Foreign supporting information", metadata_json={
            "document_type": "supporting_information", "parent_paper_id": "OTHER"}),
    }
    return service, principal, catalog


def evidence_package(fixture, row, *, tasks=None):
    service, principal, catalog = fixture
    return service._evidence_package(principal, "isolated-project", tasks or [section_task()],
                                     catalog, {"A": row})


def test_review_problem_changes_queries_but_empty_matches_keep_original_scope(isolated_service):
    task = {**section_task(), 'review_problem': 'Long-term catalyst stability'}
    row = reading_row(source_ref())
    package = evidence_package(isolated_service, row, tasks=[task])
    service, _, _ = isolated_service
    calls = service.library_index.retrieval_calls
    plans = service._evidence_queries(task, review_topic='Catalyst performance')
    assert plans[0]['question'] == task['review_problem']
    assert any('stability' in query for query, _ in calls)
    assert any(kwargs.get('term_groups') == plans[0]['required_concept_groups'] for _, kwargs in calls)
    assert package['sections'][0]['hits'][0]['content'] == SOURCE_CONTEXT


@pytest.mark.parametrize("location", ["candidate", "analysis"])
def test_navigation_resolves_registered_context_without_certifying_candidate(isolated_service, location):
    row = reading_row(source_ref(), location=location)
    snapshot = deepcopy(row)
    package = evidence_package(isolated_service, row, tasks=[section_task(), section_task(section_id="S02")])

    for section in package["sections"]:
        hit, = section["hits"]
        assert hit["content"] == SOURCE_CONTEXT
        assert hit["evidence_key"] == source_ref()["evidence_key"]
        assert hit["source_lineage_hash"] == "current-v1"
        assert hit["retrieval_passes"] == ["reading_source_navigation"]
        assert hit["source_type"] == "main_article" and hit["paper_id"] == "A"
        assert not hit.get("fact_ids") and not hit.get("fact_bindings")
        assert not hit.get("normalized_fact_value")
    registered, = package["evidence_registry"]
    assert not registered.get("fact_bindings")
    service, principal, _ = isolated_service
    assert service.library_index.resolved_calls == [(principal, "A", ["results-2"], "current-v1")]
    assert len(service.library_index.retrieval_calls) == len({
        json.dumps(call, sort_keys=True) for call in service.library_index.retrieval_calls
    })
    assert row == snapshot


def test_linked_si_is_resolved_as_source_but_cited_to_its_parent(isolated_service):
    package = evidence_package(isolated_service, reading_row(source_ref(source_id="A-SI")))
    hit, = package["sections"][0]["hits"]
    assert hit["paper_id"] == "A" and hit["source_file_id"] == "A-SI"
    assert hit["source_type"] == "supporting_information"
    assert hit["evidence_key"] == source_ref(source_id="A-SI")["evidence_key"]
    assert hit["content"] == SOURCE_CONTEXT
    assert not hit.get("fact_bindings")


@pytest.mark.parametrize("ref", [
    source_ref(lineage="stale-v0"),
    source_ref(source_id="OTHER-SI"),
    source_ref(source_id="MISSING-SI"),
    {**source_ref(), "evidence_key": "sha256:forged"},
    source_ref(chunk_id="abstract"),
    {**source_ref(), "source_lineage_hash": ""},
])
def test_stale_forged_or_foreign_locations_never_resolve(isolated_service, ref):
    package = evidence_package(isolated_service, reading_row(ref))
    assert isolated_service[0].library_index.resolved_calls == []
    assert package["sections"][0]["hits"] == []
    assert package["evidence_registry"] == []


@pytest.mark.parametrize("changes", [
    {"paper_id": "OTHER-SI"}, {"source_lineage_hash": "stale-v0"},
    {"chunk_id": "unrequested-chunk"}, {"content": ""},
])
def test_resolver_response_must_match_requested_identity(isolated_service, changes):
    isolated_service[0].library_index.resolved_override = [{
        "paper_id": "A", "chunk_id": "results-2", "source_lineage_hash": "current-v1",
        "content": SOURCE_CONTEXT, **changes,
    }]
    package = evidence_package(isolated_service, reading_row(source_ref()))
    assert len(isolated_service[0].library_index.resolved_calls) == 1
    assert package["sections"][0]["hits"] == []


def test_legacy_task_does_not_silently_enable_candidate_navigation(isolated_service):
    package = evidence_package(isolated_service, reading_row(source_ref()), tasks=[section_task(reading=False)])
    assert isolated_service[0].library_index.resolved_calls == []
    assert package["sections"][0]["hits"] == []


@pytest.fixture(scope="module")
def drafting_pipeline():
    script = Path(__file__).resolve().parents[2] / "skills/review-section-drafting-figure-picking/scripts/generate_section_drafts.py"
    spec = importlib.util.spec_from_file_location("review_reading_navigation_pipeline", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_bundle(pipeline, *, reading=False, audit_mode="selective"):
    current_task = section_task(reading=reading)
    evidence = [{
        "evidence_key": "a", "evidence_id": "a", "paper_id": "A", "chunk_id": "a",
        "content": SOURCE_TEXT, "source_lineage_hash": "current-v1", "page_start": 2, "page_end": 2,
        "claim_eligible": True, "assertion_ceiling": "direct_source_report", "support_level": "direct",
    }]
    calls = []

    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"text": SOURCE_TEXT, "claims": [{
                "text": SOURCE_TEXT, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": SOURCE_TEXT}],
                "review_reasons": [], "fact_ids": [], "result_context": [],
            }]}]}
        assert label == "section-used-claim-check"
        data = json.loads(prompt[prompt.index('{"claims":'):])
        return {"claims": [{"claim_id": claim["claim_id"], "status": "supported", "text": "", "reason": ""}
                           for claim in data["claims"]]}

    writing, generated, _ = write_from_sources(section_id="S01", task=current_task, evidence=evidence,
                                               context="", call=model, audit_mode=audit_mode)
    overview, paragraphs, _, _ = pipeline.validate_and_realize_section(
        section_id="S01", generated=generated, writing_section=writing, evidence=evidence, citation_map={"A": 1})
    package = {"sections": [{"section_id": "S01", "retrieval_mode": "lexical", "hits": evidence}],
               "evidence_registry": evidence}
    built = {"sections": [{"section_id": "S01", "paragraphs": paragraphs, "overview": overview}]}
    synthesis = {"sections": [{"section_id": "S01", "components": []}]}
    plan = {"planning_mode": "evidence_first_source_writing", "sections": [writing]}
    return current_task, writing, package, built, synthesis, plan, calls


def test_review_reading_publication_rejects_legacy_program_checked_claim(drafting_pipeline):
    task, writing, package, built, synthesis, plan, calls = source_bundle(drafting_pipeline)
    claim, = writing["claims"]
    assert calls == ["section-source-writing"]
    assert claim["source_verification"]["status"] == "program_checked"
    assert valid_source_claim(claim, {"a": package["evidence_registry"][0]})
    legacy = {"tasks": [task], "blueprint": {"schema_version": 2}}
    SectionsService._validate_academic_bundle(legacy, built, synthesis, plan, package)

    current = {**legacy, "tasks": [{**task, "reading_strategy": REVIEW_READING_STRATEGY}]}
    with pytest.raises(WorkflowValidationError, match="actual source audit"):
        SectionsService._validate_academic_bundle(current, built, synthesis, plan, package)


def test_review_reading_checks_used_claim_before_api_publication(drafting_pipeline):
    task, writing, package, built, synthesis, plan, calls = source_bundle(drafting_pipeline, reading=True)
    assert calls == ["section-source-writing", "section-used-claim-check"]
    assert writing["claims"][0]["source_verification"]["status"] == "supported"
    assert writing["claims"][0]["fact_ids"] == []
    SectionsService._validate_academic_bundle({"tasks": [task], "blueprint": {"schema_version": 2}},
                                               built, synthesis, plan, package)
