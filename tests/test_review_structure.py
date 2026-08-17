from review_writer_core.review_structure import (
    assign_primary_paper_sections,
    infer_section_role,
)


def test_section_role_inference_is_topic_agnostic() -> None:
    assert infer_section_role("Introduction and scope") == "introduction"
    assert infer_section_role("研究背景与范围") == "introduction"
    assert infer_section_role("Clinical evidence by population") == "body"
    assert infer_section_role("Materials and device architectures") == "body"
    assert infer_section_role("Summary and outlook") == "conclusion"
    assert infer_section_role("参考文献") == "references"


def test_papers_have_one_primary_owner_and_bounded_synthesis_roles() -> None:
    sections, owners = assign_primary_paper_sections(
        [
            {"section_id": "S01", "title": "Introduction", "paper_ids": ["A"]},
            {"section_id": "S02", "title": "Theme one", "paper_ids": ["A", "B"]},
            {"section_id": "S03", "title": "Theme two", "paper_ids": ["A", "C"]},
            {"section_id": "S04", "title": "Conclusion", "paper_ids": ["A", "C"]},
        ],
        ["A", "B", "C"],
    )

    assert owners == {"A": "S02", "B": "S02", "C": "S03"}
    assert sections[0]["primary_papers"] == []
    assert sections[0]["supporting_papers"] == ["A"]
    assert sections[1]["primary_papers"] == ["A", "B"]
    assert sections[2]["primary_papers"] == ["C"]
    assert sections[2]["supporting_papers"] == ["A"]
    assert sections[3]["primary_papers"] == []
    assert sections[3]["supporting_papers"] == ["A", "C"]
