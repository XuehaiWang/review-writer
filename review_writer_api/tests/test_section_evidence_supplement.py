"""Source supplement host tests: local reads only, no model/provider requests."""

from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from review_writer_api.domain_services.base import OwnedProjectService
from review_writer_api.domain_services.library_index import EvidenceHit
from review_writer_api.domain_services.section_evidence_supplement import SectionEvidenceSupplement
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.security import Principal, Role
from review_writer_core.academic_contracts import evidence_key
from review_writer_core.review_fact_readiness import REVIEW_READING_STRATEGY
from review_writer_core.stages.sections.source_supplement import evidence_mailbox
from review_writer_core.stages.sections.evidence_resolution import has_evidence_resolution, resolution_record


def hit(source, chunk="result", *, neighbor=False):
    return EvidenceHit(paper_id=source, chunk_id=chunk, content=f"Registered {source} {chunk} result.",
        page_start=2, page_end=2, section_path=("Results",), content_type="text", asset_refs=(), score=1,
        match_reason="lexical", is_neighbor=neighbor, index_id="index", source_lineage_hash=f"lineage-{source}")


class Index:
    def __init__(self):
        self.hits = [hit("P1"), hit("SI1"), hit("P2")]
        self.lineages = {sid: f"lineage-{sid}" for sid in ("P1", "P2", "SI1")}
        self.calls = []

    def summaries(self, principal, papers):
        return {pid: {"source_lineage_hash": self.lineages.get(pid)} for pid in papers}

    def retrieve(self, principal, query, **kwargs):
        assert kwargs["use_semantic"] is False
        self.calls.append((query, kwargs))
        return [row for row in self.hits if row.paper_id in kwargs["allowed_papers"]]

    def resolve_source_chunks(self, principal, source, chunks, *, expected_lineage):
        return [asdict(row) for row in self.hits if row.paper_id == source and row.chunk_id in chunks
                and row.source_lineage_hash == expected_lineage == self.lineages[source]]


@pytest.fixture
def prepared(tmp_path):
    catalog = {pid: SimpleNamespace(title=pid, metadata_json={}) for pid in ("P1", "P2", "SI1")}
    catalog["SI1"].metadata_json = {"document_type": "supporting_information", "parent_paper_id": "P1"}
    index = Index()
    service = SimpleNamespace(library_index=index, _catalog=lambda principal, allowed: catalog,
        _supporting_source_parents=OwnedProjectService._supporting_source_parents)
    manifest = {sid: {"paper_id": "P1" if sid == "SI1" else sid, "source_lineage_hash": f"lineage-{sid}"}
                for sid in catalog}
    payload = {"project_id": "project", "tasks": [{"section_id": sid, "reading_strategy": REVIEW_READING_STRATEGY,
        "allowed_papers": ["P1", "P2"]} for sid in ("S1", "S2")], "evidence_package": {
            "sections": [{"section_id": sid, "source_manifest": deepcopy(manifest), "hits": []} for sid in ("S1", "S2")],
            "evidence_registry": []}}
    principal = Principal("user", frozenset({Role.USER}))
    bridge = SectionEvidenceSupplement(service, principal, payload, tmp_path / "mailbox")
    return bridge, service, payload


def questions(query="Which control supports this result?", paper_ids=None):
    return [{"claim_id": "claim-1", "paper_ids": paper_ids or ["P1"], "query": query}]


def mailbox(bridge, section="S1", requests=None):
    requests = requests or questions()
    rid = bridge._request_id(section, requests)
    (bridge.directory / f"{rid}.request.json").write_text(json.dumps({
        "request_id": rid, "section_id": section, "requests": requests}), encoding="utf-8")
    bridge.process_mailbox()
    return json.loads((bridge.directory / f"{rid}.response.json").read_text(encoding="utf-8"))


def test_owned_local_parent_and_si_lookup_is_cached_and_original_payload_unchanged(prepared):
    bridge, service, payload = prepared
    before = deepcopy(payload)
    response = mailbox(bridge)
    assert response["status"] == "ok"
    assert response["validation_nonce"] == bridge.validation_nonce
    assert {row["source_file_id"] for row in response["evidence"]} == {"P1", "SI1"}
    assert {row["paper_id"] for row in response["evidence"]} == {"P1"}
    assert all(row["claim_eligible"] and row["evidence_key"] == evidence_key(
        row["source_file_id"], row["chunk_id"], row["source_lineage_hash"]) for row in response["evidence"])
    mailbox(bridge, "S2")
    assert len(service.library_index.calls) == 1
    published = bridge.publication_payload({})
    assert len(published["evidence_package"]["evidence_registry"]) == 2
    assert all(len(section["hits"]) == 2 for section in published["evidence_package"]["sections"])
    assert payload == before


