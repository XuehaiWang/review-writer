"""Same recovery contract for chemistry, clinical studies and computing."""
import pytest
from review_writer_core.evidence_queries import build_fact_query_plans, normalize_fact_request, fact_request_identity


@pytest.mark.parametrize("field,terms", [
    ("method_conditions", ["annealing", "temperature"]),
    ("validation_evidence", ["external cohort", "follow-up"]),
    ("quantitative_results", ["benchmark", "latency"]),
])
def test_structured_recovery_uses_bounded_targets_not_instructions(field, terms):
    request = {"field_id": field, "query": "Please provide the surrounding text identifying all relevant experiments.",
               "target_terms": terms, "experiment_id": "Trial Delta"}
    plans = build_fact_query_plans(request)
    assert 1 <= len(plans) <= 3
    assert "Trial Delta".casefold() in plans[0]["websearch_query"]
    assert "provide" not in str(plans)
    assert terms == plans[0]["term_groups"][1]
    assert plans[1]["term_groups"] == [["trial delta"]]


def test_legacy_request_and_malformed_optional_fields_are_safe():
    request = {"field_id": "scope", "query": "Provide the substrate scope tables and surrounding text to determine applicability."}
    assert len(build_fact_query_plans(request)) <= 3
    assert " OR " in build_fact_query_plans(request)[0]["websearch_query"]
    assert normalize_fact_request({**request, "target_terms": "not a list", "evidence_keys": 9})["target_terms"] == []
    assert not build_fact_query_plans({"field_id": "invented", "query": "anything"})


def test_registered_custom_field_uses_bounded_request_and_query_plan():
    field = "photocatalyst_excited_state_behavior"
    request = {
        "field_id": field,
        "query": "Find the reported excited-state oxidation behavior.",
        "target_terms": ["excited-state", "oxidation potential"],
    }
    assert normalize_fact_request(request) is None
    normalized = normalize_fact_request(request, allowed_field_ids=[field])
    assert normalized and normalized["field_id"] == field
    plans = build_fact_query_plans(request, allowed_field_ids=[field])
    assert 1 <= len(plans) <= 3
    assert "excited-state" in plans[0]["websearch_query"]


def test_problem_identity_ignores_rephrasing_not_experiments_or_sources():
    request = {"field_id": "quantitative_results", "query": "Find the result", "target_terms": ["accuracy", "control"],
               "experiment_id": "Trial Delta", "evidence_keys": ["a"]}
    assert fact_request_identity(request) == fact_request_identity({**request, "query": "Please recheck the result",
                                                                  "target_terms": ["control", "accuracy"]})
    assert fact_request_identity(request) != fact_request_identity({**request, "experiment_id": "Trial Epsilon"})
    assert fact_request_identity(request) != fact_request_identity({**request, "evidence_keys": ["b"]})
