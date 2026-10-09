from __future__ import annotations

from review_writer_core.evidence_integrity import (
    normalized_anchor_text,
    normalized_scalar_value,
    source_contains_excerpt,
    source_span_view,
    unsupported_realization_anchors,
)


def test_table_percent_is_inherited_only_from_the_corresponding_column():
    source = (r'<table><tr><td>entry</td><td>cat.</td><td>yield of 3aa(%)/time (h)</td></tr>'
              r'<tr><td>99</td><td>CdI2</td><td>$36^b/2.6$</td></tr>'
              r'<tr><td>2</td><td>ZnI2</td><td>$38^b/2.6$</td></tr>'
              r'<tr><td>3</td><td>MgI2</td><td>0/20</td></tr></table>')
    assert unsupported_realization_anchors("36% yield, 38%, 0%", [source])["quantitative"] == []
    for wrong in ("99%", "2.6%", "20%", "36% ee"):
        assert unsupported_realization_anchors(wrong, [source])["quantitative"]
    assert not source_contains_excerpt(source, "36% yield")


def test_table_parenthetical_ee_does_not_replace_yield_header():
    source = (r'<table><tr><td>amine</td><td>yield of 3 (\%)b,c</td></tr>'
              r'<tr><td>4</td><td>37 (3aa, 34% ee)d</td></tr></table>')
    assert unsupported_realization_anchors("37% yield, 34% ee", [source])["quantitative"] == []
    assert unsupported_realization_anchors("4%", [source])["quantitative"]


def test_truncated_table_preserves_closed_cells_but_not_cut_off_values():
    header = '<table><tr><td>yield (%)</td><td>time (h)</td></tr>'
    assert unsupported_realization_anchors('36% yield', [header + '<tr><td>36</td><td>2'])["quantitative"] == []
    assert unsupported_realization_anchors('36%', [header + '<tr><td>36'])["quantitative"]
    mixed = '<table><tr><td>yield/ee (%)</td></tr><tr><td>36/99</td></tr></table>'
    assert unsupported_realization_anchors('36% ee', [mixed])["quantitative"]


def test_percent_not_invented_for_unheaded_ambiguous_or_other_source_table():
    rows = '<tr><td>36</td></tr>'
    for sources in (
        [f'<table>{rows}</table>'],
        ['yield (%)', f'<table>{rows}</table>'],
        [f'<table><tr><td colspan="2">yield (%)</td></tr>{rows}</table>'],
        ['yield (%) 36'],
    ):
        assert unsupported_realization_anchors("36%", sources)["quantitative"]


def test_dotted_ratio_metric_matches_without_reinterpreting_corrupt_fraction():
    source = 'a pair of diastereoisomers (d.r. ¼ 1:1) in 69%'
    assert unsupported_realization_anchors('69% yield, dr 1:1', [source])["quantitative"] == ['69% yield']
    assert unsupported_realization_anchors('dr 2:1', [source])["quantitative"]
    assert not source_contains_excerpt(source, 'd.r. = 1:1')
    assert unsupported_realization_anchors('CdI2, 87% yield', ['87% yield'])["technical_entities"] == ['CdI2']


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


def test_split_math_scalar_with_tex_nonbreaking_space_matches_temperature() -> None:
    source = r"Stirred at $2 5 ~ ^ { \circ } \mathrm { C } .$ under argon."
    assert unsupported_realization_anchors("At 25 °C under argon.", [source])["quantitative"] == []
    assert unsupported_realization_anchors("At 250 °C.", [source])["quantitative"]
    assert unsupported_realization_anchors("At 80 °C.", [source])["quantitative"]
    assert source_contains_excerpt(source, r"$25 ^{\circ} C.$")


def test_tex_space_scalar_normalization_does_not_guess_lists_or_operators() -> None:
    assert not source_contains_excerpt("Measured 2 5 °C", "Measured 25 °C")
    assert not source_contains_excerpt(r"$2, 5 ~ ^{\circ} C$", r"$25 ^{\circ} C$")
    assert not source_contains_excerpt(r"$2 5 \sim ^{\circ} C$", r"$25 ^{\circ} C$")


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
