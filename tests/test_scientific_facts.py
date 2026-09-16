from review_writer_core.scientific_facts import (
    FACT_VALIDATION_VERSION, attach_fact_to_evidence, fact_identity, fact_relation_issues, fact_support_spans, merge_facts,
    build_fact_comparison, numerical_tokens_supported,
    is_additive_fact_repair,
    fact_is_usable, fact_usage, fact_claim_issues, registered_fact_bindings, claim_assertion_ceiling,
)
from review_writer_core.evidence_integrity import source_contains_excerpt
import pytest


@pytest.mark.parametrize("source,fact,expected", [
    ("direct_source_report", "attributed_author_interpretation", "attributed_author_interpretation"),
    ("abstract_report_only", "direct_source_report", "abstract_report_only"),
    ("direct_report_with_local_context", "direct_source_report", "direct_report_with_local_context"),
    ("direct_source_report", None, "context_only"),
    (None, None, "context_only"),
    ("unknown_ceiling", "direct_source_report", "context_only"),
    ("direct_source_report", "unknown_ceiling", "context_only"),
])
def test_claim_ceiling_respects_both_source_and_selected_fact(source, fact, expected):
    assert claim_assertion_ceiling([{"assertion_ceiling": source}], [{"assertion_ceiling": fact}]) == expected


def test_unselected_fact_on_same_passage_does_not_reduce_direct_claim():
    passage = {"assertion_ceiling": "direct_source_report", "fact_bindings": [
        {"fact_id": "interpretation", "assertion_ceiling": "attributed_author_interpretation"},
        {"fact_id": "result", "assertion_ceiling": "direct_source_report"},
    ]}
    assert claim_assertion_ceiling([passage], [passage["fact_bindings"][1]]) == "direct_source_report"
    assert claim_assertion_ceiling([]) == "context_only"


@pytest.mark.parametrize("unit,number", [(r"^{\circ} C", "245"), ("K", "318"), (r"\%", "72"), ("mg", "125")])
def test_split_scalar_math_digits_are_formatting_not_new_values(unit, number):
    quote = "$" + " ".join(number) + " " + unit + "$"
    assert numerical_tokens_supported(number, quote)
    assert not numerical_tokens_supported(str(int(number) + 1), quote)
    assert not numerical_tokens_supported(number, " ".join(number) + " " + unit)
    assert not numerical_tokens_supported(number, "$1 2; " + " ".join(number) + " " + unit + "$")
    wrapped_unit = r"\mathrm { " + unit + " }"
    assert numerical_tokens_supported(number, "$" + " ".join(number) + " " + wrapped_unit + " ,$")


@pytest.mark.parametrize("metric,other", [("yield", "ee"), ("accuracy", "recall"), ("sensitivity", "specificity")])
def test_ambiguous_local_association_is_not_disproved_by_another_experiment(metric, other):
    sources = [f"Trial A: 83% {metric}.", "Trial B attained an outcome of 67%."]
    assert not fact_relation_issues(f"Trial B: 67% {metric}", sources)
    assert fact_relation_issues(f"Trial B: 67% {metric}", [sources[0], f"Trial B: 67% {other}."])


@pytest.mark.parametrize("metric", ["yield", "accuracy", "specificity"])
def test_selected_experiment_cannot_borrow_metric_from_another_selected_fact(metric):
    facts = [{"value": f"Trial {trial} gave {value}% {metric}.", "experiment_id": f"Trial {trial}",
              "support_level": "direct", "evidence_refs": [{"evidence_key": trial}]}
             for trial, value in (("Alpha", 83), ("Beta", 67))]
    assert not fact_claim_issues(f"Trial Alpha gave 83% {metric}; Trial Beta gave 67% {metric}.", facts, claim_kind="reported_finding")
    assert fact_claim_issues(f"Trial Alpha gave 67% {metric}.", facts, claim_kind="reported_finding")
    assert fact_claim_issues(f"Trial Alpha gave 67% {metric}; Trial Beta gave 83% {metric}.", facts, claim_kind="reported_finding")


