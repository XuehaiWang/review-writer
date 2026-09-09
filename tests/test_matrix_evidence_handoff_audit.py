"""Offline audit of Matrix -> Blueprint -> section evidence handoff.

Run with: .venv/Scripts/python -B -m pytest -v -p no:cacheprovider
          tests/test_matrix_evidence_handoff_audit.py

Production functions are called directly. Search is stubbed to isolate the
fact-card supplement, and Blueprint integration uses a disposable SQLite DB.
Characterization tests explicitly assert current behavior, including defects.
All three diagnosed handoff defects now have regression coverage.
"""

from copy import deepcopy
import ipaddress
import socket
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import httpx2 as httpx

from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.domain_services.library_index import EvidenceHit
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.tests import test_section_assertion_ceiling as ceiling_fixture
from review_writer_api.tests import test_sections_v1 as fixture_module
from review_writer_core.academic_contracts import evidence_key as academic_evidence_key
from review_writer_core.scientific_facts import (
    claim_assertion_ceiling,
    fact_claim_issues,
    fact_is_usable,
)


PAPER = "P001"
LINEAGE = "a" * 64
QUOTE = "The catalyst afforded the product in 87% yield at 298 K."


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This diagnostic must not use external services")

    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        # Windows asyncio implements socketpair using a loopback connection.
        # HTTP transports remain blocked even for localhost model gateways.
        if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
            return original_connect(sock, address)
        return forbidden(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def make_fact(field="quantitative_results", paper=PAPER, **changes):
    fact = {
        "fact_id": f"{paper}-fact-a",
        "paper_id": paper,
        "field_id": field,
        "value": QUOTE,
        "support_excerpt": QUOTE,
        "support_level": "direct",
        "source_channel": "body",
        "confidence": 0.95,
        "validation_contract": "audit-source-bound-v1",
        "verification": {"status": "supported"},
        "evidence_refs": [{
            "evidence_key": academic_evidence_key(paper, "chunk-a", LINEAGE),
            "chunk_id": "chunk-a",
            "source_file_id": paper,
            "source_lineage_hash": LINEAGE,
            "page_start": 3,
            "support_excerpt": QUOTE,
        }],
    }
    fact.update(changes)
    return fact


def make_index(papers=(PAPER,), *, fulltext=False):
    return SimpleNamespace(
        enabled=True, vector_enabled=False,
        tuning=SimpleNamespace(subsection_top_k=12),
        summaries=Mock(return_value={pid: {
            "fulltext": "ready" if fulltext else "not_indexed",
            "chunk_count": 1 if fulltext else 0,
            "source_lineage_hash": LINEAGE,
        } for pid in papers}),
        retrieve=Mock(return_value=[]),
        primary_coverage_hits=Mock(return_value=[]),
        ensure_embeddings=Mock(side_effect=AssertionError("No embeddings allowed")),
    )


def package(facts, *, added_question=None, task_updates=None, index=None):
    service = SectionsService.__new__(SectionsService)
    service.library_index = index or make_index()
    service._owned_project = Mock(return_value=SimpleNamespace(topic="Catalytic synthesis"))
    task = {
        "section_id": "S02", "section_role": "body",
        "heading": "Catalytic synthesis", "core_argument": "Compare reported results.",
        "primary_papers": [PAPER], "allowed_papers": [PAPER],
        "required_fact_roles": ["object_input", "method_conditions", "quantitative_results", "scope"],
        "scientific_claims": [],
    }
    task.update(task_updates or {})
    plans = service._evidence_queries(task, review_topic="Catalytic synthesis")
    if added_question:
        extra = deepcopy(plans[0])
        extra["question_id"] = added_question
        plans.append(extra)
        service._evidence_queries = Mock(return_value=plans)
    original = deepcopy(facts)
    result = service._evidence_package(
        SimpleNamespace(user_id="offline-owner"), "offline-project", [task],
        {PAPER: SimpleNamespace(title="Audit paper", metadata_json={})},
        {PAPER: {"paper_id": PAPER, "scientific_facts": facts}},
    )["sections"][0]
    assert facts == original, "Reading facts must not mutate Matrix input"
    service.library_index.ensure_embeddings.assert_not_called()
    return result


def test_control_matching_field_preserves_source_and_claim_binding():
    fact = make_fact()
    section = package([fact])
    assert len(section["hits"]) == 1
    hit = section["hits"][0]
    assert hit["claim_eligible"]
    assert hit["fact_ids"] == [fact["fact_id"]]
    assert hit["evidence_key"] == fact["evidence_refs"][0]["evidence_key"]
    assert hit["content"] == QUOTE
    assert hit["index_id"] is None  # Saved excerpt; not a fresh chunk lookup.


def test_missing_fact_ceiling_fails_closed_in_section_evidence():
    hit = package([make_fact()])["hits"][0]
    assert hit["assertion_ceiling"] == "context_only"
    assert claim_assertion_ceiling([hit], hit["fact_bindings"]) == "context_only"


def test_unresolved_targeted_fact_gap_records_a_bounded_stop_reason():
    section = package(
        [],
        task_updates={
            "required_fact_roles": ["mechanism"],
            "targeted_fact_gaps": {PAPER: ["mechanism"]},
        },
    )
    assert section["targeted_fact_gap_outcomes"] == [
        {
            "field_id": "mechanism",
            "requested_papers": [PAPER],
            "matched_papers": [],
            "unresolved_papers": [PAPER],
            "status": "not_found",
            "stop_reason": "bounded_retrieval_exhausted",
        }
    ]


def test_field_alias_or_unknown_name_does_not_drop_valid_fact():
    fact = make_fact()
    control = package([fact])
    renamed = {**fact, "field_id": "custom_measurement"}
    assert fact_is_usable(renamed, purpose="detail")
    retained = package([renamed])
    admitted = package([renamed], added_question="custom_measurement")
    assert len(control["hits"]) == len(admitted["hits"]) == len(retained["hits"]) == 1
    assert retained["hits"][0]["fact_routes"][0]["original_field_id"] == "custom_measurement"
    assert retained["hits"][0]["question_ids"] == ["quantitative_results"]


def test_relevant_direct_fact_survives_independent_field_name():
    section = package([make_fact("custom_measurement")])
    assert section["claim_eligible_hit_count"] == 1


def test_unresolved_fact_is_retained_without_filling_question_or_claim_gaps():
    fact = make_fact("novel_dimension", value="The intervention used protocol Alpha.")
    index = make_index()
    section = package([fact], index=index, task_updates={"scientific_claims": [{
        "claim_id": "claim-one", "proposition": "The protocol improves external validation accuracy.",
        "required_for_section": True,
    }]})
    hit = section["hits"][0]
    assert hit["claim_eligible"]
    assert hit["question_ids"] == []
    assert hit["fact_routes"][0]["status"] == "needs_semantic_planning"
    assert all(not question["matched_papers"] for question in section["query_plans"])
    assert section["scientific_claim_states"][0]["status"] not in {"evidence_supported", "partially_supported"}
    control_index = make_index()
    control = package([], index=control_index, task_updates={"scientific_claims": [{
        "claim_id": "claim-one", "proposition": "The protocol improves external validation accuracy.",
        "required_for_section": True,
    }]})
    assert len(section["query_plans"]) == len(control["query_plans"])
    assert index.retrieve.call_count == control_index.retrieve.call_count


@pytest.mark.parametrize("add_question", [None, "topic_partition"])
def test_classification_is_filtered_even_when_question_id_matches(add_question):
    fact = make_fact("topic_partition")
    assert not fact_is_usable(fact, purpose="detail")
    assert package([fact], added_question=add_question)["hits"] == []


def test_claim_query_ids_do_not_rescue_topic_partition():
    section = package([make_fact("topic_partition")], added_question="required_claim_01")
    assert "required_claim_01" in {q["question_id"] for q in section["query_plans"]}
    assert section["hits"] == []


def test_abstract_summary_can_be_background_but_not_direct_claim_evidence():
    fact = make_fact("abstract_summary", support_level="abstract_limited", source_channel="abstract")
    fact["evidence_refs"][0]["chunk_id"] = "abstract"
    fact["evidence_refs"][0]["evidence_key"] = academic_evidence_key(PAPER, "abstract", LINEAGE)
    assert fact_is_usable(fact) and not fact_is_usable(fact, purpose="detail")
    assert len(package([fact])["hits"]) == 1
    admitted = package([fact], added_question="abstract_summary")
    assert len(admitted["hits"]) == 1
    assert admitted["hits"][0]["background_eligible"]
    assert not admitted["hits"][0]["claim_eligible"]


@pytest.mark.parametrize("status", ["rejected", "pending", "uncertain", "unavailable"])
def test_unverified_facts_remain_excluded(status):
    assert package([make_fact(verification={"status": status})])["hits"] == []


@pytest.mark.parametrize("ref_change", [
    {"source_lineage_hash": "obsolete"},
    {"source_file_id": "unrelated-paper"},
])
def test_lineage_and_source_ownership_guards_are_active(ref_change):
    fact = make_fact()
    fact["evidence_refs"][0].update(ref_change)
    assert package([fact])["hits"] == []


def test_shared_evidence_key_keeps_both_usable_fact_bindings():
    first = make_fact()
    second = make_fact("method_conditions", fact_id="P001-fact-b")
    section = package([first, second])
    assert len(section["hits"]) == 1
    assert set(section["hits"][0]["fact_ids"]) == {first["fact_id"], second["fact_id"]}


def test_background_fact_with_direct_label_is_initially_not_claim_eligible():
    fact = make_fact(source_channel="abstract", epistemic_status="abstract_level_report")
    assert not fact_is_usable(fact, purpose="detail")
    assert not package([fact])["hits"][0]["claim_eligible"]


def test_duplicate_background_fact_never_gains_direct_claim_permission():
    fact = make_fact(source_channel="abstract", epistemic_status="abstract_level_report")
    assert not fact_is_usable(fact, purpose="detail")
    section = package([fact, deepcopy(fact)])
    assert len(section["hits"]) == 1
    assert not section["hits"][0]["claim_eligible"]
    assert section["hits"][0]["support_level"] == "abstract_limited"
    assert section["hits"][0]["assertion_ceiling"] == "abstract_report_only"


@pytest.mark.parametrize("restriction", [
    {"source_channel": "abstract"},
    {"epistemic_status": "abstract_level_report"},
    {"assertion_ceiling": "abstract_report_only"},
    {"support_level": "abstract_limited"},
])
def test_every_background_signal_prevents_claim_and_question_coverage(restriction):
    fact = make_fact(**restriction)
    section = package([fact, deepcopy(fact)])
    hit = section["hits"][0]
    assert not hit["claim_eligible"] and not hit["counts_as_evidence"]
    assert hit["support_level"] == "abstract_limited"
    assert hit["assertion_ceiling"] == "abstract_report_only"
    assert hit["background_eligible"]
    question = next(q for q in section["query_plans"] if q["question_id"] == "quantitative_results")
    assert not question["matched_primary_papers"]
    assert not question["matched_papers"]
    assert not section["writeable_primary_papers"]


@pytest.mark.parametrize("background_first", [False, True])
def test_mixed_source_preserves_direct_evidence_without_upgrading_background_fact(background_first):
    direct = make_fact(assertion_ceiling="direct_source_report")
    background = make_fact(fact_id="P001-background", source_channel="abstract",
                           assertion_ceiling="abstract_report_only")
    ordered = [background, direct] if background_first else [direct, background]
    hit = package(ordered)["hits"][0]
    assert hit["claim_eligible"] and hit["counts_as_evidence"]
    assert hit["support_level"] == "direct"
    assert hit["source_channel"] == "body"
    assert hit["content_type"] == "fact_card"
    assert hit["assertion_ceiling"] == "direct_source_report"
    bindings = {f["fact_id"]: f for f in hit["fact_bindings"]}
    assert fact_is_usable(bindings[direct["fact_id"]], purpose="detail")
    assert not fact_is_usable(bindings[background["fact_id"]], purpose="detail")


def test_background_merge_preserves_real_indexed_table_evidence():
    index = make_index(fulltext=True)
    index.retrieve.return_value = [EvidenceHit(
        paper_id=PAPER, chunk_id="chunk-a", content=QUOTE, page_start=3, page_end=3,
        section_path=("Results",), content_type="table", asset_refs=(), score=1,
        match_reason="query", is_neighbor=False, index_id="existing-index", source_lineage_hash=LINEAGE,
    )]
    background = make_fact(source_channel="abstract", assertion_ceiling="abstract_report_only")
    hit = package([background, deepcopy(background)], index=index)["hits"][0]
    assert hit["claim_eligible"]
    assert hit["source_channel"] == "table"
    assert hit["content_type"] == "table"
    assert hit["assertion_ceiling"] == "direct_report_with_local_context"


def test_downstream_ceiling_check_still_rejects_upgrading_abstract_claims():
    fact = make_fact(source_channel="abstract", epistemic_status="abstract_level_report",
                     assertion_ceiling="abstract_report_only")
    source = package([fact, deepcopy(fact)])["hits"][0]
    args = ceiling_fixture.bundle()
    args[4]["evidence_registry"] = [source]
    args[4]["sections"][0]["hits"] = [source]
    claim = args[3]["sections"][0]["claims"][0]
    claim.update(citation_group=[PAPER], fact_ids=[fact["fact_id"]],
                 evidence_refs=[{"evidence_key": source["evidence_key"]}],
                 assertion_ceiling="abstract_report_only")
    # Verified background is admissible with its limited ceiling, not rejected
    # by a conflicting direct-only publication rule.
    SectionsService._validate_academic_bundle(*args)
    # Old stored artifacts can still carry the pre-fix eligibility flag.
    # Their existing assertion-ceiling safeguard must remain effective.
    source["claim_eligible"] = True
    SectionsService._validate_academic_bundle(*args)
    claim["assertion_ceiling"] = "direct_source_report"
    with pytest.raises(WorkflowValidationError, match="assertion ceiling"):
        SectionsService._validate_academic_bundle(*args)
    assert "abstract_detail_requires_full_text" in fact_claim_issues(QUOTE, [fact], claim_kind="reported_method")


@pytest.fixture
def isolated_planning():
    case = fixture_module.SectionsV1Tests(methodName="test_payload_resolves_blueprint_papers")
    try:
        case.setUp()
        yield case
    finally:
        if hasattr(case, "engine"):
            case.tearDown()


def blueprint_readiness(case, fact, *, fulltext=False):
    service = case.app.state.planning_service
    matrix, artifact = service._matrix(case.first, case.project_id)
    matrix = deepcopy(matrix)
    for row in matrix["rows"]:
        # Do not let fixture abstracts mask the fact-specific readiness path.
        row["abstract"] = ""
        copied = deepcopy(fact)
        copied["paper_id"] = row["paper_id"]
        if fact:
            copied["fact_id"] = f"{row['paper_id']}-fact-a"
            for ref in copied["evidence_refs"]:
                ref["source_file_id"] = row["paper_id"]
                ref["evidence_key"] = academic_evidence_key(row["paper_id"], ref["chunk_id"], LINEAGE)
        row["scientific_facts"] = [copied] if fact else []
    index = make_index([row["paper_id"] for row in matrix["rows"]], fulltext=fulltext)
    state = service.repository.get_stage_state(case.first.user_id, case.project_id, "blueprint")
    with patch.object(service, "_matrix", return_value=(matrix, artifact)), patch.object(service, "library_index", index):
        prepared = service.prepare_blueprint(case.first, case.project_id, revision=state.revision)
    index.ensure_embeddings.assert_not_called()
    body = [s for s in prepared["section_blueprint"]["sections"] if s["section_role"] == "body"]
    assert len(body) == 1
    assert not body[0]["generation_eligible"]  # The chapter planning call has not run yet.
    return body[0]["evidence_readiness"]


def test_optional_fact_reaches_sections_without_blueprint_evidence_gate(isolated_planning):
    fact = make_fact("custom_measurement")
    ready = blueprint_readiness(isolated_planning, fact)
    section = package([fact])
    assert ready["status"] == "not_reviewed"
    assert "writeable_primary_papers" not in ready
    assert len(section["hits"]) == 1
    assert section["writeable_primary_papers"] == [PAPER]


def test_classification_without_fulltext_is_not_ready(isolated_planning):
    assert blueprint_readiness(isolated_planning, make_fact("topic_partition"))["status"] != "ready"


@pytest.mark.parametrize("status", ["rejected", "pending", "uncertain", "unavailable"])
def test_unverified_fact_without_fulltext_does_not_make_blueprint_ready(isolated_planning, status):
    ready = blueprint_readiness(isolated_planning, make_fact(verification={"status": status}))
    assert ready["status"] != "ready"
    assert "writeable_primary_papers" not in ready


def test_background_fact_is_context_not_direct_readiness(isolated_planning):
    ready = blueprint_readiness(isolated_planning, make_fact(source_channel="abstract"))
    assert ready["status"] == "not_reviewed"
    assert "writeable_primary_papers" not in ready
    assert "context_only_primary_papers" not in ready


def test_optional_direct_fact_does_not_trigger_blueprint_evidence_audit(isolated_planning):
    assert blueprint_readiness(isolated_planning, make_fact())["status"] == "not_reviewed"


def test_no_facts_and_no_index_is_not_ready(isolated_planning):
    assert blueprint_readiness(isolated_planning, {})["status"] != "ready"


def test_fulltext_does_not_trigger_blueprint_evidence_audit(isolated_planning):
    assert blueprint_readiness(isolated_planning, {}, fulltext=True)["status"] == "not_reviewed"