def test_missing_question_uses_shared_lexical_terms_not_postgres_sentence_and(prepared):
    bridge, service, _ = prepared
    mailbox(bridge, requests=questions("The replication cohort retained feasibility under changed conditions"))
    query, arguments = service.library_index.calls[0]
    assert '"replication" OR "cohort"' in query
    assert len(arguments["term_groups"]) == 1
    assert "replication" in arguments["term_groups"][0]
    assert arguments["allowed_papers"] == ["P1", "SI1"]


def test_round_budget_survives_worker_restart(prepared):
    bridge, service, payload = prepared
    assert mailbox(bridge)["status"] == "ok"
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    assert mailbox(resumed, requests=questions("Another question"))["status"] == "unavailable"
    assert len(service.library_index.calls) == 1


@pytest.mark.parametrize("tamper", [False, True])
def test_saved_response_gets_current_worker_nonce_only_after_source_revalidation(prepared, tamper):
    bridge, service, payload = prepared
    response = mailbox(bridge)
    output = bridge.directory / f"{response['request_id']}.response.json"
    if tamper:
        response["evidence"][0]["content"] = "Forged cached passage"
        output.write_text(json.dumps(response), encoding="utf-8")
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    assert resumed.validation_nonce != response["validation_nonce"]
    assert json.loads(output.read_text(encoding="utf-8"))["validation_nonce"] != resumed.validation_nonce
    resumed.process_mailbox()
    validated = json.loads(output.read_text(encoding="utf-8"))
    assert validated["validation_nonce"] == resumed.validation_nonce
    assert validated["status"] == ("unavailable" if tamper else "ok")
    assert len(service.library_index.calls) == 1


def test_request_id_must_match_contents_not_only_filename(prepared):
    bridge, service, _ = prepared
    original = questions()
    rid = bridge._request_id("S1", original)
    (bridge.directory / f"{rid}.request.json").write_text(json.dumps({
        "request_id": rid, "section_id": "S1", "requests": questions("Changed query")}), encoding="utf-8")
    bridge.process_mailbox()
    response = json.loads((bridge.directory / f"{rid}.response.json").read_text(encoding="utf-8"))
    assert response["status"] == "unavailable" and not response["evidence"]
    assert not service.library_index.calls


def test_cli_does_not_consume_an_old_response_before_current_worker_rechecks_it(prepared, monkeypatch):
    bridge, service, payload = prepared
    old = mailbox(bridge)
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    monkeypatch.setenv("REVIEW_SECTION_EVIDENCE_MAILBOX_NONCE", resumed.validation_nonce)
    client = evidence_mailbox(bridge.directory, "S1", timeout=0.01)
    with pytest.raises(RuntimeError, match="saved draft"):
        client(questions())
    resumed.process_mailbox()
    response = client(questions())
    assert response["request_id"] == old["request_id"]
    assert response["validation_nonce"] == resumed.validation_nonce
    assert len(service.library_index.calls) == 1


def test_neighbor_context_is_returned_without_becoming_claim_eligible(prepared):
    bridge, service, payload = prepared
    service.library_index.hits.append(hit("P1", "boundary", neighbor=True))
    response = mailbox(bridge)
    context = next(row for row in response["evidence"] if row["chunk_id"] == "boundary")
    assert context["is_neighbor"] is True
    assert context["claim_eligible"] is context["counts_as_evidence"] is False
    assert context["support_level"] == context["assertion_ceiling"] == "context_only"
    record = {**response, "requests": questions()}
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    published = resumed.publication_payload({"authoring_states": {"S1": {"state": {"evidence_supplement": record}}}})
    restored = next(row for row in published["evidence_package"]["sections"][0]["hits"] if row["chunk_id"] == "boundary")
    assert restored["claim_eligible"] is restored["counts_as_evidence"] is False


@pytest.mark.parametrize("requests", [questions(paper_ids=["foreign"]), questions(paper_ids=["SI1"]),
                                    questions() * 4, questions("x" * 1201)])
