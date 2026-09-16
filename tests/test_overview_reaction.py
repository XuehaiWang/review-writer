from __future__ import annotations

import pytest

from review_writer_core.stages.figures.overview_reaction import (
    choose_reaction_presentation,
    is_reaction_mode,
    map_reaction_r_groups,
    normalize_reaction_review,
    scheme_for_presentation,
)


SCHEME = {
    "substrate_smiles": "*C#C.*C=O",
    "substrate_name": "terminal alkyne and aldehyde",
    "product_smiles": "*C(O)=C=C*",
    "product_name": "axially chiral allenol",
    "catalyst_label": "prolinol, toluene",
    "reaction_name": "asymmetric allenol synthesis",
}


def test_supported_connectivity_keeps_generic_reaction_without_conditions():
    decision = choose_reaction_presentation(
        {
            "connectivity_supported": True,
            "product_supported": True,
            "conditions_supported": False,
            "confidence": 58,
            "reason": "Transformation class is explicit; conditions are incomplete.",
        }
    )
    assert decision["mode"] == "reaction_generic"
    assert is_reaction_mode(decision["mode"])
    presented = scheme_for_presentation(SCHEME, decision)
    assert presented["substrate_smiles"] == SCHEME["substrate_smiles"]
    assert presented["product_smiles"] == SCHEME["product_smiles"]
    assert presented["catalyst_label"] == ""


def test_exact_reaction_keeps_supported_conditions():
    decision = choose_reaction_presentation(
        {
            "connectivity_supported": True,
            "product_supported": True,
            "conditions_supported": True,
            "confidence": 91,
        }
    )
    assert decision["mode"] == "reaction"
    assert scheme_for_presentation(SCHEME, decision)["catalyst_label"] == "prolinol, toluene"


def test_string_false_is_never_treated_as_supported():
    review = normalize_reaction_review(
        {
            "connectivity_supported": "false",
            "product_supported": "false",
            "conditions_supported": "false",
            "supported": "false",
            "confidence": 99,
        }
    )
    assert review["connectivity_supported"] is False
    assert choose_reaction_presentation(review)["mode"] == "concept"


def test_r_groups_receive_stable_cross_reaction_maps():
    pytest.importorskip("rdkit")
    substrate, product, report = map_reaction_r_groups("*C#C.*C=O", "*C(O)=C=C*")
    assert report["status"] == "mapped"
    assert "[*:1]" in substrate
    assert "[*:1]" in product
    assert report["product_map_numbers"] == [1, 2]


def test_explicit_r_group_maps_are_preserved():
    pytest.importorskip("rdkit")
    substrate, product, report = map_reaction_r_groups(
        "[*:7]C#C", "[*:7]C=C=C[*:9]"
    )
    assert "[*:7]" in substrate
    assert "[*:7]" in product
    assert "[*:9]" in product
    assert report["status"] == "mapped"
def test_explicit_rejection_overrides_legacy_approval():
    from review_writer_core.stages.figures.overview_reaction import choose_reaction_presentation
    decision = choose_reaction_presentation({"supported": True, "product_supported": False, "confidence": 95})
    assert decision["mode"] == "concept"
