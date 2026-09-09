from review_writer_core.figure_qualification import (
    candidate_qualification,
    figure_output_state,
    figure_requirement,
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


def test_figure_needs_handle_empty_optional_and_structured_requests():
    for value in (None, "", [], {}, "none", "no"):
        assert figure_requirement(value) == "none"
    assert figure_requirement("None unless an overview clarifies the scope.") == "optional"
    assert figure_requirement([{"requirement": "optional", "purpose": "An overview"}]) == "optional"
    assert figure_requirement([{"requirement": "none", "purpose": "Legacy required text"}]) == "none"
    assert figure_requirement([{"requirement": "optional"}, {"requirement": "required"}]) == "required"
    assert figure_requirement([{"purpose": "Show representative source schemes"}]) == "required"


def test_empty_needs_do_not_remove_the_paper_candidate_pool(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "skills/review-section-drafting-figure-picking/scripts/select_initial_figure_candidates.py"
    spec = importlib.util.spec_from_file_location("test_candidate_selection", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = {
        "paper_figure_inventory.json": {"papers": [{"paper_id": "P001", "title": "Study",
                                                   "top_candidates": [_candidate()]}]},
        "section_tasks.json": [{"section_id": "S01", "allowed_papers": ["P001"], "figure_need": []}],
        "section_drafts.json": {"sections": [{"section_id": "S01", "paragraphs": [
            {"paragraph_id": "S01-p1", "paper_id": "P001", "text": "Study overview."}]}]},
    }
    monkeypatch.setattr(module, "read_json", lambda path: data[path.name])
    pool, manuscript = module.build_outputs(tmp_path)
    assert manuscript == []
    assert len(pool["papers"]) == 1
    assert len(pool["papers"][0]["candidates"]) == 1


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