def test_abstract_can_describe_distinct_objects_but_not_claim_superiority():
    fact = {"value": "One study examines images; another examines audio.", "support_level": "abstract_limited",
            "evidence_refs": [{"evidence_key": "abstract"}]}
    assert not fact_claim_issues(fact["value"], [fact], claim_kind="cross_study_comparison")
    assert not fact_claim_issues("The outcomes cannot be ranked because the measurement bases differ.", [fact], claim_kind="cross_study_comparison")
    assert "abstract_detail_requires_full_text" in fact_claim_issues(
        "Image analysis is superior to audio analysis.", [fact], claim_kind="cross_study_comparison")


def test_synthesis_can_name_multiple_selected_systems_without_subject_scope_false_positive():
    facts = [
        {"value": "The authors used CuI for aldehyde coupling.", "subject": "the reaction",
         "support_level": "direct", "evidence_refs": [{"evidence_key": "a"}]},
        {"value": "CuBr was used for alkynol coupling with 83% yield.", "subject": "CuBr",
         "support_level": "direct", "evidence_refs": [{"evidence_key": "b"}]},
    ]
    thesis = "The studies use CuI for aldehyde coupling and CuBr for alkynol coupling."
    assert not fact_claim_issues(thesis, facts, claim_kind="provisional_synthesis")
    assert fact_claim_issues(thesis.replace("CuI", "CdI2"), facts, claim_kind="provisional_synthesis")
    assert fact_claim_issues("CuBr gave 91% yield.", facts, claim_kind="provisional_synthesis")
    facts[1]["experiment_id"] = "Trial Beta"
    assert fact_claim_issues("Trial Beta uses CuI.", facts, claim_kind="provisional_synthesis")


def test_background_does_not_poison_quantitative_assertions_supported_by_selected_fulltext():
    abstract = {"value": "The study examined image classification.", "support_level": "abstract_limited",
                "evidence_refs": [{"evidence_key": "abstract"}]}
    direct = {"value": "The external cohort accuracy was 84%.", "support_level": "direct",
              "evidence_refs": [{"evidence_key": "fulltext"}]}
    assert not fact_claim_issues("The study examined image classification. The external cohort accuracy was 84%.",
                                 [abstract, direct], claim_kind="provisional_synthesis")
    assert fact_claim_issues("The study examined image classification. The external cohort accuracy was 98%.",
                            [abstract, direct], claim_kind="provisional_synthesis")


def test_metric_values_are_not_interchangeable():
    quote = "The result was 91% yield and 96% ee."
    assert not fact_relation_issues("91% yield and 96% ee", [quote])
    assert fact_relation_issues("96% yield and 91% ee", [quote])
    assert not fact_relation_issues("yield of 91%", [quote])
    assert fact_relation_issues("accuracy 96%", ["accuracy 91%, recall 96%"])
    assert not numerical_tokens_supported("95% yield", "195% yield")
    assert numerical_tokens_supported("91% yield", "Entry 3 91 % yield")
    assert numerical_tokens_supported("91% yield", "The isolated yield was 91%.")
    assert not numerical_tokens_supported("91", "91.5")


def test_markup_equivalence_never_invents_ocr_digits_or_ranges():
    assert source_contains_excerpt("Measured by <sup>1</sup>H NMR with ZnBr<sub>2</sub>.",
                                   "Measured by ¹H NMR with ZnBr₂.")
    assert not source_contains_excerpt("ZnBr", "ZnBr2")
    assert not numerical_tokens_supported("7-33%", "7e33%")
    assert not numerical_tokens_supported("99% ee", "499% ee")


def test_fact_usage_is_contextual_and_classification_never_counts_as_results():
    base = {"value": "The authors report a selective method.", "evidence_refs": [{"evidence_key": "a"}]}
    abstract = {**base, "support_level": "abstract_limited"}
    assert fact_usage(abstract) == "background"
    assert fact_is_usable(abstract)
    assert not fact_is_usable(abstract, purpose="detail")
    assert not fact_is_usable({**base, "fact_type": "classification"})
    assert not fact_is_usable({**base, "validation_contract": FACT_VALIDATION_VERSION})
    assert fact_is_usable({**base, "validation_contract": FACT_VALIDATION_VERSION,
                          "verification": {"status": "supported"}})
    assert not build_fact_comparison("S1", ["P1"], {"P1": {"scientific_facts": [
        {**abstract, "field_id": "quantitative_results"}, {**base, "field_id": "topic_partition"},
    ]}})["cells"]


