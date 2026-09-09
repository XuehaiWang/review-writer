from copy import deepcopy

import pytest

from review_writer_core.stages.sections.fact_routing import fact_routing_report, route_fact_questions


PLANS = [{"question_id": name} for name in (
    "section_focus", "object_input", "method_conditions", "quantitative_results", "scope", "specialized_metrics",
    "required_claim_01", "claim_boundary_01",
)]


@pytest.mark.parametrize("label,expected", [
    ("quantitative_results", "quantitative_results"),
    ("Quantitative Results", "quantitative_results"),
    ("Yield", "quantitative_results"),
    ("accuracy", "quantitative_results"),
    ("reaction-condition", "method_conditions"),
    ("dataset", "object_input"),
    ("substrate_scope", "scope"),
    ("effect size", "specialized_metrics"),
])
def test_shared_role_vocabulary_routes_clear_labels(label, expected):
    fact = {"field_id": label, "value": "A source-supported observation."}
    before = deepcopy(fact)
    route = route_fact_questions(fact, PLANS)
    assert route["question_ids"] == [expected]
    assert route["status"] == "rule_matched"
    assert fact == before


@pytest.mark.parametrize("value", ["The isolated yield was 87%.", "The product was isolated in 87% yield.",
                                  "The external accuracy was 84%."])
def test_explicit_metric_value_can_route_unknown_label(value):
    route = route_fact_questions({"field_id": "custom_field", "value": value}, PLANS)
    assert route["question_ids"] == ["quantitative_results"]
    assert route["method"] == "explicit_metric_value"


@pytest.mark.parametrize("field", ["control experiment", "novel_descriptor", "required_claim_01", "claim_boundary_01", ""])
def test_unknown_or_ambiguous_field_never_grants_claim_question_coverage(field):
    route = route_fact_questions({"field_id": field, "value": "The authors report protocol Alpha."}, PLANS)
    assert route["question_ids"] == []
    assert route["status"] == "needs_semantic_planning"


def test_known_role_outside_this_section_is_not_forced_into_focus():
    route = route_fact_questions({"field_id": "mechanism", "value": "An observed intermediate."}, PLANS)
    assert route["canonical_field_id"] == "mechanism"
    assert route["question_ids"] == []
    assert route["method"] == "field_outside_question_plan"


def test_validated_usage_report_distinguishes_nonselection_from_prompt_budget():
    hits = [{"paper_id": "P001", "fact_bindings": [{"fact_id": fid}], "fact_routes": [{
        "fact_id": fid, "section_id": "S02", "status": "needs_semantic_planning",
    }]} for fid in ["used", "unused", "omitted"]]
    writing = {"section_id": "S02", "claims": [{"claim_id": "C1", "paragraph_id": "S02-p1",
        "claim_kind": "reported_method", "fact_ids": ["used"]}]}
    before = deepcopy(hits)
    report = {row["fact_id"]: row for row in fact_routing_report(hits, writing, hits[:2])}
    assert report["used"]["decision"] == "used_in_validated_plan"
    assert report["used"]["claim_kinds"] == ["reported_method"]
    assert report["unused"]["decision"] == "not_selected"
    assert report["omitted"]["decision"] == "deferred_by_prompt_budget"
    assert hits == before
    assert fact_routing_report(hits, {**writing, "section_id": "S03"}, hits) == []
