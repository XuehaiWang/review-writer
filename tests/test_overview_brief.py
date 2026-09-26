import copy
from unittest.mock import patch

from review_writer_core.stages.figures.overview_brief import brief_prompt, apply_brief
from tests.test_overview_prompt_context import large_features
from tests.test_overview_content_pack import overview


def test_brief_is_bounded_and_grouping_keeps_original_sources():
    features = large_features()
    original = copy.deepcopy(features["argument_execution"])
    prompt, context = brief_prompt(features, "Review summary.")
    assert len(prompt) < 14000
    ids = [c["claim_id"] for c in context["claims"][:2]]
    apply_brief(features, {"directions": [{"label": "Selective synthesis",
        "summary": "Catalysis controls product selectivity.", "claim_ids": ids}]}, context)
    assert features["overview_modules"] == ["Selective synthesis"]
    assert len(features["overview_source_modules"]) == 38
    assert features["overview_evidence_bindings"]["Selective synthesis"]["claim_ids"] == ids
    assert features["argument_execution"] == original


def test_unknown_sources_and_repeated_directions_are_not_displayed():
    features = large_features()
    _, context = brief_prompt(features, "Summary")
    row = {"label": "Catalytic control", "summary": "Catalysis controls product selectivity.",
           "claim_ids": [context["claims"][0]["claim_id"]]}
    apply_brief(features, {"directions": [row, row, {**row, "claim_ids": ["invented"]}]}, context)
    assert len(features["overview_brief"]["directions"]) == 1


def test_confirmed_structure_uses_one_call_and_cache(tmp_path):
    features = {"_project_dir": tmp_path, "overview_structure_contract": {
        "status": "resolved", "role": "target_product", "smiles": "C=C=C"}}
    # Cache identity includes the projection contracts; re-run from identical
    # source features, just as a fresh task materializes them.
    original = copy.deepcopy(features)
    with patch.object(overview, "call_gateway_json", return_value={}) as model:
        assert overview.resolve_reaction_scheme(features) is None
        assert overview.resolve_skeleton_smiles(features) == "C=C=C"
        overview.resolve_reaction_scheme(original)
        assert model.call_count == 1
        assert model.call_args.kwargs["label"] == "overview-visual-brief"


def test_substrate_cannot_become_product_and_model_reaction_is_ignored():
    features = {"substrate_smiles": "CCO", "overview_structure_contract": {"status": "unresolved"}}
    with patch.object(overview, "_cached_overview_json", return_value={
        "product_smiles": "C=C=C", "substrate_smiles": "C#C"}):
        assert overview.resolve_reaction_scheme(features) is None
        assert overview.resolve_skeleton_smiles(features) == ""


def test_confirmed_reaction_must_match_confirmed_product():
    features = {"overview_structure_contract": {"status": "resolved", "role": "target_product",
        "smiles": "C=C=C"}, "confirmed_reaction": {"status": "confirmed",
        "substrate_smiles": "C#C", "product_smiles": "C#C"}}
    with patch.object(overview, "_cached_overview_json", return_value={}):
        assert overview.resolve_reaction_scheme(features) is None
        assert overview.resolve_skeleton_smiles(features) == "C=C=C"


def test_legacy_manuscript_has_explicit_excerpt_provenance():
    _, context = brief_prompt({}, "A supported manuscript conclusion.")
    assert context["claims"][0]["binding_level"] == "current_manuscript"
    assert context["claims"][0]["claim_id"] == "current-manuscript-excerpt"