def test_requests_cannot_expand_scope_or_budget(prepared, requests):
    bridge, service, _ = prepared
    response = mailbox(bridge, requests=requests)
    assert response["status"] == "unavailable" and not response["evidence"]
    assert service.library_index.calls == []
    assert "not_reported" not in json.dumps(response)


def test_source_version_change_stops_retrieval(prepared):
    bridge, service, _ = prepared
    service.library_index.lineages["SI1"] = "changed"
    assert mailbox(bridge)["status"] == "unavailable"
    assert not service.library_index.calls


@pytest.mark.parametrize("change", ["content", "lineage", "parent", "key", "out_of_request"])
def test_checkpoint_sources_are_revalidated_before_publication(prepared, change):
    bridge, service, payload = prepared
    response = mailbox(bridge)
    record = {**response, "requests": questions()}
    row = record["evidence"][0]
    if change == "content":
        row["content"] = "Invented result"
    elif change == "lineage":
        service.library_index.lineages["P1"] = "changed"
    elif change == "parent":
        row["paper_id"] = "P2"
    elif change == "key":
        row["evidence_key"] = "forged"
    else:
        wrong = hit("P2")
        record["evidence"] = [{**asdict(wrong), "source_file_id": "P2",
                               "evidence_key": evidence_key("P2", wrong.chunk_id, wrong.source_lineage_hash)}]
    checkpoint = {"authoring_states": {"S1": {"state": {"evidence_supplement": record}}}}
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    with pytest.raises(WorkflowValidationError):
        resumed.publication_payload(checkpoint)


def test_checkpoint_validation_restores_new_sources_without_a_second_lookup(prepared):
    bridge, service, payload = prepared
    response = mailbox(bridge)
    record = {**response, "requests": questions()}
    checkpoint = {"authoring_states": {"S1": {"state": {"evidence_supplement": record}}},
                  "entries": {"S1": {"synthesis": {"source_review": {"evidence_supplement": record}}}}}
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    published = resumed.publication_payload(checkpoint)
    assert len(published["evidence_package"]["sections"][0]["hits"]) == 2
    assert len(service.library_index.calls) == 1


def test_completed_limited_section_keeps_base_identity_after_validated_publication_merge(prepared):
    bridge, service, payload = prepared
    base = payload["evidence_package"]["sections"][0]
    output = {"generation_mode": "limited_evidence",
              "evidence_resolution": resolution_record(base, pending=False, reason="Some questions remain")}
    response = mailbox(bridge)
    record = {**response, "requests": questions()}
    checkpoint = {"entries": {"S1": {"output": output,
                  "synthesis": {"source_review": {"evidence_supplement": record}}}}}
    resumed = SectionEvidenceSupplement(service, bridge.principal, payload, bridge.directory)
    package = resumed.publication_payload(checkpoint)["evidence_package"]["sections"][0]
    assert len(package["hits"]) == 2
    assert has_evidence_resolution(output, package)
    assert not base["hits"]
    assert len(service.library_index.calls) == 1


def test_only_new_chunks_and_at_most_nine_hits_are_returned(prepared):
    bridge, service, payload = prepared
    old = hit("P1")
    payload["evidence_package"]["sections"][0]["hits"] = [{
        "evidence_key": evidence_key(old.paper_id, old.chunk_id, old.source_lineage_hash)}]
    service.library_index.hits += [hit("P1", f"extra-{index}") for index in range(15)]
    response = mailbox(bridge)
    assert response["status"] == "ok" and len(response["evidence"]) == 9
    assert old.content not in {row["content"] for row in response["evidence"]}


def test_retrieval_failure_is_unavailable_not_an_absence_claim(prepared):
    bridge, service, _ = prepared
    def unavailable(*args, **kwargs):
        raise RuntimeError("Index busy")
    service.library_index.retrieve = unavailable
    response = mailbox(bridge)
    assert response["status"] == "unavailable"
    assert response["evidence"] == []
    assert "not_reported" not in json.dumps(response)


def test_legacy_or_unpinned_tasks_do_not_open_supplement_channel(prepared, tmp_path):
    bridge, service, payload = prepared
    for task in payload["tasks"]:
        task.pop("reading_strategy")
    disabled = SectionEvidenceSupplement(service, bridge.principal, payload, tmp_path / "unused")
    assert not disabled.enabled and not disabled.directory.exists()
