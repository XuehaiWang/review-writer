from review_writer_core.figure_qualification import (
    candidate_qualification,
    figure_output_state,
)


def _candidate(**extra):
    row = {
        "paper_id": "P001",
        "source_label": "Scheme 1",
        "source_caption_text": "Representative reaction scheme",
        "source_type": "image",
        "source_image_path": "paper/image.png",
        "inventory_score": 2,
    }
    row.update(extra)
    return row


def test_hard_exclusion_cannot_be_overridden_by_a_high_score() -> None:
    result = candidate_qualification(
        _candidate(
            source_label="Table 1",
            source_type="table",
            inventory_score=999,
        )
    )
    assert result["eligible"] is False
    assert "table_or_optimization_screenshot" in result["reasons"]


def test_resolved_scientific_scheme_passes_minimum_candidate_gate() -> None:
    result = candidate_qualification(_candidate())
    assert result["eligible"] is True
    assert result["score"] >= result["minimum_score"]


def test_output_state_distinguishes_source_ai_and_manual_results() -> None:
    assert (
        figure_output_state(
            {
                "source_preserved": True,
                "output_artifact_id": "source",
                "source_artifact_id": "source",
            }
        )
        == "source_original"
    )
    assert (
        figure_output_state(
            {"ai_redraw_performed": True, "output_artifact_id": "redrawn"}
        )
        == "ai_redrawn"
    )
    assert (
        figure_output_state(
            {
                "render_mode": "manual-arrow-edit",
                "output_artifact_id": "manual",
                "human_approval": {"status": "approved"},
            }
        )
        == "approved_manually_edited"
    )