def test_selected_fact_quote_retains_identity_but_not_other_experiments():
    fact = {"value": "69% yield and 1:1 dr", "support_level": "direct",
            "evidence_refs": [{"evidence_key": "a", "support_excerpt": "Product 1m gave 69% yield and 1:1 dr."}]}
    assert not fact_claim_issues("Product 1m gave 69% yield and 1:1 dr.", [fact], claim_kind="reported_finding")
    assert fact_claim_issues("Product 1m gave 99% yield.", [fact], claim_kind="reported_finding")
    abstract = {**fact, "support_level": "abstract_limited"}
    assert "abstract_detail_requires_full_text" in fact_claim_issues("69% yield", [abstract], claim_kind="reported_finding")
    assert not fact_claim_issues("The authors report a selective method.", [abstract], claim_kind="historical_transition")


def test_selected_fact_cannot_borrow_conditions_from_adjacent_unselected_experiment():
    fact = {"value": "Trial A gave 84% accuracy.", "experiment_id": "Trial A", "support_level": "direct",
            "support_excerpt": "Trial A gave 84% accuracy at 245 K. Trial B used 318 K.",
            "qualifiers": {"temperature": "245 K"}, "evidence_refs": [{"evidence_key": "table"}]}
    assert not fact_claim_issues("Trial A gave 84% accuracy at 245 K.", [fact], claim_kind="reported_finding")
    assert fact_claim_issues("Trial A gave 84% accuracy at 318 K.", [fact], claim_kind="reported_finding")
    fact["qualifiers"] = {"temperature": "999 K"}
    assert fact_claim_issues("Trial A gave 84% accuracy at 999 K.", [fact], claim_kind="reported_finding")


def test_canonical_binding_requires_every_span_and_owned_current_sources():
    fact = {"fact_id": "F1", "paper_id": "P1", "value": "91% yield at 50 C", "support_level": "direct",
            "support_excerpt": "Entry A at 50 C. Entry A gave 91% yield.", "evidence_refs": [
                {"evidence_key": "a", "support_excerpt": "Entry A at 50 C.", "source_lineage_hash": "v1"},
                {"evidence_key": "b", "support_excerpt": "Entry A gave 91% yield.", "source_lineage_hash": "v1"}]}
    a = {"paper_id": "P1", "evidence_key": "a", "content": "Entry A at 50 C.", "source_lineage_hash": "v1"}
    b = {"paper_id": "P1", "evidence_key": "b", "content": "Entry A gave 91% yield.", "source_lineage_hash": "v1"}
    attach_fact_to_evidence(a, fact)
    assert "F1" in registered_fact_bindings([a, b], ["P1"])
    assert not registered_fact_bindings([a], ["P1"])
    assert not registered_fact_bindings([a, {**b, "paper_id": "P2"}], ["P1", "P2"])
    assert not registered_fact_bindings([a, {**b, "source_lineage_hash": "v2"}], ["P1"])
    assert not registered_fact_bindings([a, b], ["P2"])


def test_multispan_quotes_are_registered_individually():
    sources = {
        "a": {"content": "Entry A at 50 C", "page_start": 2},
        "b": {"content": "Entry A gave 91% yield", "page_start": 3},
    }
    fact = {"value": "Entry A gave 91% yield at 50 C", "support_spans": [
        {"evidence_key": "a", "support_excerpt": "Entry A at 50 C", "page_start": 999},
        {"evidence_key": "b", "support_excerpt": "Entry A gave 91% yield"},
    ]}
    assert [span["page_start"] for span in fact_support_spans(fact, sources)] == [2, 3]
    fact["support_spans"][1]["evidence_key"] = "forged"
    assert not fact_support_spans(fact, sources)


