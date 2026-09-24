"""Concurrency and request-volume regressions without paid model requests."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock

import pytest
from review_writer_core.model_gateway_client import DeferredModelCall


SCRIPT = Path(__file__).resolve().parents[1] / "skills/review-literature-matrix-outline/scripts/enrich_matrix_facts.py"
SPEC = importlib.util.spec_from_file_location("matrix_fact_execution", SCRIPT)
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


def paper(paper_id):
    return {"paper_id": paper_id, "source_fingerprint": f"source-{paper_id}",
            "required_fact_roles": ["quantitative_results"],
            "evidence_candidates": [{"evidence_key": f"key-{paper_id}", "chunk_id": "result",
                "source_lineage_hash": f"lineage-{paper_id}", "question_ids": ["quantitative_results"],
                "content_type": "text", "content": "The optimized protocol gave 91% yield."}]}


def fact_response(paper_id):
    return {"facts": [{"field_id": "quantitative_results", "value": "91% yield",
                      "evidence_key": f"key-{paper_id}", "support_excerpt": "The optimized protocol gave 91% yield.",
                      "confidence": 0.99}]}


def verify_response(prompt):
    before_context = prompt.split("\nSource context:\n")[0]
    facts = json.loads(before_context[before_context.rindex("\n[") + 1:])
    return {"verdicts": [{"fact_id": fact["fact_id"], "status": "supported",
                          "reason": "The same experiment explicitly reports this result."} for fact in facts]}


def execute(source, checkpoint=None):
    checkpoints, progress = [], []
    results = PIPELINE.enrich_papers(source, checkpoint or {},
        save_checkpoint=lambda value: checkpoints.append(deepcopy(value)),
        save_progress=lambda value: progress.append(deepcopy(value)), retrieve=lambda _: [])
    return results, checkpoints, progress


def test_limited_paper_execution_yields_without_repeating_completed_papers(monkeypatch):
    calls = []
    source = {"source_matrix_artifact_id": "matrix-1", "papers": [paper("P1"), paper("P2")]}

    def fake_extract(_source, item, _previous, *, publish, retrieve):
        paper_id = item["paper_id"]
        calls.append(paper_id)
        result = {"paper_id": paper_id, "status": "complete", "facts": []}
        publish(paper_id, {"source_fingerprint": item["source_fingerprint"],
                           "result": result, "agent_state": {}}, "completed", {})
        return result

    monkeypatch.setattr(PIPELINE, "extract_paper", fake_extract)
    saved = []
    run = lambda checkpoint: PIPELINE.enrich_papers(
        source, checkpoint, save_checkpoint=lambda value: saved.append(deepcopy(value)),
        save_progress=lambda _: None, retrieve=lambda _: [], max_new_papers=1,
    )
    first = run({})
    assert calls == ["P1"] and [row["paper_id"] for row in first] == ["P1"]
    assert saved[-1]["completed_papers"] == ["P1"]
    second = run(saved[-1])
    assert calls == ["P1", "P2"] and [row["paper_id"] for row in second] == ["P1", "P2"]
    source["papers"][0]["source_fingerprint"] = "changed"
    run(saved[-1])
    assert calls[-1] == "P1"


def test_deferred_paper_model_call_resumes_without_spending_paper_budget_twice(monkeypatch):
    item = paper("P1")
    source = {"source_matrix_artifact_id": "matrix-1", "attempt_id": "job-1", "papers": [item]}
    snapshots = []
    waiting = True
    calls = []

    def model(prompt, **kwargs):
        nonlocal waiting
        calls.append(kwargs["label"])
        if waiting:
            raise DeferredModelCall("child-1")
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        return fact_response("P1")

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    def run(checkpoint):
        return PIPELINE.enrich_papers(source, checkpoint,
            save_checkpoint=lambda value: snapshots.append(deepcopy(value)),
            save_progress=lambda _: None, retrieve=lambda _: [], max_new_papers=1)

    with pytest.raises(DeferredModelCall):
        run({})
    checkpoint = snapshots[-1]
    assert checkpoint["entries"]["P1"]["agent_state"]["model_calls"] == 0
    assert checkpoint["entries"]["P1"]["agent_state"]["attempt_model_calls"] == 0
    assert checkpoint["completed_papers"] == []

    waiting = False
    result = run(checkpoint)
    assert calls == ["matrix-facts-P1", "matrix-facts-P1", "fact-verify-P1"]
    assert result[0]["fact_extraction_profile"]["model_calls"] == 2
    assert snapshots[-1]["completed_papers"] == ["P1"]


def test_new_facts_check_ownership_once_and_reuse_audit_across_jobs(monkeypatch):
    item = paper("P1")
    item["required_fact_roles"] = ["object_input"]
    source = item["evidence_candidates"][0]
    source.update(question_ids=["object_input"], content="Aromatic substrates were examined.")
    calls = []
    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            assert '"study_ownership": "unknown"' in prompt
            return verify_response(prompt)
        return {"facts": [{"field_id": "object_input", "value": source["content"],
                          "support_excerpt": source["content"], "evidence_key": source["evidence_key"], "confidence": 0.99}]}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    results, checkpoints, _ = execute({"papers": [item], "attempt_id": "first"})
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]
    fact = results[0]["facts"][0]
    assert fact["verification"]["status"] == "supported"
    assert PIPELINE.fact_is_usable(fact, purpose="detail")
    assert results[0]["fact_extraction_profile"]["source_quote_count"] == 0
    assert results[0]["fact_extraction_profile"]["semantic_supported_count"] == 1
    execute({"papers": [item], "attempt_id": "retry"}, checkpoints[-1])
    assert len(calls) == 2
    other_stage = deepcopy(item)
    other_stage["source_fingerprint"] = "different-task-same-source"
    other_stage["reused_fact_cache"] = {"facts": results[0]["facts"]}
    execute({"papers": [other_stage], "attempt_id": "other-stage"})
    assert len(calls) == 2


@pytest.mark.parametrize("text", ["The sample contained 12 compounds.", "The method causes inhibition.",
    "The approach was better than the comparator.", "The substrates are identical.", "No effect was observed."])
def test_critical_or_inferential_quotes_keep_semantic_audit(monkeypatch, text):
    item = paper("P1")
    item["required_fact_roles"] = ["object_input"]
    item["evidence_candidates"][0].update(question_ids=["object_input"], content=text)
    calls = []
    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        return {"facts": [{"field_id": "object_input", "value": text,
            "support_excerpt": text, "evidence_key": "key-P1", "confidence": 0.99}]}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    execute({"papers": [item]})
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]


def test_extraction_ignores_unrequested_fields_without_changing_retrieval_registry(monkeypatch):
    item = paper("P1")
    item["evidence_candidates"].append({"evidence_key": "scope", "content_type": "text",
        "content": "UNREQUESTED SCOPE PASSAGE", "question_ids": ["scope"]})
    before = deepcopy(item)
    def model(prompt, **kwargs):
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        assert "UNREQUESTED SCOPE PASSAGE" not in prompt
        return {**fact_response("P1"), "facts": [*fact_response("P1")["facts"],
            {"field_id": "scope", "value": "UNREQUESTED SCOPE PASSAGE", "support_excerpt": "UNREQUESTED SCOPE PASSAGE",
             "evidence_key": "scope", "confidence": 0.99}],
             "failed_fields": ["mechanism"], "evidence_requests": [{"field_id": "mechanism", "query": "explain mechanism"}]}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute({"papers": [item]})[0][0]
    assert [fact["field_id"] for fact in result["facts"]] == ["quantitative_results"]
    assert not result["failed_fields"]
    assert item == before


def test_changed_source_cannot_reuse_a_quote_cache():
    item = paper("P1")
    result = PIPELINE.normalize_result(item, fact_response("P1"))
    fact = result["facts"][0]
    fact["verification"] = {"contract": PIPELINE.FACT_VALIDATION_VERSION, "status": "supported"}
    item["reused_fact_cache"] = {"facts": [fact]}
    assert PIPELINE.cache_covers_current_fields(item)
    item["evidence_candidates"][0]["content"] = "The source now reports a different outcome."
    assert not PIPELINE.cache_covers_current_fields(item)


def test_audit_prompt_keeps_scientific_relations_without_repeating_storage_metadata():
    fact = PIPELINE.normalize_result(paper("P1"), fact_response("P1"))["facts"][0]
    fact.update(subject="product", qualifiers={"scope": "optimized protocol"},
                correction_of_fact_id="old", correction_target={"fact_id": "old", "value": "91% yield", "support_excerpt": "91% yield"})
    compact = PIPELINE.fact_audit_payload(fact)
    assert compact["fact_id"] == fact["fact_id"] and compact["value"] == fact["value"]
    assert compact["subject"] == "product" and compact["qualifiers"] == fact["qualifiers"]
    assert compact["correction_target"]["value"] == "91% yield"
    assert compact["support_spans"][0]["evidence_key"] == "key-P1"
    assert not {"extraction", "source_span", "evidence_refs", "verification"} & compact.keys()
    assert len(json.dumps(compact)) < len(json.dumps(fact))


@pytest.mark.parametrize("change", ["paraphrase", "source_missing", "different_paper", "revision", "interpretation", "multi_span", "table", "source_damage"])
def test_plain_quote_shortcut_does_not_admit_other_fact_kinds(change):
    item = paper("P1")
    source = item["evidence_candidates"][0]
    source.update(question_ids=["object_input"], content="Aromatic substrates were examined.", paper_id="P1")
    fact = PIPELINE.normalize_result(item, {"facts": [{"field_id": "object_input", "value": source["content"],
        "support_excerpt": source["content"], "evidence_key": "key-P1", "confidence": 0.99}]})["facts"][0]
    if change == "paraphrase": fact["value"] = "Aromatic substrates were extensively examined."
    elif change == "source_missing": source["content"] = "Different passage."
    elif change == "different_paper": source["paper_id"] = "P2"
    elif change == "revision": fact["revision_of_fact_id"] = "old"
    elif change == "interpretation": fact["fact_type"] = "author_interpretation"
    elif change == "multi_span": fact["support_spans"] *= 2
    elif change == "table": source["content_type"] = "table"
    elif change == "source_damage": fact["verification"] = {"status": "unavailable", "source_damage": True}
    before = deepcopy(fact)
    assert not PIPELINE.verify_plain_source_quote(fact, {"key-P1": source})
    assert fact == before


def test_targeted_request_scopes_extraction_without_inheriting_another_questions_budget(monkeypatch):
    item = paper("P1")
    item["required_fact_roles"] = ["scope"]  # Broader Matrix topic, not this repair's question.
    item["existing_fact_result"] = {"fact_extraction_profile": {"supplement_rounds": 1}}
    source = deepcopy(item["evidence_candidates"][0])
    source.update(evidence_key="control", content="The control gave 42% yield.")
    calls, saved = [], []
    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts": return verify_response(prompt)
        assert 'Available fact fields for this review: ["quantitative_results"]' in prompt
        return {"facts": [{"field_id": "quantitative_results", "value": "42% yield", "evidence_key": "control",
            "support_excerpt": source["content"], "confidence": 0.99}]}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    task = {"targeted_evidence_requests": {"P1": [{"field_id": "quantitative_results", "query": "control yield"}]}}
    result = PIPELINE.extract_paper(task, item, None, publish=lambda *args: saved.append(deepcopy(args)), retrieve=lambda _: [source])
    assert calls == ["fact-supplement-P1", "fact-verify-P1"]
    assert result["fact_extraction_profile"]["supplement_rounds"] == 1
    assert item["required_fact_roles"] == ["scope"]


def test_papers_overlap_finish_out_of_order_and_resume_without_repeating_verification(monkeypatch):
    second_done, lock = Event(), Lock()
    in_flight, peak = 0, 0
    progress, checkpoints = [], []
    def model(prompt, **kwargs):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            if kwargs["required_list"] == "verdicts":
                return verify_response(prompt)
            paper_id = kwargs["label"].removeprefix("matrix-facts-")
            if paper_id == "P1":
                assert second_done.wait(3), "P2 must finish while P1 is still awaiting the model"
            return fact_response(paper_id)
        finally:
            with lock:
                in_flight -= 1
    def publish(value):
        progress.append(deepcopy(value))
        if "P2" in value["completed_papers"]:
            second_done.set()
    source = {"papers": [paper("P1"), paper("P2"), paper("P3")], "attempt_id": "first",
              "fact_agent_limits": {"paper_concurrency": 2}}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    results = PIPELINE.enrich_papers(source, {},
        save_checkpoint=lambda value: checkpoints.append(deepcopy(value)), save_progress=publish, retrieve=lambda _: [])
    assert peak == 2
    assert [result["paper_id"] for result in results] == ["P1", "P2", "P3"]
    assert next(value for value in progress if value["completed_papers"])["completed_papers"][0] == "P2"
    assert [value["current"] for value in progress] == sorted(value["current"] for value in progress)
    assert progress[-1]["current"] == 3 and progress[-1]["active_paper_ids"] == []
    for result in results:
        assert result["fact_extraction_profile"]["model_calls"] == 2
        assert PIPELINE.fact_is_usable(result["facts"][0])
        assert result["facts"][0]["evidence_refs"][0]["evidence_key"] == f"key-{result['paper_id']}"
    assert "facts" not in checkpoints[0]["entries"]["P1"]["result"]  # Earlier snapshots stay immutable.
    checkpoint = deepcopy(checkpoints[-1])
    monkeypatch.setattr(PIPELINE, "call_json_model", lambda *a, **k: pytest.fail("Completed verdicts must be reused"))
    resumed, _, _ = execute({**source, "attempt_id": "retry"}, checkpoint)
    assert all(PIPELINE.fact_is_usable(row["facts"][0]) for row in resumed)
    assert checkpoint == checkpoints[-1]


def test_failed_paper_does_not_discard_other_papers_and_old_checkpoints_survive_progress(monkeypatch):
    old = {"source_fingerprint": "source-P2", "result": {"paper_id": "P2", "facts": []},
           "agent_state": {"pending_requests": [{"field_id": "scope", "query": "cohort"}]}}
    def model(prompt, **kwargs):
        if kwargs["label"] == "matrix-facts-P1":
            raise TimeoutError("provider unavailable")
        return verify_response(prompt) if kwargs["required_list"] == "verdicts" else fact_response("P2")
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    results, checkpoints, progress = execute({"papers": [paper("P1"), paper("P2")]}, {"entries": {"P2": old}})
    assert results[0]["status"] == "failed"
    assert PIPELINE.fact_is_usable(results[1]["facts"][0])
    assert checkpoints[0]["entries"]["P2"] == old
    assert progress[-1]["failed_papers"] == ["P1"]


def test_one_combined_classification_request_preserves_route_verification(monkeypatch):
    item = paper("P1")
    item["partition_evidence_candidates"] = deepcopy(item["evidence_candidates"])
    axes = [{"axis_id": "approach", "axis_role": "primary_organization", "label": "Approach",
             "partitions": [{"partition_id": "optimized", "label": "Optimized protocol"}]}]
    calls = []
    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        if kwargs["label"].startswith("matrix-facts-"):
            return fact_response("P1")
        assert "topic_classification_assignments" in prompt and "routing_recommendation" in prompt
        return {"facts": [], "routing_recommendation": {
            "status": "classified", "label": "Optimized protocol", "confidence": 0.99,
            "evidence_key": "key-P1", "support_excerpt": item["evidence_candidates"][0]["content"]}}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    results, _, _ = execute({"papers": [item], "classification_axes": axes,
                            "routing_axis_id": "approach", "routing_categories": [{"label": "Optimized protocol"}]})
    assert len(calls) == 3  # Initial extraction + combined classification + mandatory verification.
    route = results[0]["routing_recommendation"]
    assert route["status"] == "classified" and route["verification"]["status"] == "supported"
    assert results[0]["automatic_resolution"]["targeted_recheck_attempted"]


def test_axis_without_partitions_does_not_trigger_per_paper_recheck(monkeypatch):
    item = paper("P1")
    item["partition_evidence_candidates"] = deepcopy(item["evidence_candidates"])
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        return verify_response(prompt) if kwargs["required_list"] == "verdicts" else fact_response("P1")

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute({"papers": [item], "classification_axes": [{
        "axis_id": "substrate", "axis_role": "primary_organization",
        "label": "Substrate class", "partitions": [],
    }]})[0][0]
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]
    assert result["classification_outcomes"] == []
    assert not result["automatic_resolution"]["targeted_recheck_attempted"]


def test_provisional_agent_outline_is_not_a_formal_fact_classification(monkeypatch):
    item = paper("P1")
    item["partition_evidence_candidates"] = deepcopy(item["evidence_candidates"])
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        return verify_response(prompt) if kwargs["required_list"] == "verdicts" else fact_response("P1")

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute({"papers": [item], "classification_axes": [{
        "axis_id": "topic_organization", "axis_role": "primary_organization",
        "source_type": "agent_recommended",
        "partitions": [{"partition_id": "section_1", "label": "Methods and evidence"}],
    }]})[0][0]
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]
    assert result["evidence_backed_tags"] == {}


def test_changed_outline_routes_reused_facts_without_reextracting_them(monkeypatch):
    item = paper("P1")
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        if kwargs["label"] == "matrix-facts-P1":
            return fact_response("P1")
        assert kwargs["label"] == "matrix-route-recheck-P1"
        return {"facts": [], "topic_classification_assignments": [{
            "axis_id": "approach", "partition_id": "optimized",
            "relation_to_paper": "primary_contribution", "confidence": 0.99,
            "evidence_key": "key-P1",
            "support_excerpt": item["evidence_candidates"][0]["content"],
        }]}

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    first = execute({"papers": [item]})[0][0]
    assert first["facts"] and first["facts"][0]["verification"]["status"] == "supported"
    calls.clear()
    changed = {**item, "source_fingerprint": "new-classification-contract",
               "reusable_fact_result": first,
               "partition_evidence_candidates": deepcopy(item["evidence_candidates"])}
    axes = [{"axis_id": "approach", "axis_role": "primary_organization",
             "partitions": [{"partition_id": "optimized", "label": "Optimized protocol"}]}]
    refreshed = execute({"papers": [changed], "classification_axes": axes})[0][0]
    assert calls == ["matrix-route-recheck-P1", "fact-verify-P1"]
    assert any(fact["field_id"] == "quantitative_results" for fact in refreshed["facts"])
    assert refreshed["evidence_backed_tags"]["approach"]


def test_explicit_topic_partition_can_be_refreshed_from_reused_facts(monkeypatch):
    item = paper("P1")
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        if kwargs["label"] == "matrix-facts-P1":
            return fact_response("P1")
        return {"facts": [], "topic_partition_classification": {
            "partition": "Optimized protocol", "confidence": 0.99,
            "evidence_key": "key-P1",
            "support_excerpt": item["evidence_candidates"][0]["content"],
        }}

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    first = execute({"papers": [item]})[0][0]
    calls.clear()
    refreshed_item = {**item, "source_fingerprint": "topic-partition-update",
                      "reusable_fact_result": first}
    result = execute({"papers": [refreshed_item],
                      "topic_partitions": ["Optimized protocol"]})[0][0]
    assert calls == ["matrix-route-recheck-P1"]
    assert result["topic_partition_classification"]["status"] == "classified"


def test_first_pass_formal_route_needs_only_extraction_and_verification(monkeypatch):
    item = paper("P1")
    item["partition_evidence_candidates"] = deepcopy(item["evidence_candidates"])
    axes = [{"axis_id": "approach", "axis_role": "primary_organization",
             "partitions": [{"partition_id": "optimized", "label": "Optimized protocol"}]}]
    calls = []
    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        assert kwargs["label"] == "matrix-facts-P1"
        return {**fact_response("P1"), "topic_classification_assignments": [{
            "axis_id": "approach", "partition_id": "optimized", "relation_to_paper": "primary_contribution",
            "confidence": 0.99, "evidence_key": "key-P1", "support_excerpt": item["evidence_candidates"][0]["content"]}]}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute({"papers": [item], "classification_axes": axes, "routing_axis_id": "approach",
                      "routing_categories": [{"label": "Optimized protocol"}]})[0][0]
    assert len(calls) == 2
    assert result["routing_recommendation"]["status"] == "formal_axis_route_available"
    assert all(fact["verification"]["status"] == "supported" for fact in result["facts"])


def test_first_pass_explicit_route_avoids_a_separate_routing_call(monkeypatch):
    item = paper("P1")
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        assert kwargs["label"] == "matrix-facts-P1"
        assert "routing_recommendation" in prompt
        return {
            **fact_response("P1"),
            "routing_recommendation": {
                "status": "classified",
                "label": "Optimized protocol",
                "confidence": 0.99,
                "evidence_key": "key-P1",
                "support_excerpt": item["evidence_candidates"][0]["content"],
                "rationale": "The source states the optimized protocol.",
            },
        }

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute(
        {
            "papers": [item],
            "routing_axis_id": "approach",
            "routing_categories": [{"label": "Optimized protocol"}],
        }
    )[0][0]
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]
    assert result["routing_recommendation"]["status"] == "classified"
    assert result["routing_recommendation"]["extraction_method"] == "initial_fact_pass"
    assert result["routing_recommendation"]["verification"]["status"] == "supported"


def test_review_ready_baseline_defers_optional_supplements(monkeypatch):
    item = paper("P1")
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        return {
            **fact_response("P1"),
            "evidence_requests": [
                {
                    "field_id": "quantitative_results",
                    "query": "Find an additional control result.",
                    "experiment_id": "control",
                }
            ],
        }

    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute(
        {"papers": [item]},
    )[0][0]
    assert calls == ["matrix-facts-P1", "fact-verify-P1"]
    profile = result["fact_extraction_profile"]
    assert profile["supplement_rounds"] == 0
    assert profile["deferred_supplement_count"] == 1
    assert profile["stop_reason"] == "review_ready_deferred_supplements"
    assert profile["unresolved_requests"][0]["experiment_id"] == "control"


def test_identical_source_fact_restores_cached_semantic_verdict():
    item = paper("P1")
    cached = PIPELINE.normalize_result(item, fact_response("P1"))["facts"][0]
    cached["verification"] = {
        "contract": PIPELINE.FACT_VALIDATION_VERSION,
        "status": "supported",
        "reason": "The same experiment explicitly reports this result.",
        "input_fingerprint": PIPELINE.review_fingerprint(cached),
    }
    item["existing_fact_result"] = {"facts": [deepcopy(cached)]}
    current = PIPELINE.normalize_result(item, fact_response("P1"))
    state = {}
    PIPELINE.run_fact_agent(
        item,
        current,
        model_call=lambda *args, **kwargs: pytest.fail("Cached verdict must be reused"),
        retrieve=lambda _: [],
        state=state,
        report=lambda *_args, **_kwargs: None,
    )
    assert state["verification_cache_hits"] == 1
    assert current["facts"][0]["verification"]["status"] == "supported"


def test_changed_source_lineage_cannot_restore_cached_semantic_verdict():
    item = paper("P1")
    cached = PIPELINE.normalize_result(item, fact_response("P1"))["facts"][0]
    cached["verification"] = {
        "contract": PIPELINE.FACT_VALIDATION_VERSION,
        "status": "supported",
        "reason": "The same experiment explicitly reports this result.",
        "input_fingerprint": PIPELINE.review_fingerprint(cached),
    }
    item["existing_fact_result"] = {"facts": [deepcopy(cached)]}
    item["evidence_candidates"][0]["source_lineage_hash"] = "changed-lineage"
    current = PIPELINE.normalize_result(item, fact_response("P1"))
    assert PIPELINE.restore_cached_verifications(item, current) == 0
    assert PIPELINE.fact_needs_verification(current["facts"][0])


@pytest.mark.parametrize("failure", ["unavailable", "unsupported"])
def test_combined_classification_failure_keeps_initial_facts_and_never_invents_a_route(monkeypatch, failure):
    item = paper("P1")
    item["partition_evidence_candidates"] = deepcopy(item["evidence_candidates"])
    axes = [{"axis_id": "approach", "axis_role": "primary_organization", "partitions": [{"partition_id": "a", "label": "A"}]}]
    calls = []
    def model(prompt, **kwargs):
        calls.append(kwargs["label"])
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        if kwargs["label"].startswith("matrix-facts-"):
            return fact_response("P1")
        if failure == "unavailable":
            raise TimeoutError("unavailable")
        return {"facts": [], "routing_recommendation": {"status": "classified", "label": "A", "confidence": 1,
                "evidence_key": "invented", "support_excerpt": "unsupported"}}
    monkeypatch.setattr(PIPELINE, "call_json_model", model)
    result = execute({"papers": [item], "classification_axes": axes, "routing_axis_id": "approach",
                      "routing_categories": [{"label": "A"}]})[0][0]
    assert len(calls) == 3
    assert PIPELINE.fact_is_usable(result["facts"][0])
    assert result["routing_recommendation"]["status"] == "insufficient_evidence"


def test_supplement_uses_only_gap_sources_and_requested_roles():
    item = paper("P1")
    item["partition_evidence_candidates"] = [{"evidence_key": "unrelated", "content": "UNRELATED CLASSIFICATION CONTEXT"}]
    result = PIPELINE.normalize_result(item, fact_response("P1"))
    result["evidence_requests"] = [{"field_id": "quantitative_results", "query": "control yield", "experiment_id": "control"}]
    context = {"evidence_key": "control", "chunk_id": "control", "question_ids": ["quantitative_results", "scope"],
               "content": "The control gave 42% yield. The scope included 12 samples.", "source_lineage_hash": "lineage-P1"}
    prompts = []
    def model(prompt, **kwargs):
        if kwargs["required_list"] == "verdicts":
            return verify_response(prompt)
        prompts.append(prompt)
        return {"facts": [
            {"field_id": "quantitative_results", "value": "42% yield", "evidence_key": "control",
             "support_excerpt": "The control gave 42% yield.", "confidence": 0.99},
            {"field_id": "scope", "value": "12 samples", "evidence_key": "control",
             "support_excerpt": "The scope included 12 samples.", "confidence": 0.99}],
            "evidence_requests": [{"field_id": "scope", "query": "expand into sample selection"}]}
    PIPELINE.run_fact_agent(item, result, model_call=model, retrieve=lambda _: [context], state={}, report=lambda _: None)
    assert len(prompts) == 1 and "UNRELATED CLASSIFICATION CONTEXT" not in prompts[0]
    assert all(fact["field_id"] == "quantitative_results" for fact in result["facts"])
    assert len(result["facts"]) == 2 and all(PIPELINE.fact_is_usable(fact) for fact in result["facts"])
    assert not result["normalization_rejections"]
    assert item["partition_evidence_candidates"]  # The trusted registry was not narrowed.


def test_evidence_mailbox_never_overwrites_another_papers_request(tmp_path, monkeypatch):
    request_path, response_path = tmp_path / "request.json", tmp_path / "response.json"
    response_path.write_text("{}", encoding="utf-8")
    first_written, second_started, second_written, release_first = Event(), Event(), Event(), Event()
    mailbox = {}
    def write(path, value):
        mailbox.update(value)
        (first_written if value["paper_id"] == "P1" else second_written).set()
    def read(path):
        if mailbox["paper_id"] == "P1":
            assert release_first.wait(3)
        return {"request_id": mailbox["request_id"], "evidence": [{"paper_id": mailbox["paper_id"]}]}
    monkeypatch.setattr(PIPELINE, "write_json", write)
    monkeypatch.setattr(PIPELINE, "read_json", read)
    retrieve = PIPELINE.evidence_mailbox(request_path, response_path)
    def second():
        second_started.set()
        return retrieve({"paper_id": "P2"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(retrieve, {"paper_id": "P1"})
        assert first_written.wait(3)
        other = pool.submit(second)
        assert second_started.wait(3)
        try:
            assert not second_written.wait(0.05)
        finally:
            release_first.set()
        assert first.result() == [{"paper_id": "P1"}]
        assert other.result() == [{"paper_id": "P2"}]
