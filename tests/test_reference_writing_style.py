from review_writer_core.reference_writing_style import normalize_writing_style, reference_style_prompt


def test_projection_is_optional_bounded_and_excludes_source_content():
    # The section-drafting skill still projects a blueprint's style into its
    # rules; an absent or unsafe style must stay empty rather than leak content.
    assert reference_style_prompt(None) == ""
    assert normalize_writing_style({"forbidden_content_check": {"contains_source_topic_content": True},
                                    "organization_pattern": "unsafe"}) == {}
    style = normalize_writing_style({"organization_pattern": "x" * 2000,
        "source_text": "Do not transmit the review", "conclusion_conventions": "conclusion",
        "paragraph_conventions": {"opening_move": "State the problem.", "typical_length": "500"}})
    assert len(style["organization_pattern"]) == 1200
    assert style["paragraph_conventions"] == {"opening_move": "State the problem."}
    assert "source_text" not in style and "conclusion_conventions" not in style
