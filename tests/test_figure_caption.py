import json
from copy import deepcopy

import pytest

from review_writer_core.figure_caption import caption_fields, enrich_selected_captions, recover_caption
from review_writer_core.publication_caption import normalize_publication_caption
from review_writer_api.domain_services.final import _figure_argument_findings


def row():
    return {"paper_id": "P001", "source_image_sha256": "image1", "source_label": "Scheme 2",
            "source_caption_text": "Scheme 2. Proposed copper-catalyzed pathway for allene synthesis."}


def test_short_source_is_specific_and_needs_no_model():
    figure = row()
    enrich_selected_captions([figure], call=lambda _: pytest.fail("Unnecessary model request"))
    assert figure["publication_caption_text"] == "Proposed copper-catalyzed pathway for allene synthesis"
    assert figure["source_caption_text"] == row()["source_caption_text"]


@pytest.mark.parametrize("text", ["", "Figure candidate 4", "Representative figure from the cited source study."])
def test_no_title_or_role_inference_when_caption_is_missing(text):
    fields = caption_fields({"source_caption_text": text, "title": "Excellent allene synthesis",
                            "what_it_shows": "Invented results", "representative_role": "core_transformation"})
    assert not fields["publication_caption_text"]
    assert fields["caption_quality"]["status"] == "pending"


def test_long_source_keeps_complete_opening_sentence():
    source = row()["source_caption_text"] + " Experimental details " * 120
    assert normalize_publication_caption(source).publication_text == "Proposed copper-catalyzed pathway for allene synthesis"


def test_recover_split_caption_only_from_same_page_and_specific_label():
    blocks = [{"type": "image", "img_path": "images/a.png", "page_idx": 2},
              {"type": "text", "text": "Scheme 2. Copper-catalyzed allene synthesis.", "page_idx": 2}]
    recovered = recover_caption(blocks, 0)
    fields = caption_fields({**row(), "source_caption_text": "", **recovered})
    assert fields["caption_provenance"]["method"] == "source_figure_context"
    assert "Copper-catalyzed" in fields["publication_caption_text"]
    blocks[1]["page_idx"] = 3
    assert recover_caption(blocks, 0) == {}


def test_recover_markdown_requires_unique_matching_image():
    blocks = [{"type": "image", "img_path": "images/a.png", "page_idx": 1}]
    md = "![x](images/a.png)\n\nScheme 2. Copper-catalyzed allene synthesis."
    assert recover_caption(blocks, 0, markdown=md)["caption_source_ref"]["method"] == "markdown_image_caption"
    assert recover_caption(blocks, 0, markdown=md.replace("a.png", "b.png")) == {}
    assert recover_caption(blocks, 0, markdown=md + "\n" + md) == {}


def test_batch_compression_cache_tracks_source_and_image():
    source = "Proposed copper-catalyzed pathway for allene synthesis with " + "reported intermediate and ligand details " * 18
    figure = {**row(), "source_caption_text": source}
    cache, calls = {}, []
    def model(prompt):
        calls.append(prompt)
        request = json.loads(prompt.split("\n", 1)[1])
        return {"captions": [{"key": r["key"], "text": "Proposed copper-catalyzed pathway for allene synthesis.",
                              "support_quote": r["source"]} for r in request]}
    enrich_selected_captions([figure], call=model, cache=cache)
    assert figure["publication_caption_text"]
    cached = {**row(), "source_caption_text": source}
    enrich_selected_captions([cached], call=model, cache=cache)
    assert len(calls) == 1
    # A changed image cannot keep a caption generated for an earlier asset.
    cached["source_image_sha256"] = "image2"
    assert not caption_fields(cached)["publication_caption_text"]
    enrich_selected_captions([cached], call=model, cache=cache)
    assert len(calls) == 2


@pytest.mark.parametrize("bad", ["Invented yield of 99%.", "Representative figure from the cited source study."])
def test_bad_generated_caption_stays_pending(bad):
    figure = {**row(), "source_caption_text": "Copper-catalyzed allene synthesis " * 70}
    def model(prompt):
        request = json.loads(prompt.split("\n", 1)[1])[0]
        return {"captions": [{"key": request["key"], "text": bad, "support_quote": request["source"]}]}
    enrich_selected_captions([figure], call=model)
    assert not figure["publication_caption_text"]


def test_provider_failure_is_nonblocking():
    figure = {**row(), "source_caption_text": "Copper-catalyzed allene synthesis " * 70}
    def fail(_):
        raise TimeoutError()
    enrich_selected_captions([figure], call=fail)
    assert figure["caption_quality"]["status"] == "pending"


def figure_markup(prose, *, basis="source_figure_context"):
    metadata = {"figure_id": "P001-F01", "paper_id": "P001", "published_label": "Figure 3",
                "interpretation_basis": basis, "output_artifact_id": "image", "source_label": "Scheme 2"}
    return f'{prose}\n<!-- inserted_figure: {json.dumps(metadata)} -->\n![Figure 3](/api/v1/artifacts/image/content)\n*Figure 3. Copper-catalyzed allene synthesis.*'


def test_natural_callout_passes_without_prescribed_verb():
    assert _figure_argument_findings(figure_markup("The source reports this transformation (Figure 3).")) == []
    assert _figure_argument_findings(figure_markup("Figure 3 provides the reported substrate scope.")) == []


def test_image_caption_alone_is_not_a_prose_callout():
    findings = _figure_argument_findings(figure_markup("The source reports this transformation."))
    assert "visible_callout_or_interpretation_missing" in findings[0]["issues"]
    findings = _figure_argument_findings(figure_markup("See Figure 30."))
    assert "visible_callout_or_interpretation_missing" in findings[0]["issues"]
