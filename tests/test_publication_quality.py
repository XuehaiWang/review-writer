import pytest

from review_writer_core.chemical_typography import normalize_chemical_typography
from review_writer_core.draft_bibliography import (
    citation_entries_from_draft, citation_map_comment, repair_numbered_references,
)
from review_writer_core.publication_tables import render_section_comparison, split_table_row
from review_writer_core.manuscript_state import build_manuscript_state
from review_writer_core.latex_renderer import latex_escape


def test_pdf_preserves_terminal_alkyne_math_without_allowing_tex_file_commands():
    formula = r"$\mathrm{RC} \equiv \mathrm{CH}$"
    assert latex_escape(formula) == formula
    assert latex_escape(r"\(\mathrm{RC} \equiv \mathrm{CH}\)") == formula
    unsafe = latex_escape(r"$\mathrm{RC} \equiv \input{secret}$")
    assert r"\textbackslash{}input" in unsafe
    assert r"\input{secret}" not in unsafe
from review_writer_api.domain_services.drafts import DraftsService
from review_writer_api.domain_services.final import FinalService, _normalize_publication_markup


@pytest.mark.parametrize(("plain", "expected"), [
    ("ZnI2 C12H22O11 Al2(SO4)3 Pd(PPh3)4", "ZnI₂ C₁₂H₂₂O₁₁ Al₂(SO₄)₃ Pd(PPh₃)₄"),
    ("CuI/ZnBr2/Ti(OEt)4", "CuI/ZnBr₂/Ti(OEt)₄"),
    ("Pd(OAc)2/PPh3 and CuBr2/ZnCl2", "Pd(OAc)₂/PPh₃ and CuBr₂/ZnCl₂"),
    ("CuI / ZnBr2 / Ti(OEt)4", "CuI / ZnBr₂ / Ti(OEt)₄"),
    ("P001/S02 4aa/4ab 93/99 CuI/ZnBr2/data CuI/ZnBr2.pdf ./ZnBr2", "P001/S02 4aa/4ab 93/99 CuI/ZnBr2/data CuI/ZnBr2.pdf ./ZnBr2"),
    ("CuSO4·5H2O", "CuSO₄·5H₂O"),
    ("Pd2(dba)3 and C60", "Pd₂(dba)₃ and C₆₀"),
    ("(ZnI2), [Co(NH3)6]Cl3", "(ZnI₂), [Co(NH₃)₆]Cl₃"),
    ("Fe3+ NH4+ O2+ [Fe(CN)6]3-", "Fe³⁺ NH₄⁺ O₂⁺ [Fe(CN)₆]³⁻"),
    ("CH3- and MeO-substituted substrates", "CH₃- and MeO-substituted substrates"),
    ("CH3- or MeO-substituted substrates", "CH₃- or MeO-substituted substrates"),
    ("CH3- and NH4+ ions", "CH₃- and NH₄⁺ ions"),
    ("CH3-, MeO-, and Cl-substituted substrates", "CH₃-, MeO-, and Cl-substituted substrates"),
    ("Cl- und CH3-substituted", "Cl- und CH₃-substituted"),
    ("CH3^- and [OH]- ions", "CH₃⁻ and [OH]⁻ ions"),
    ("加入ZnI2催化", "加入ZnI₂催化"),
    ("P001 A12 B12 13C SO42-", "P001 A12 B12 13C SO42-"),
    ("`ZnI2` $ZnI_2$ https://example.com/ZnI2 10.1000/ZnI2", "`ZnI2` $ZnI_2$ https://example.com/ZnI2 10.1000/ZnI2"),
    ("<!-- evidence: ZnI2 > 2 --> [paper](./ZnI2.pdf)", "<!-- evidence: ZnI2 > 2 --> [paper](./ZnI2.pdf)"),
])
def test_shared_chemical_typography(plain, expected):
    assert normalize_chemical_typography(plain) == expected
    assert normalize_chemical_typography(expected) == expected


