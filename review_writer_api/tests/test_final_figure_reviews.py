import json

from review_writer_api.domain_services.final import _figure_argument_findings
from review_writer_api.domain_services.final_figures import apply_figure_reviews


def fixture():
    metadata = {"figure_id": "F1", "published_label": "Figure 4", "output_artifact_id": "out",
                "paper_id": "P1", "source_label": "Scheme 2", "source_identity_status": "resolved",
                "interpretation_basis": "pending", "caption_quality": {"status": "pending"}}
    md = "Related prose (Figure 4).\n\n<!-- inserted_figure: " + json.dumps(metadata) + " -->\n"
    md += "![Old caption](/api/v1/artifacts/out/content)\n*Figure 4. Old caption. Adapted from Ref. 1.*\n\nNext paragraph."
    reviews = {"figures": {"F1": {"source_draft_id": "d", "source_manifest_id": "m", "output_artifact_id": "out",
                "caption": "Correct caption", "reviewed_at": "now", "reviewed_by": "user"}}}
    return md, reviews


def test_confirmation_clears_only_caption_checks_and_keeps_credit_and_prose():
    md, reviews = fixture()
    assert set(_figure_argument_findings(md)[0]["issues"]) == {"paper_level_interpretation_missing", "figure_caption_pending"}
    result = apply_figure_reviews(md, reviews, "d", "m")
    assert not _figure_argument_findings(result)
    assert "*Figure 4. Correct caption. Adapted from Ref. 1.*" in result
    assert result.startswith("Related prose (Figure 4).")
    assert result.endswith("Next paragraph.")
    source_missing = md.replace('"source_identity_status": "resolved"', '"source_identity_status": "unresolved"')
    assert "source_figure_identity_unresolved" in _figure_argument_findings(apply_figure_reviews(source_missing, reviews, "d", "m"))[0]["issues"]


def test_changed_draft_manifest_or_image_does_not_reuse_confirmation():
    md, reviews = fixture()
    assert apply_figure_reviews(md, reviews, "new-draft", "m") == md
    assert apply_figure_reviews(md, reviews, "d", "new-manifest") == md
    changed = md.replace('"output_artifact_id": "out"', '"output_artifact_id": "new"')
    assert apply_figure_reviews(changed, reviews, "d", "m") == changed
