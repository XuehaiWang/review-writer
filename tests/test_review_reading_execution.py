"""New reading lifecycle and legacy recovery, without provider requests."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from review_writer_core.review_fact_readiness import (
    REVIEW_READING_STRATEGY, fact_processing_complete, fact_processing_state, paper_reading_current,
)
from review_writer_core.scientific_facts import (
    FACT_VALIDATION_VERSION, fact_is_usable, fact_needs_verification, merge_facts, review_fingerprint,
)
from review_writer_core.stages.planning.matrix import refresh_matrix_fact_summary
from review_writer_core.stages.planning.fact_revision import revise_fact
from review_writer_core.model_gateway_client import DeferredModelCall
from review_writer_core.fact_model_requests import REVIEW_SOURCE_MARKER, fact_request_descriptor, review_prompt_sources

SPEC = importlib.util.spec_from_file_location("review_reading_execution", Path(__file__).resolve().parents[1]
    / "skills/review-literature-matrix-outline/scripts/enrich_matrix_facts.py")
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


def reading_paper():
    return {"paper_id": "P1", "title": "A bounded experiment", "source_fingerprint": "request-1",
            "reading_strategy": REVIEW_READING_STRATEGY, "reading_source_key": "sources-1",
            "required_fact_roles": ["object_input"], "evidence_candidates": [{
                "evidence_key": "source-1", "paper_id": "P1", "chunk_id": "results",
                "source_lineage_hash": "lineage-1", "content_type": "text",
                "question_ids": ["object_input"], "content": "The optimized protocol gave 91% yield."}]}


def response():
    return {"facts": [{"field_id": "quantitative_results", "value": "The protocol afforded 91% yield.",
                       "study_ownership": "own_results", "confidence": 0.99,
                       "evidence_key": "source-1", "support_excerpt": "The optimized protocol gave 91% yield.",
                       "verification": {"status": "supported", "contract": FACT_VALIDATION_VERSION}}],
            "paper_analysis": {"research_question": "Which yield can be achieved?",
                               "contribution": "A source-bounded experimental result.",
                               "evidence_keys": ["source-1"], "open_questions": ["Are other substrates supported?"]},
            "evidence_requests": [{"field_id": "scope", "query": "Which other substrates were tested?",
                                   "evidence_keys": ["source-1"]}]}


def checked_response(prompt):
    head = prompt.split("\nSource context:\n")[0]
    facts = json.loads(head[head.rindex("\n[") + 1:])
    return {"verdicts": [{"fact_id": fact["fact_id"], "status": "supported",
                          "reason": "This experiment reports the stated yield and belongs to this study."}
                         for fact in facts]}


def run(item, *, previous=None, source=None, retrieve=None):
    snapshots = []
    result = PIPELINE.extract_paper(source or {}, item, previous,
        publish=lambda _pid, entry, phase, _details: snapshots.append((deepcopy(entry), phase)),
        retrieve=retrieve or (lambda _: pytest.fail("Ordinary reading must not launch optional supplements")))
    return result, snapshots


def test_first_reading_is_one_call_and_cannot_self_approve(monkeypatch):
    calls = []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda prompt, **kw: (calls.append(kw["label"]), response())[1])
    result, snapshots = run(reading_paper(), source={"routing_axis_id": "route",
        "routing_categories": [{"label": "A"}, {"label": "B"}]})
    assert calls == ["matrix-facts-P1"]
    fact = result["facts"][0]
    assert fact["verification"] == {"status": "pending"}
    assert fact["validation_contract"] == FACT_VALIDATION_VERSION
    assert fact_needs_verification(fact) and not fact_is_usable(fact)
    assert result["status"] == "complete"
    assert result["processing"]["reading"] == "completed"
    assert result["processing"]["verification"] == "pending"
    assert paper_reading_current(result["facts"], result, source_key="sources-1")
    assert not paper_reading_current(result["facts"], result, source_key="sources-2")
    assert not fact_processing_complete(result["facts"], result)
    assert result["paper_analysis"]["open_questions"] == ["Are other substrates supported?"]
    assert result["paper_analysis"]["reading_scope"]["selected_count"] == 1
    assert snapshots[-1][1] == "completed"
    damaged = deepcopy(fact)
    damaged.pop("verification")
    damaged.pop("validation_contract")
    assert not fact_is_usable(damaged), "New candidates must not enter legacy no-contract compatibility"


@pytest.mark.parametrize("existing", [False, True])
def test_source_bound_guide_without_facts_completes_and_resumes_without_model(monkeypatch, existing):
    generated = response()
    generated["facts"] = []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda *args, **kwargs: deepcopy(generated))
    item = reading_paper()
    result, snapshots = run(item)
    assert result["status"] == "complete" and result["error"] == ""
    assert result["processing"]["extraction"] == "completed"
    assert result["facts"] == []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda *a, **k: pytest.fail("Completed source-bound reading repeated"))
    if existing:
        item["existing_fact_result"] = result
    resumed, _ = run(item, previous=None if existing else snapshots[-1][0])
    assert paper_reading_current([], resumed, source_key="sources-1")
    matrix = {"rows": [{"scientific_facts": [], "fact_enrichment": resumed}]}
    assert refresh_matrix_fact_summary(matrix)["reading_completed_count"] == 1
    assert matrix["rows"][0]["fact_enrichment"]["review_readiness"] != "complete"


@pytest.mark.parametrize("generated", [{"facts": []}, {"facts": [], "paper_analysis": {
    "contribution": "Invented source", "evidence_keys": ["not-registered"]}}])
def test_no_source_bound_reading_is_a_failure(monkeypatch, generated):
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda *a, **k: deepcopy(generated))
    result, _ = run(reading_paper())
    assert result["status"] == "failed"
    assert result["fact_extraction_profile"]["reading_status"] == "failed"
    assert not paper_reading_current([], result, source_key="sources-1")


def test_missing_sources_do_not_call_model_or_claim_reading_success(monkeypatch):
    item = reading_paper()
    item["evidence_candidates"] = []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda *a, **k: pytest.fail("No readable sources"))
    result, _ = run(item)
    assert result["processing"]["reading"] == "failed"


def supported_fact(item):
    fact = PIPELINE.normalize_result(PIPELINE.extraction_validation_paper(item), response())["facts"][0]
    fact["support_level"] = "direct"
    fact["assertion_ceiling"] = "direct_source_report"
    fact["verification"] = {"contract": FACT_VALIDATION_VERSION, "status": "supported",
                            "input_fingerprint": review_fingerprint(fact)}
    return fact


def test_reading_retains_identical_audits_without_paid_verification(monkeypatch):
    item = reading_paper()
    cached = supported_fact(item)
    item["reused_fact_cache"] = {"prompt_version": "older-prompt", "facts": [cached]}
    calls = []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda prompt, **kw: (calls.append(kw["label"]), response())[1])
    result, _ = run(item)
    assert calls == ["matrix-facts-P1"]
    assert fact_is_usable(result["facts"][0])
    assert result["facts"][0]["verification"] == cached["verification"]


def test_ownership_change_does_not_restore_audit_by_fact_id():
    item = reading_paper()
    old = supported_fact(item)
    changed = deepcopy(old)
    changed["study_ownership"] = "prior_work"
    changed["verification"] = {"status": "pending"}
    item["reused_fact_cache"] = {"facts": [old]}
    result = {"facts": [changed]}
    assert PIPELINE.restore_cached_verifications(item, result) == 0
    assert not fact_is_usable(changed)


def test_automatic_candidate_does_not_overwrite_manual_revision():
    old = supported_fact(reading_paper())
    old["human_checked"] = True
    fresh = deepcopy(old)
    fresh["human_checked"] = False
    fresh["verification"] = {"status": "pending"}
    fresh["study_ownership"] = "unknown"
    assert merge_facts([old], [fresh]) == [old]


def test_provider_failure_is_not_cached_as_completed_reading(monkeypatch):
    item = reading_paper()
    item["reused_fact_cache"] = {"prompt_version": "old", "facts": [supported_fact(item)]}
    def fail(*a, **k):
        raise RuntimeError("Provider unavailable")
    monkeypatch.setattr(PIPELINE, "call_json_model", fail)
    result, snapshots = run(item)
    assert result["facts"] and fact_is_usable(result["facts"][0])
    assert result["processing"]["reading"] == "failed"
    assert not paper_reading_current(result["facts"], result, source_key="sources-1")
    calls = []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda prompt, **kw: (calls.append(kw["label"]), response())[1])
    retried, _ = run(item, previous=snapshots[-1][0])
    assert calls == ["matrix-facts-P1"]
    assert paper_reading_current(retried["facts"], retried, source_key="sources-1")


@pytest.mark.parametrize("operation", ["fact_revision", "verify_only", "targeted"])
def test_explicit_audits_keep_strict_verification(monkeypatch, operation):
    item = reading_paper()
    fact = PIPELINE.normalize_result(PIPELINE.extraction_validation_paper(item), response())["facts"][0]
    source = {"operation": operation}
    if operation == "fact_revision":
        item["revision_facts"] = [fact]
    elif operation == "verify_only":
        source["verify_only"] = True
        item["existing_fact_result"] = {"facts": [fact]}
    else:
        source["targeted_evidence_requests"] = {"P1": [{"field_id": "scope", "query": "Find the substrate scope"}]}
        item["reused_fact_cache"] = {"facts": [fact]}
    calls = []
    def model(prompt, **kw):
        calls.append(kw["label"])
        return checked_response(prompt)
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result, _ = run(item, source=source, retrieve=lambda _: [])
    assert calls == ["fact-verify-P1"]
    assert fact_is_usable(result["facts"][0])
    assert "reading" not in fact_processing_state(result["facts"], result)["processing"]


def test_unmarked_legacy_request_keeps_its_original_prompt_and_audit(monkeypatch):
    item = reading_paper()
    item.pop("reading_strategy")
    item.pop("reading_source_key")
    calls = []
    def model(prompt, **kw):
        calls.append(kw["label"])
        if kw["required_list"] == "verdicts":
            return checked_response(prompt)
        assert "Strategy: review-oriented/1" not in prompt
        generated = response()
        generated["evidence_requests"] = []
        return generated
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result, _ = run(item)
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]
    assert "reading" not in result["processing"]
    assert paper_reading_current(result["facts"], result) == fact_processing_complete(result["facts"], result)


def test_mixed_reading_and_manual_revision_use_only_their_respective_model_call(monkeypatch):
    ordinary = reading_paper()
    revision = {**reading_paper(), "paper_id": "P2", "reading_strategy": "", "verify_only": True}
    original = supported_fact(revision)
    revised = revise_fact(original, {"value": "The optimized experiment afforded a yield of 91%."})
    revision["revision_facts"] = [revised]
    calls, snapshots = [], []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["label"] == "matrix-facts-P1":
            assert "Strategy: review-oriented/1" in prompt
            assert "topic_classification_assignments" not in prompt
            assert "routing_recommendation" not in prompt
            return response()
        assert kwargs["label"] == "fact-verify-P2"
        return checked_response(prompt)

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    source = {"reading_strategy": REVIEW_READING_STRATEGY, "source_matrix_artifact_id": "matrix",
              "papers": [ordinary, revision], "fact_agent_limits": {"paper_concurrency": 2}}
    results = PIPELINE.enrich_papers(source, {}, save_checkpoint=lambda snap: snapshots.append(deepcopy(snap)),
        save_progress=lambda _: None, retrieve=lambda _: pytest.fail("No optional supplements in this batch"))
    assert sorted(calls) == ["fact-verify-P2", "matrix-facts-P1"]
    assert results[0]["processing"]["reading"] == "completed"
    assert results[0]["facts"][0]["verification"]["status"] == "pending"
    assert results[1]["facts"][0]["value"] == revised["value"]
    assert fact_is_usable(results[1]["facts"][0])
    assert "reading" not in results[1]["processing"]
    assert snapshots[-1]["entries"]["P1"]["reading_strategy"] == REVIEW_READING_STRATEGY
    assert "reading_strategy" not in snapshots[-1]["entries"]["P2"]


def test_deferred_reading_resumes_its_exact_strategy_without_spending_an_audit(monkeypatch):
    item = reading_paper()
    snapshots, calls = [], []
    ready = False
    def model(prompt, **kw):
        calls.append((prompt, kw))
        if not ready:
            raise DeferredModelCall("reading-child")
        return response()
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    source = {"source_matrix_artifact_id": "matrix", "papers": [item], "attempt_id": "same-job"}
    def execute(checkpoint):
        return PIPELINE.enrich_papers(source, checkpoint,
            save_checkpoint=lambda snapshot: snapshots.append(deepcopy(snapshot)),
            save_progress=lambda _: None, retrieve=lambda _: pytest.fail("Unexpected supplement"), max_new_papers=1)
    with pytest.raises(DeferredModelCall):
        execute({})
    entry = snapshots[-1]["entries"]["P1"]
    assert entry["reading_strategy"] == REVIEW_READING_STRATEGY
    assert entry["reading_source_key"] == "sources-1"
    assert entry["agent_state"]["model_calls"] == 0
    ready = True
    completed = execute(snapshots[-1])[0]
    assert calls[0] == calls[1], "Do not change the paid request snapshot while waiting"
    assert completed["fact_extraction_profile"]["model_calls"] == 1


def _reordered_prompt(item):
    prompt = PIPELINE.review_reading_prompt("topic", item)
    head, windows = prompt.split(REVIEW_SOURCE_MARKER)
    return head + REVIEW_SOURCE_MARKER + json.dumps(list(reversed(json.loads(windows))), ensure_ascii=False) + "\n"


def _multi_source_paper():
    item = reading_paper()
    item["evidence_candidates"] += [
        {**deepcopy(item["evidence_candidates"][0]), "evidence_key": f"window-{i}",
         "content": f"A distinct source window {i}."} for i in range(8)]
    return item


def test_legacy_paid_reading_with_reordered_windows_recovers_original_key(monkeypatch):
    item = _multi_source_paper()
    old_prompt = _reordered_prompt(item)
    current_prompt = PIPELINE.review_reading_prompt("topic", item)
    assert old_prompt != current_prompt
    descriptor = fact_request_descriptor(old_prompt, label="matrix-facts-P1",
                                         request_key="legacy-paid-key", parent_job_id="same-job")
    previous = {"source_fingerprint": item["source_fingerprint"], "reading_strategy": item["reading_strategy"],
                "reading_source_key": item["reading_source_key"], "result": {}, "agent_state": {
                    "attempt_id": "same-job", "pending_model_request": descriptor,
                    "waiting_model_job_ids": ["original-child"]}}
    calls = []
    def model(prompt, **kwargs):
        calls.append((prompt, kwargs))
        assert kwargs["resume_request_key"] == "legacy-paid-key"
        return response()
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result, snapshots = run(item, previous=previous, source={"review_topic": "topic", "attempt_id": "same-job"})
    assert len(calls) == 1 and result["processing"]["reading"] == "completed"
    assert "pending_model_request" not in snapshots[-1][0]["agent_state"]
    assert len(json.loads(calls[0][0].split(REVIEW_SOURCE_MARKER)[1])) == 9


@pytest.mark.parametrize("change", ["content", "topic", "role", "paper", "source", "reading_source", "parent"])
def test_changed_scientific_input_or_scope_does_not_consume_old_call(monkeypatch, change):
    item = _multi_source_paper()
    old_prompt = _reordered_prompt(item)
    previous = {"source_fingerprint": item["source_fingerprint"], "reading_strategy": item["reading_strategy"],
                "reading_source_key": item["reading_source_key"], "result": {}, "agent_state": {
                    "attempt_id": "same-job", "pending_model_request": fact_request_descriptor(
                        old_prompt, label="matrix-facts-P1", request_key="legacy-paid-key", parent_job_id="same-job")}}
    source = {"review_topic": "topic", "attempt_id": "same-job"}
    if change == "content":
        item["evidence_candidates"][-1]["content"] += " Contradictory observations were recorded."
    elif change == "topic":
        source["review_topic"] = "changed focus"
    elif change == "role":
        item["required_fact_roles"] = ["scope"]
    elif change == "paper":
        item["paper_id"] = "another-paper"
    elif change == "source":
        item["source_fingerprint"] = "new-source"
    elif change == "reading_source":
        item["reading_source_key"] = "new-reading-source"
    else:
        source["attempt_id"] = "new-job"
    calls = []
    def model(prompt, **kwargs):
        assert "resume_request_key" not in kwargs
        calls.append(prompt)
        return response()
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    run(item, previous=previous, source=source)
    assert len(calls) == 1


def test_pending_identity_is_saved_before_submission_and_reused_after_reorder(monkeypatch):
    monkeypatch.setenv("REVIEW_WRITER_DELEGATE_MODEL_CALLS", "1")
    item = _multi_source_paper()
    snapshots, calls = [], []
    ready = False
    def model(prompt, **kwargs):
        calls.append((prompt, kwargs))
        saved = snapshots[-1]["agent_state"]["pending_model_request"]
        assert saved["stage"] == kwargs["label"]
        if not ready:
            raise DeferredModelCall("child", request_key=saved["request_key"])
        assert kwargs["resume_request_key"] == saved["request_key"]
        return response()
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    def execute(previous):
        return PIPELINE.extract_paper({"attempt_id": "same-job"}, item, previous,
            publish=lambda _pid, entry, *_: snapshots.append(deepcopy(entry)), retrieve=lambda _: [])
    with pytest.raises(DeferredModelCall):
        execute(None)
    previous = snapshots[-1]
    assert previous["agent_state"]["model_calls"] == 0
    item["evidence_candidates"].reverse()
    ready = True
    result = execute(previous)
    assert result["fact_extraction_profile"]["model_calls"] == 1
    assert calls[0][0] == calls[1][0]


def test_custom_field_order_is_stable_without_changing_explicit_topic_priority():
    item = _multi_source_paper()
    item["evidence_candidates"][0]["question_ids"] = ["custom_a", "custom_b", "object_input"]
    item["evidence_candidates"][1]["question_ids"] = ["custom_b", "custom_a"]
    item["required_fact_roles"] = ["scope", "object_input"]
    prompt = PIPELINE.review_reading_prompt("topic", item)
    item["evidence_candidates"].reverse()
    for candidate in item["evidence_candidates"]:
        candidate["question_ids"].reverse()
    assert PIPELINE.review_reading_prompt("topic", item) == prompt
    assert 'Priority fields: ["scope", "object_input"]' in prompt
    item["required_fact_roles"].reverse()
    assert PIPELINE.review_reading_prompt("topic", item) != prompt


def test_old_budgeted_reading_keeps_its_original_windows_and_validation_context(monkeypatch):
    item = _multi_source_paper()
    item["evidence_candidates"] += [
        {**deepcopy(item["evidence_candidates"][0]), "evidence_key": f"window-{index}",
         "content": f"Original experiment {index} gave a distinct result."} for index in range(8, 32)]
    head, selected = review_prompt_sources(PIPELINE.review_reading_prompt("topic", item))
    assert len(selected) == 24
    excluded = next(row for row in item["evidence_candidates"]
                    if row["evidence_key"] not in {window["evidence_key"] for window in selected})
    old_windows = [*selected[:-1], {key: excluded[key] for key in selected[0]}]
    old_prompt = head + REVIEW_SOURCE_MARKER + json.dumps(old_windows) + "\n"
    item["pending_reading_prompt"] = old_prompt
    previous = {"source_fingerprint": item["source_fingerprint"], "reading_strategy": item["reading_strategy"],
                "reading_source_key": item["reading_source_key"], "result": {}, "agent_state": {
                    "attempt_id": "same-job", "pending_model_request": fact_request_descriptor(
                        old_prompt, label="matrix-facts-P1", request_key="old-budget-key", parent_job_id="same-job")}}
    def model(prompt, **kwargs):
        assert kwargs["resume_request_key"] == "old-budget-key"
        assert review_prompt_sources(prompt)[1] == old_windows
        return {"facts": [{"field_id": "quantitative_results", "value": "A distinct experimental result.",
                           "study_ownership": "own_results", "confidence": 0.99,
                           "evidence_key": excluded["evidence_key"], "support_excerpt": excluded["content"]}]}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result, _ = run(item, previous=previous, source={"review_topic": "topic", "attempt_id": "same-job"})
    assert result["processing"]["reading"] == "completed"
    assert result["facts"][0]["evidence_refs"][0]["evidence_key"] == excluded["evidence_key"]
    assert result["paper_analysis"]["reading_scope"]["selected_count"] == 24
    assert "_pending_reading_context" not in item, "Caller input is not mutated"


def test_changed_or_unregistered_old_window_cannot_replace_current_evidence():
    item = _multi_source_paper()
    head, windows = review_prompt_sources(PIPELINE.review_reading_prompt("topic", item))
    for change in ("content", "key", "topic", "page"):
        changed = deepcopy(windows)
        if change == "content":
            changed[0]["content"] = "Invented source content."
        elif change == "key":
            changed[0]["evidence_key"] = "unowned-key"
        elif change == "page":
            changed[0]["page_start"] = 999
        item["pending_reading_prompt"] = head + REVIEW_SOURCE_MARKER + json.dumps(changed)
        PIPELINE.restore_pending_reading_context(item, "different topic" if change == "topic" else "topic")
        assert "_pending_reading_context" not in item


def test_crash_after_submission_can_consume_result_at_request_budget(monkeypatch):
    monkeypatch.setenv("REVIEW_WRITER_DELEGATE_MODEL_CALLS", "1")
    item, snapshots = reading_paper(), []
    ready = False
    def model(prompt, **kwargs):
        if not ready:
            raise KeyboardInterrupt("simulated worker exit after submission")
        assert kwargs["resume_request_key"] == snapshots[-1]["agent_state"]["pending_model_request"]["request_key"]
        return response()
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    def execute(previous):
        return PIPELINE.extract_paper({"attempt_id": "same-job", "fact_agent_limits": {"max_model_calls": 1}},
            item, previous, publish=lambda _pid, entry, *_: snapshots.append(deepcopy(entry)), retrieve=lambda _: [])
    with pytest.raises(KeyboardInterrupt):
        execute(None)
    previous = snapshots[-1]
    assert previous["agent_state"]["pending_model_request"]["counted"] is True
    assert previous["agent_state"]["attempt_model_calls"] == 1
    ready = True
    result = execute(previous)
    assert result["processing"]["reading"] == "completed"
    assert result["fact_extraction_profile"]["model_calls"] == 1
    assert snapshots[-1]["agent_state"]["attempt_model_calls"] == 1


def test_old_completed_checkpoint_cannot_skip_new_strategy(monkeypatch):
    item = reading_paper()
    old = {"paper_id": "P1", "facts": [], "status": "complete"}
    checkpoint = {"source_matrix_artifact_id": "matrix", "completed_papers": ["P1"], "entries": {
        "P1": {"source_fingerprint": item["source_fingerprint"], "result": old, "agent_state": {}}}}
    calls = []
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda prompt, **kw: (calls.append(kw["label"]), response())[1])
    result = PIPELINE.enrich_papers({"source_matrix_artifact_id": "matrix", "papers": [item]}, checkpoint,
        save_checkpoint=lambda _: None, save_progress=lambda _: None, retrieve=lambda _: [], max_new_papers=1)
    assert calls == ["matrix-facts-P1"]
    assert result[0]["processing"]["reading"] == "completed"


def test_only_explicit_revision_candidates_are_revision_pending():
    item = reading_paper()
    fact = PIPELINE.normalize_result(PIPELINE.extraction_validation_paper(item), response())["facts"][0]
    assert fact_processing_state([fact], {})["pending_revision_fact_ids"] == []
    fact["revision_of_fact_id"] = "old-fact"
    assert fact_processing_state([fact], {})["pending_revision_fact_ids"] == [fact["fact_id"]]
