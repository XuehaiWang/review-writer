from types import SimpleNamespace

from review_writer_api.domain_services.library_index import _contribution_chunk_candidates
from review_writer_core.stages.sections.authoring import style_findings


def _chunk(ordinal, page, content, heading="Results"):
    return SimpleNamespace(
        ordinal=ordinal,
        page_start=page,
        content=content,
        normalized_content=content.casefold(),
        content_type="text",
        section_path_json=[heading],
    )


def test_primary_contribution_sample_prefers_own_body_over_introduction_and_references():
    chunks = [
        _chunk(0, 1, "Traditional allenoate methods were reported in earlier studies. " * 3, "Introduction"),
        _chunk(1, 2, "Electroreductive carboxylation of propargylic acetates with carbon dioxide furnished the target allenoate. " * 2),
        _chunk(2, 3, "The substrate scope included substituted propargylic acetates and the resulting allenoates. " * 2),
        _chunk(3, 4, "Electroreductive carboxylation of propargylic acetates is cited repeatedly. " * 3, "References"),
    ]
    chosen = _contribution_chunk_candidates(chunks, "Electroreductive carboxylation of propargylic acetates", 2)
    assert [chunk.page_start for chunk in chosen] == [2, 3]


def test_contribution_sample_does_not_treat_short_or_reference_text_as_result():
    chunks = [_chunk(0, 1, "Introduction."),
              _chunk(1, 2, "Reference citation to a different study. " * 3, "Bibliography")]
    assert _contribution_chunk_candidates(chunks, "A new result", 2) == []


def test_same_passage_repeated_experiment_is_an_advisory_not_a_rejection():
    quote = "The catalyst-controlled reaction of indoles and propargylic alcohols affords azepines under acid catalysis."
    claims = [
        {"claim_id": "C1", "paragraph_id": "p1", "claim":
         "With TsOH or B(C6F5)3, indoles react with propargylic alcohols to give azepines under the reported conditions.",
         "evidence_refs": [{"paper_id": "P1", "evidence_key": "k", "quote": quote}]},
        {"claim_id": "C2", "paragraph_id": "p1", "claim":
         "In the reported conditions, indoles react with propargylic alcohols to give azepines when TsOH or B(C6F5)3 is used.",
         "evidence_refs": [{"paper_id": "P1", "evidence_key": "k", "quote": quote}]},
    ]
    assert "possible_repeated_experiment_in_paragraph" in style_findings(claims)["C2"]
    claims[1]["evidence_refs"][0]["quote"] = "A different experiment examined substrate scope and failed examples."
    assert "possible_repeated_experiment_in_paragraph" not in style_findings(claims).get("C2", [])
