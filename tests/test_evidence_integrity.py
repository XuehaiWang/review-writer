from __future__ import annotations

from review_writer_core.evidence_integrity import (
    normalized_anchor_text,
    normalized_scalar_value,
    source_contains_excerpt,
    source_span_view,
    unsupported_realization_anchors,
)


def test_source_excerpt_accepts_only_presentation_level_tex_and_dash_variants() -> None:
    source = r"The products 4ua-4xa were obtained at $25\,\mathrm { C }$."
    excerpt = r"products 4ua–4xa were obtained at $25\,\mathrm { C}$"
    assert source_contains_excerpt(source, excerpt)
    assert not source_contains_excerpt(source, "products 4ua-4xa were obtained at 80 C")


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


def test_source_span_is_a_view_over_existing_evidence_identity() -> None:
    span = source_span_view(
        {
            "evidence_key": "sha256:abc",
            "chunk_id": "chunk-1",
            "page_start": 4,
            "page_end": 4,
            "source_lineage_hash": "lineage",
        },
        source={
            "content_type": "text",
            "content": "The experiment was conducted at 80 °C.",
            "section_path": ["Experimental"],
        },
        paper_id="P001",
    )
    assert span["paper_id"] == "P001"
    assert span["evidence_key"] == "sha256:abc"
    assert span["source_type"] == "body"
    assert span["verbatim_text_sha256"]
    assert "source_span_id" not in span


def test_scalar_value_normalizes_only_unambiguous_number_and_unit() -> None:
    assert normalized_scalar_value("80 °C") == (80, "°C")
    assert normalized_scalar_value("reported at 80 °C") == ("reported at 80 °C", "")
    assert unsupported_realization_anchors(
        "The allene was obtained in 42% yield.",
        ["The yield of the allene dropped to 42 % under these conditions."],
    )["quantitative"] == []
