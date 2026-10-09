"""Offline new-reading → actual-use audit → bounded local source revision."""
from copy import deepcopy
import json

import pytest

from review_writer_core.model_gateway_client import DeferredModelCall
from review_writer_core.review_fact_readiness import REVIEW_READING_STRATEGY
from review_writer_core.stages.sections import source_writing as writer
from review_writer_core.stages.sections.source_units import UNIT_FORMAT
from review_writer_core.stages.sections.source_supplement import prepare_supplement, evidence_mailbox


def source(key="base", text="The intervention was feasible in this pilot cohort.", paper="P1"):
    return {"evidence_key": key, "paper_id": paper, "chunk_id": key, "content": text,
            "source_lineage_hash": "v1", "claim_eligible": True, "assertion_ceiling": "direct_source_report"}


NEW = source("replication", "Feasibility was demonstrated in a replication cohort with sustained response.")
CLAIMS = ["Feasibility extended to a replication cohort.", "The pilot findings establish a starting point for evaluation."]


def arguments(**updates):
    return {"section_id": "S1", "task": {"section_id": "S1", "allowed_papers": ["P1"],
            "primary_papers": ["P1"], "reading_strategy": REVIEW_READING_STRATEGY},
            "context": "", "evidence": [source()], "citation_format": UNIT_FORMAT,
            "audit_mode": "selective", **updates}


def proposed(prompt):
    data = json.loads(prompt[prompt.index('{"task":'):])
    uid = data["sources"][0]["source_units"][0]["unit_id"]
    return {"paragraphs": [{"reader_takeaway": text, "role": "anchor_case", "text": text,
        "claims": [{"text": text, "claim_kind": "reported_finding", "fact_ids": [], "review_reasons": [],
                    "support_spans": [{"unit_id": uid}], "result_context": []}]} for text in CLAIMS]}


def verdict(cid, status="supported", **kwargs):
    return {"claim_id": cid, "status": status, "text": "", "reason": "", "conflict_quote": "", **kwargs}


def test_new_reading_cannot_skip_actual_use_audit_with_empty_model_reasons():
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return proposed(prompt)
        data = json.loads(prompt[prompt.index('{"claims":'):])
        assert data["review_scope"] == "whole_section"
        assert len(data["claims"]) == 2
        return {"claims": [verdict(c["claim_id"]) for c in data["claims"]]}
    writing, _, report = writer.write_from_sources(**arguments(call=model))
    assert calls == ["section-source-writing", "section-used-claim-check"]
    assert report["audit_mode"] == "full"
    assert report["program_checked_claim_count"] == 0
    assert all(c["source_verification"]["status"] == "supported" for c in writing["claims"])


@pytest.mark.parametrize("interruption", [None, "deferred", "provider"])
def test_supplement_repairs_only_missing_claim_and_reuses_immutable_requests(interruption):
    saved, calls, lookups, repair_calls = {}, [], [], []
    def save(value):
        saved.clear()
        saved.update(deepcopy(value))
    def fetch(requests):
        lookups.append(deepcopy(requests))
        assert len(requests) == 1 and requests[0]["claim_id"] == "S1-p1-C01"
        assert requests[0]["paper_ids"] == ["P1"]
        return {"status": "ok", "evidence": [NEW]}
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return proposed(prompt)
        if label == "section-used-claim-check":
            return {"claims": [verdict("S1-p1-C01", "unresolved", reason="Need the replication results."),
                               verdict("S1-p2-C01")]}
        assert label == "section-source-content-repair"
        repair_calls.append((prompt, deepcopy(schema)))
        if interruption and len(repair_calls) == 1:
            if interruption == "deferred":
                raise DeferredModelCall("paid-repair")
            raise RuntimeError("Provider unavailable")
        data = json.loads(prompt[prompt.index('{"claims":'):])
        assert [c["claim_id"] for c in data["claims"]] == ["S1-p1-C01"]
        new = next(row for row in data["sources"] if row["evidence_key"] == "replication")
        return {"claims": [verdict("S1-p1-C01", support_spans=[{"unit_id": new["source_units"][0]["unit_id"]}])]}
    inputs = arguments(call=model, retrieve_evidence=fetch, save_state=save)
    original = deepcopy((inputs["task"], inputs["evidence"]))
    if interruption:
        with pytest.raises(DeferredModelCall if interruption == "deferred" else RuntimeError):
            writer.write_from_sources(**inputs)
        snapshot = deepcopy(saved["request_snapshot"])
        writing, generated, report = writer.write_from_sources(**inputs, resume_state=deepcopy(saved))
        assert saved["request_snapshot"] == snapshot
        assert repair_calls[0] == repair_calls[1]
    else:
        writing, generated, report = writer.write_from_sources(**inputs)
    before = deepcopy(saved)
    again = writer.write_from_sources(**inputs, resume_state=deepcopy(saved))
    assert again == (writing, generated, report)
    assert saved["request_snapshot"] == before["request_snapshot"]
    assert (inputs["task"], inputs["evidence"]) == original
    assert len(lookups) == 1
    assert calls.count("section-source-writing") == calls.count("section-used-claim-check") == 1
    assert len(repair_calls) == (2 if interruption else 1)
    assert writing["claims"][1]["claim"] == CLAIMS[1]
    assert writing["claims"][1]["evidence_refs"][0]["evidence_key"] == "base"
    assert writing["claims"][0]["evidence_refs"][0]["evidence_key"] == "replication"
    registry = {r["evidence_key"]: r for r in [source(), NEW]}
    assert all(writer.valid_source_claim(c, registry) for c in writing["claims"])
    assert report["evidence_supplement"]["evidence"] == [NEW]


