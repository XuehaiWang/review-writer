import copy
import json

from review_writer_core.stages.figures.overview_prompt_context import (
    compact_json, overview_prompt_context,
)


def large_features():
    sections = [{"section_id": f"S{i}", "claims": [{
        "claim_id": f"S{i}-C{j}", "claim": f"Method {j} in section {i} works only under the tested conditions.",
        "binding_level": "source_passage", "paper_ids": [f"P{j}"],
        "evidence_refs": [{"passage": "large source passage " * 1000}],
    } for j in range(40)]} for i in range(38)]
    bindings = {f"Module {i}": {"section_id": f"S{i}"} for i in range(38)}
    return {"argument_execution": {"sections": sections}, "overview_evidence_bindings": bindings,
            "overview_content_contract": {"title": "Review", "modules": list(bindings),
                "primary_axis": "Approach", "source_claim_bindings": [c for s in sections for c in s["claims"]]},
            "overview_structure_contract": {"status": "resolved", "role": "target_product",
                "smiles": "C=C=C", "required_smarts": "C=C=C", "fact_id": "F1",
                "evidence_refs": [{"passage": "long provenance " * 5000}]}}


def test_large_context_is_bounded_fair_and_does_not_mutate_evidence():
    features = large_features()
    before = copy.deepcopy(features)
    context = overview_prompt_context(features)
    assert len(compact_json(context)) < 38000
    assert {c["section_id"] for c in context["claims"]} == {f"S{i}" for i in range(38)}
    assert all(c["claim"].endswith("only under the tested conditions.") for c in context["claims"])
    assert context["structure"]["smiles"] == "C=C=C"
    assert context["structure"]["required_smarts"] == "C=C=C"
    assert context["structure"]["status"] == "resolved"
    assert "large source passage" not in compact_json(context)
    assert features == before




def test_unbound_sections_are_excluded_and_long_claim_does_not_starve_later_claim():
    features = large_features()
    features["overview_evidence_bindings"] = {"One": {"section_id": "S37"}}
    claims = features["argument_execution"]["sections"][-1]["claims"]
    claims.insert(0, {"claim_id": "long", "claim": "huge " * 50000})
    claims.append(dict(claims[1], claim_id="duplicate"))
    context = overview_prompt_context(features)
    assert {c["section_id"] for c in context["claims"]} == {"S37"}
    assert "long" not in {c["claim_id"] for c in context["claims"]}
    assert "duplicate" not in {c["claim_id"] for c in context["claims"]}


def test_unresolved_structure_is_not_promoted_from_substrate():
    features = {"substrate_smiles": "CCO", "overview_structure_contract": {
        "status": "unresolved", "role": "target_product", "smiles": "", "reason": "No evidence"}}
    context = overview_prompt_context(features)
    assert context["structure"]["status"] == "unresolved"
    assert context["structure"]["smiles"] == ""