def test_fact_identity_and_merge_preserve_different_experiments_and_results():
    a = {"paper_id": "P1", "field_id": "quantitative_results", "value": "91% yield",
         "experiment_id": "entry 1", "evidence_refs": [{"evidence_key": "a"}]}
    b = {**a, "value": "72% yield", "experiment_id": "entry 2"}
    assert len(merge_facts([a, b], [a])) == 2
    assert fact_identity(a) != fact_identity({**a, "value": "92% yield"})
    assert fact_identity(a) != fact_identity({**a, "qualifiers": {"temperature": "50 C"}})
    same_result = {**a, "experiment_id": "", "support_excerpt": "Entry A gave 91% yield."}
    other_entry = {**same_result, "support_excerpt": "Entry B gave 91% yield."}
    assert len(merge_facts([same_result, other_entry])) == 2


def test_shared_source_keeps_all_verified_results_and_does_not_erase_audit():
    a = {"paper_id": "P1", "field_id": "quantitative_results", "value": "91% yield",
         "evidence_refs": [{"evidence_key": "a"}], "support_level": "direct",
         "verification": {"contract": FACT_VALIDATION_VERSION, "status": "supported"}}
    evidence = {}
    attach_fact_to_evidence(evidence, a)
    attach_fact_to_evidence(evidence, {**a, "value": "control 42% yield"})
    attach_fact_to_evidence(evidence, {**a, "value": "96% yield", "verification": {"status": "uncertain"}})
    assert evidence["normalized_fact_values"] == ["91% yield", "control 42% yield"]
    repeated = {key: value for key, value in a.items() if key != "verification"}
    assert merge_facts([a], [repeated])[0]["verification"]["status"] == "supported"


def test_comparison_retains_experiments_without_claiming_direct_comparability():
    fact = {"field_id": "quantitative_results", "value": "91% yield", "evidence_refs": [{"evidence_key": "a"}]}
    rows = {"P1": {"scientific_facts": [fact, {**fact, "value": "42% yield", "experiment_id": "control"}]},
            "P2": {"scientific_facts": [{**fact, "value": "99% yield"}]}}
    table = build_fact_comparison("S1", list(rows), rows)
    assert len(table["cells"]) == 3
    policy = table["comparability"][0]
    assert policy["level"] == "source_grounded_synthesis"
    assert policy["ranking_status"] == "not_assessed"
    assert "source_supported_qualitative_synthesis" in policy["allowed_uses"]
    assert "quantitative_ranking" in policy["requires_verification"]
    assert table["input_fingerprint"] == build_fact_comparison("S1", list(rows), rows)["input_fingerprint"]


def test_additive_repair_proves_unchanged_old_evidence_not_just_matching_ids():
    from copy import deepcopy
    fact = {"value": "91% yield", "evidence_refs": [{"evidence_key": "a"}]}
    previous = {"review_topic": "Topic", "rows": [{"paper_id": "P1", "scientific_facts": [fact]}]}
    current = deepcopy(previous)
    current["rows"][0]["scientific_facts"].append({"field_id": "claim_targeted_fact", "value": "Control: 42% yield",
        "evidence_refs": [{"evidence_key": "b"}], "origin_paragraph_id": "S1-p1", "support_excerpt": "Control: 42% yield"})
    current["fact_repair_history"] = [{"operation": "draft_targeted_fact_promotion"}]
    assert is_additive_fact_repair(previous, current)
    changed = deepcopy(current)
    changed["rows"][0]["scientific_facts"][0]["value"] = "42% yield"
    assert not is_additive_fact_repair(previous, changed)
    changed = deepcopy(current)
    changed["review_topic"] = "Another scope"
    assert not is_additive_fact_repair(previous, changed)
    changed = deepcopy(current)
    changed["rows"][0]["scientific_facts"].pop(0)
    assert not is_additive_fact_repair(previous, changed)
def test_study_ownership_is_part_of_fact_audit_identity():
    from review_writer_core.scientific_facts import review_fingerprint, verify_plain_source_quote
    fact = {"value": "A result", "field_id": "scope"}
    legacy = review_fingerprint(fact)
    own = {**fact, "study_ownership": "own_results"}
    prior = {**fact, "study_ownership": "prior_work"}
    assert len({legacy, review_fingerprint(own), review_fingerprint(prior)}) == 3
    assert verify_plain_source_quote(own, {}) is False
