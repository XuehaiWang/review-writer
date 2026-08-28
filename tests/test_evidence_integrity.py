from __future__ import annotations

from review_writer_core.evidence_integrity import (
    normalized_anchor_text,
    unsupported_realization_anchors,
)


def test_mineru_latex_formula_matches_plain_formula() -> None:
    assert normalized_anchor_text(r"$\mathrm{Ti}(\mathrm{OEt})_{4}$") == "ti(oet)4"
    assert unsupported_realization_anchors(
        "Ti(OEt)4 was used.",
        [r"The reaction used $\mathrm{Ti}(\mathrm{OEt})_{4}$."]
    ) == {"quantitative": [], "technical_entities": []}


def test_mineru_subscript_formula_and_role_alias_are_supported() -> None:
    result = unsupported_realization_anchors(
        "The CdI2-mediated transformation was reported.",
        [r"The transformation used $\mathrm{CdI}_{2}$."],
        domain_terms=["CdI2-mediated"],
    )
    assert result == {"quantitative": [], "technical_entities": []}


def test_unsupported_quantitative_value_is_still_rejected() -> None:
    result = unsupported_realization_anchors(
        "The product was obtained in 97% yield.",
        ["The product was obtained in 81% yield."],
    )
    assert "97%" in result["quantitative"]
    assert "81%" not in result["quantitative"]


def test_metric_value_order_does_not_create_a_false_rejection() -> None:
    assert unsupported_realization_anchors(
        "The products were obtained with 94-99% de.",
        ["The products were obtained with very high de (94-99%)."],
    )["quantitative"] == []
    assert unsupported_realization_anchors(
        "The allene was obtained in 42% yield.",
        ["The yield of the allene dropped to 42 % under these conditions."],
    )["quantitative"] == []