def test_export_boundaries_share_chemical_typography():
    assert _normalize_publication_markup("ZnI2 and C12H22O11") == "ZnI₂ and C₁₂H₂₂O₁₁"
    assert r"ZnI\ensuremath{_{2}}" in latex_escape("ZnI2")
    assert "P001" in latex_escape("P001")
    formula = "CuI/ZnBr2/Ti(OEt)4"
    assert _normalize_publication_markup(formula) == "CuI/ZnBr₂/Ti(OEt)₄"
    assert latex_escape(formula) == r"CuI/ZnBr\ensuremath{_{2}}/Ti(OEt)\ensuremath{_{4}}"


def _claim(text, papers, fact_ids=None):
    claim = {"text": text, "citation_group": papers}
    if fact_ids is not None:
        claim["fact_ids"] = fact_ids
    return claim


def _index(claims, text=""):
    return {"sections": [{"section_id": "S1", "heading": "Systems", "section_role": "body", "paragraphs": [
        {"paragraph_id": "S1-p1", "text": text or " ".join(c["text"] for c in claims),
         "cited_paper_ids": list(dict.fromkeys(p for c in claims for p in c["citation_group"])), "claim_realizations": claims},
    ]}]}


MATRIX = {"rows": [{"paper_id": "P1", "title": "First"}, {"paper_id": "P2", "title": "Second"}]}


def test_repair_restores_claim_locations_and_sorts_repeated_groups():
    claims = [_claim("A uses ZnI2.", ["P1"]), _claim("B uses water.", ["P2"]), _claim("Both are reported.", ["P2", "P1"])]
    index = _index(claims)
    markdown = "A uses ZnI2. B uses water. Both are reported. [16, 2]\n\n<!-- paragraph_id: S1-p1 -->"
    repaired, report = repair_numbered_references(markdown, citation_entries_from_draft(markdown, index), MATRIX)
    assert "A uses ZnI₂. [1] B uses water. [2] Both are reported. [1, 2]" in repaired
    assert report["status"] == "applied"
    identity = citation_entries_from_draft(repaired, index)
    assert identity["entries"] == [{"callout": 1, "paper_id": "P1"}, {"callout": 2, "paper_id": "P2"}]
    assert repair_numbered_references(repaired, identity, MATRIX)[0] == repaired


def test_polished_prose_is_never_replaced_by_original_claims():
    index = _index([_claim("Original A.", ["P1"]), _claim("Original B.", ["P2"])])
    markdown = "Rewritten A [8]. Rewritten B [16]. Both [16, 8].\n\n<!-- paragraph_id: S1-p1 -->\n\n" + citation_map_comment({"P1": 8, "P2": 16})
    repaired, report = repair_numbered_references(markdown, citation_entries_from_draft(markdown, index), MATRIX)
    assert "Rewritten A [1]. Rewritten B [2]. Both [1, 2]." in repaired
    assert "Original" not in repaired
    assert report["paragraph_ids_needing_revalidation"] == ["S1-p1"]


def test_additional_prose_keeps_its_local_citation():
    index = _index([_claim("Original A.", ["P1"])])
    markdown = "Original A. [8] Additional supported result [16].\n\n<!-- paragraph_id: S1-p1 -->\n\n" + citation_map_comment({"P1": 8, "P2": 16})
    repaired, _ = repair_numbered_references(markdown, citation_entries_from_draft(markdown, index), MATRIX)
    assert "Original A. [1] Additional supported result [2]." in repaired


def test_ambiguous_legacy_identity_is_not_silently_assigned():
    markdown = "A [1].\n\n<!-- paragraph_id: a -->\n\nB [1].\n\n<!-- paragraph_id: b -->"
    index = {"sections": [{"paragraphs": [
        {"paragraph_id": "a", "cited_paper_ids": ["P1"]},
        {"paragraph_id": "b", "cited_paper_ids": ["P2"]},
    ]}]}
    repaired, report = repair_numbered_references(markdown, citation_entries_from_draft(markdown, index), MATRIX)
    assert repaired == markdown and report["status"] == "not_applied"


def test_final_numbering_sorts_groups_without_moving_them():
    body, ledger = FinalService._render_final_citation_numbers("A [8]. B [16]. Both [16, 8].", {
        "complete": True, "entries": [{"callout": 8, "paper_id": "P1"}, {"callout": 16, "paper_id": "P2"}],
    })
    assert "A [1]. B [2]. Both [1, 2]." in body
    assert ledger["complete"]


