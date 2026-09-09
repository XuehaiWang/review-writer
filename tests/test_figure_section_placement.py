import pytest

from review_writer_api.domain_services.figures import FiguresService


@pytest.mark.parametrize("role,expected", [
    ("mechanism_model", "body-p1"),
    ("conceptual_overview", "intro-p1"),
])
def test_study_figure_prefers_citing_body_but_overview_can_open_review(monkeypatch, role, expected):
    service = object.__new__(FiguresService)
    sections = [
        {"section_id": "S01", "section_role": "introduction", "heading": "Introduction overview",
         "paragraphs": [{"paragraph_id": "intro-p1", "cited_paper_ids": ["A"],
                         "text": "Conceptual strategy, mechanism pathway and intermediate."}]},
        {"section_id": "S02", "section_role": "body", "heading": "Catalysis",
         "paragraphs": [{"paragraph_id": "body-p1", "cited_paper_ids": ["A"],
                         "text": "The catalytic cycle is discussed here."}]},
    ]
    monkeypatch.setattr(service, "_read_json", lambda *args: ({"sections": sections}, None))
    candidate = {"paper_id": "A", "representative_role": role, "target_paragraph_id": "intro-p1"}
    result = service._derive_current_placement(None, "project", candidate)
    assert result["target_paragraph_id"] == expected
    assert result["source_target_paragraph_id"] == "intro-p1"
    sections[1]["paragraphs"][0]["cited_paper_ids"] = ["B"]
    assert service._derive_current_placement(None, "project", candidate)["target_paragraph_id"] == "intro-p1"