def test_supplement_is_bounded_no_foreign_sources_no_overwrite_and_no_truth_promotion():
    state, requests = {}, []
    targets = [{"claim_id": str(i), "claim": f"Question {i}", "evidence_refs": [{"paper_id": "P1"}]} for i in range(10)]
    def fetch(items):
        requests.extend(items)
        return {"status": "ok", "evidence": [source("base", "malicious overwrite"),
            source("foreign", paper="P2"), {**NEW, "fact_bindings": [{"fact_id": "candidate"}]}]}
    kwargs = dict(task=arguments()["task"], targets=targets, verdicts={}, registry={"base": source()},
                  state=state, persist=lambda: None, fetch=fetch)
    assert prepare_supplement(**kwargs) == [NEW]
    assert prepare_supplement(**kwargs) == [NEW]
    assert len(requests) == 3
    assert "fact_bindings" not in state["evidence_supplement"]["evidence"][0]


@pytest.mark.parametrize("invalid_spans", [
    [{"unit_id": "nonexistent"}], [],
    [{"evidence_key": "replication", "quote": NEW["content"]}],
])
def test_invalid_local_repair_source_mapping_remains_protocol_failure(invalid_spans):
    calls, lookups = [], []

    def fetch(requests):
        lookups.append(deepcopy(requests))
        return {"status": "ok", "evidence": [NEW]}

    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return proposed(prompt)
        if label == "section-used-claim-check":
            return {"claims": [verdict("S1-p1-C01", "unresolved", reason="Need replication source."),
                               verdict("S1-p2-C01")]}
        assert label == "section-source-content-repair"
        # A status alone is not a scientific verdict: this request requires
        # a valid source-unit selection, including for supported originals.
        return {"claims": [verdict("S1-p1-C01", support_spans=deepcopy(invalid_spans))]}

    writing, _, report = writer.write_from_sources(**arguments(call=model, retrieve_evidence=fetch))
    assert [c["claim_id"] for c in writing["claims"]] == ["S1-p2-C01"]
    assert writing["claims"][0]["claim"] == CLAIMS[1]
    assert [row["claim_id"] for row in report["unresolved"]] == ["S1-p1-C01"]
    assert not any(row.get("disposition") == "withheld" for row in report["omitted"])
    assert report["section_review"]["status"] == "not_reviewed"
    assert len(lookups) == 1
    assert calls == ["section-source-writing", "section-used-claim-check", "section-source-content-repair"]


def test_contradiction_does_not_trigger_confirmation_hunting_or_old_task_lookup():
    kwargs = dict(task=arguments()["task"], targets=[{"claim_id": "c"}], verdicts={"c": {"status": "unsupported"}},
        registry={}, state={}, persist=lambda: None, fetch=lambda *_: pytest.fail("must not retrieve"))
    assert prepare_supplement(**kwargs) == []
    assert prepare_supplement(**{**kwargs, "task": {}, "verdicts": {}}) == []


def test_local_mailbox_is_optional_and_reuses_same_query_identity(tmp_path):
    assert evidence_mailbox(None, "S1") is None
    callback = evidence_mailbox(tmp_path, "S1", timeout=0)
    with pytest.raises(RuntimeError, match="saved draft"):
        callback([{"query": "scope", "paper_ids": ["P1"]}])
    first = next(tmp_path.glob("*.request.json"))
    with pytest.raises(RuntimeError):
        callback([{"query": "scope", "paper_ids": ["P1"]}])
    assert list(tmp_path.glob("*.request.json")) == [first]