def _fact(paper, field, value, experiment="E1", **extra):
    return {"paper_id": paper, "fact_id": f"{paper}-{field}-{experiment}", "field_id": field, "value": value,
            "experiment_id": experiment, "subject": "System " + experiment,
            "evidence_refs": [{"evidence_key": paper + "-source"}], **extra}


def _comparison_fixture():
    claims = [_claim("A result.", ["P1"]), _claim("B result.", ["P2"])]
    index = _index(claims)
    rows = {p: {"paper_id": p, "title": p, "scientific_facts": [
        _fact(p, "object_input", "Substrate A | B"),
        _fact(p, "method_conditions", "ZnI2", qualifiers={"temperature": "25 °C"}),
        _fact(p, "quantitative_results", "85% yield"),
        _fact(p, "mechanism", "Intermediate proposed", assertion_ceiling="attributed_author_interpretation"),
    ]} for p in ["P1", "P2"]}
    rows["P1"]["scientific_facts"].append(_fact("P1", "quantitative_results", "40% yield", "E2", qualifiers={"temperature": "80 °C"}))
    rows["P2"]["scientific_facts"].append(_fact("P2", "scope", "UNVERIFIED SCOPE", verification={"status": "rejected"}))
    return index, rows


def test_comparison_preserves_experiments_citations_and_evidence_limits():
    index, rows = _comparison_fixture()
    markdown = DraftsService._assemble_markdown("Review", index, {}, {"rows": list(rows.values())})
    assert "Table 1." in markdown
    assert "ZnI₂; T 25 °C" in markdown
    assert "Author interpretation: Intermediate proposed" in markdown
    assert "UNVERIFIED SCOPE" not in markdown
    # A sparse second experiment is not dumped into the publication table.
    assert "40% yield" not in markdown
    table_lines = [line for line in markdown.splitlines() if line.startswith("|")]
    assert all(len(split_table_row(line)) == 5 for line in table_lines)
    assert table_lines[2].count("[1]") == 1
    assert table_lines[3].count("[2]") == 1
    identity = citation_entries_from_draft(markdown, index)
    repaired, report = repair_numbered_references(markdown, identity, {"rows": list(rows.values())})
    assert report["status"] == "applied"
    assert [line for line in repaired.splitlines() if line.startswith("|")] == table_lines
    state = build_manuscript_state(markdown)
    tables = [block for block in state["blocks"] if block["kind"] == "table"]
    assert len(tables) == 1 and "Table 1" in tables[0]["caption"]
    assert "Substrate A | B" in str(tables[0]["rows"])


def test_no_table_for_single_source_or_unusable_facts():
    index, rows = _comparison_fixture()
    section = index["sections"][0]
    assert render_section_comparison(section, rows, {"P1": 1}, table_number=1) == ""
    assert render_section_comparison(section, {}, {"P1": 1, "P2": 2}, table_number=1) == ""
    assert render_section_comparison({**section, "section_role": "introduction"}, rows, {"P1": 1, "P2": 2}, table_number=1) == ""


def test_table_preserves_zero_conditions_math_and_source_local_labels():
    index, rows = _comparison_fixture()
    fact = rows["P1"]["scientific_facts"][1]
    fact["value"] = r"Compound [16] with $\mathrm{ZnI}_{2}$"
    fact["qualifiers"] = {"temperature": 0}
    table = render_section_comparison(index["sections"][0], rows, {"P1": 1, "P2": 2}, table_number=1)
    assert r"Compound ［16］ with $\mathrm{ZnI}_{2}$; T 0" in table


def test_publication_table_uses_only_facts_bound_to_current_claims():
    index, rows = _comparison_fixture()
    paragraph = index["sections"][0]["paragraphs"][0]
    paragraph["claim_realizations"] = [
        _claim(
            "A comparison.",
            ["P1"],
            ["P1-object_input-E1", "P1-quantitative_results-E1"],
        ),
        _claim(
            "B comparison.",
            ["P2"],
            ["P2-object_input-E1", "P2-quantitative_results-E1"],
        ),
    ]

    table = render_section_comparison(
        index["sections"][0], rows, {"P1": 1, "P2": 2}, table_number=1
    )

    assert "System / substrate" in table
    assert "Key result" in table
    assert "Conditions" not in table
    assert "ZnI" not in table
    assert "P1-method_conditions-E1" not in table.split('"selected_fact_ids":', 1)[1].split(']', 1)[0]


def test_publication_table_prefers_structured_qualifiers_over_long_prose():
    index, rows = _comparison_fixture()
    for paper in ("P1", "P2"):
        fact = rows[paper]["scientific_facts"][1]
        fact["value"] = (
            "The study reports that the optimized experiment used a long procedural "
            "description that should not be copied verbatim into a reader-facing table."
        )
        fact["qualifiers"] = {
            "catalyst_loading": "20 mol%",
            "solvent": "toluene",
            "temperature": "130 °C",
            "time": "12 h",
        }

    table = render_section_comparison(
        index["sections"][0], rows, {"P1": 1, "P2": 2}, table_number=1
    )

    assert "long procedural description" not in table
    assert "catalyst: 20 mol%; toluene; T 130 °C; time 12 h" in table


def test_result_qualifiers_do_not_false_match_short_metric_names():
    index, rows = _comparison_fixture()
    for paper in ("P1", "P2"):
        fact = rows[paper]["scientific_facts"][2]
        fact["qualifiers"] = {
            "aldehyde": "1.8 equiv",
            "screen_reference": "Table S3",
        }

    table = render_section_comparison(
        index["sections"][0], rows, {"P1": 1, "P2": 2}, table_number=1
    )

    assert "85% yield" in table
    assert "aldehyde: 1.8 equiv" not in table


def test_publication_table_uses_one_representative_row_per_paper():
    index, rows = _comparison_fixture()
    table = render_section_comparison(
        index["sections"][0], rows, {"P1": 1, "P2": 2}, table_number=1
    )
    data_rows = [line for line in table.splitlines() if line.startswith("|")][2:]

    assert len(data_rows) == 2
    assert sum("[1]" in line for line in data_rows) == 1
    assert sum("[2]" in line for line in data_rows) == 1


def _source_claim(paper, *, records=None, text=None):
    from review_writer_core.stages.sections.source_writing import CONTRACT, support_fingerprint
    text = text or f"The {paper} system affords the product under the reported conditions."
    refs = [{"paper_id": paper, "evidence_key": paper + "-source", "quote": text}]
    claim = {"claim_id": paper + "-C1", "text": text, "citation_group": [paper],
             "evidence_refs": refs, "fact_ids": [], "claim_kind": "source_report",
             "result_context": records or []}
    claim["source_verification"] = {"contract": CONTRACT, "status": "supported",
        "input_fingerprint": support_fingerprint(text, refs, claim["claim_kind"], claim["result_context"])}
    return claim


def test_source_tables_assemble_without_fact_cards_and_export_as_table():
    claims = [_source_claim(p, records=[{"evidence_key": p + "-source", "object": "Substrate " + p,
              "conditions": "25 °C", "result": "85% yield", "units": "%"}]) for p in ("P1", "P2")]
    markdown = DraftsService._assemble_markdown("Review", _index(claims), {}, MATRIX)
    assert "Table 1." in markdown and "85% yield" in markdown and "25 °C" in markdown
    assert '"evidence_mode":"source_claims"' in markdown
    assert '"claim_id":"P1-C1"' in markdown
    assert any(block["kind"] == "table" for block in build_manuscript_state(markdown)["blocks"])


def test_refresh_saved_tables_preserves_prose_and_manual_tables():
    from review_writer_core.publication_tables import refresh_generated_comparison_tables
    claims = [_source_claim(p, records=[{"evidence_key": p + "-source", "object": "Substrate " + p,
              "conditions": "25 °C", "result": "85% yield", "units": "%"}]) for p in ("P1", "P2")]
    index = _index(claims)
    old = ('Table 4. Old summary.\n\n| System | Finding |\n| --- | --- |\n'
           '| Reported finding | Old summary [8] |\n\n'
           '<!-- comparison_table: {"section_id":"S1","projection":"publication-comparison/3"} -->')
    prefix = '## Section\n\nOptimized prose [8].\n\n![Figure](asset.svg)\n\n'
    suffix = ('\n\nTable 1. Manual data.\n\n| Value |\n| --- |\n| 7 |\n\n'
              + citation_map_comment({"P1": 8, "P2": 16}) + '\n\n## References\n\n[8] First.\n')
    result = refresh_generated_comparison_tables(prefix + old + suffix, index, MATRIX['rows'])
    expected = render_section_comparison(index['sections'][0], {}, {"P1": 8, "P2": 16}, table_number=2)
    assert result == prefix + expected + suffix
    assert refresh_generated_comparison_tables(result, index, MATRIX['rows']) == result
    assert refresh_generated_comparison_tables(prefix + old + suffix, {}, []) == prefix + old + suffix
    assert refresh_generated_comparison_tables(prefix + old, index, MATRIX['rows']) == prefix + old
    empty = _index([_source_claim('P1'), _source_claim('P2')])
    assert refresh_generated_comparison_tables(prefix + old + suffix, empty, []) == prefix + suffix


def test_source_tables_do_not_turn_unstructured_prose_into_comparison():
    claims = [_source_claim("P1", text="A longer account of the conditions was reported, but only for a restricted substrate scope."),
              _source_claim("P1", text="A route was proposed, not demonstrated."), _source_claim("P2")]
    table = render_section_comparison(_index(claims)["sections"][0], {}, {"P1": 1, "P2": 2}, table_number=1)
    assert table == ""



@pytest.mark.parametrize("changed", ["text", "result_context", "citation_group", "source_verification"])
def test_stale_source_audit_cannot_revive_legacy_table(changed):
    claims = [_source_claim("P1"), _source_claim("P2")]
    if changed == "text":
        claims[0][changed] = "Changed scientific assertion."
    elif changed == "result_context":
        claims[0][changed] = [{"evidence_key": "P1-source", "result": "100%"}]
    elif changed == "citation_group":
        claims[0][changed] = ["P2"]
    else:
        claims[0][changed]["status"] = "rejected"
    _, rows = _comparison_fixture()
    assert render_section_comparison(_index(claims)["sections"][0], rows, {"P1": 1, "P2": 2}, table_number=1) == ""


def test_source_table_does_not_merge_separate_experiments():
    claims = [_source_claim(p, records=[
        {"evidence_key": p + "-source", "object": "System A", "conditions": "25 °C"},
        {"evidence_key": p + "-source", "object": "System B", "result": "90% yield"},
    ]) for p in ("P1", "P2")]
    table = render_section_comparison(_index(claims)["sections"][0], {}, {"P1": 1, "P2": 2}, table_number=1)
    for row in table.splitlines():
        assert not (row.startswith("|") and "25 °C" in row and "90% yield" in row)
    assert "Table 1." in table


def test_comparison_has_separate_references_and_no_background_rows_or_duplicate_units():
    claims = [_source_claim(p, records=[{"evidence_key": p + "-source", "object": "System " + p,
        "conditions": "conditions A; entry 12, Table 2", "result": "50% yield with 93% ee", "units": "50% yield"}]) for p in ("P1", "P2", "P3")]
    section = {**_index(claims)["sections"][0], "primary_papers": ["P1", "P2"]}
    table = render_section_comparison(section, {}, {"P1": 1, "P2": 2, "P3": 3}, table_number=1)
    visible = table.split("<!--", 1)[0]
    assert "| System / substrate | Key result | Ref. |" in visible
    assert "P3" not in visible and "[3]" not in visible
    assert "conditions A" not in visible and "Table 2" not in visible
    assert "50% yield with 93% ee; 50% yield" not in visible
    assert "Reported finding" not in visible and "Reported system" not in visible
    assert '"comparison_question":' in table
    assert "50% yield with 93% ee | [1]" in visible

def test_numeric_units_preserve_metrics_missing_from_result():
    from review_writer_core.publication_tables import _result_with_units
    assert _result_with_units("highly enantioselective synthesis", "93–99% ee") == "highly enantioselective synthesis; 93–99% ee"
    assert _result_with_units("a pair of diastereoisomers", "d.r. = 1:1") == "a pair of diastereoisomers; d.r. = 1:1"
    assert _result_with_units("93–99% ee", "% ee") == "93–99% ee"
