# -*- coding: utf-8 -*-
"""Heading extraction and import for an uploaded reference review."""
import io
import zipfile

from review_writer_core.reference_outline import (
    Heading,
    OutlineSection,
    extract_outline,
    headings_from_docx,
    headings_from_markdown,
    headings_from_plain_text,
    render_outline_markdown,
    title_present_verbatim,
)


def docx_bytes(body: str) -> bytes:
    """Build a minimal DOCX carrying only the paragraph XML under test."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>",
        )
    return buffer.getvalue()


def paragraph(text: str, style: str = "") -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>"


def test_markdown_headings_keep_their_own_levels_and_numbering():
    text = "# Title\n\n## 1. Introduction\n\n### 1.1 Scope\n\ntext\n\n## 2. Methods\n"
    headings = headings_from_markdown(text)
    assert [(h.level, h.title) for h in headings] == [
        (1, "Title"),
        (2, "1. Introduction"),
        (3, "1.1 Scope"),
        (2, "2. Methods"),
    ]


def test_markdown_ignores_headings_inside_code_fences():
    text = "## Real\n\n```\n# not a heading\n```\n\n### Also real\n"
    assert [h.title for h in headings_from_markdown(text)] == ["Real", "Also real"]


def test_a_lone_top_level_heading_is_the_document_title_not_a_section():
    markdown = "# A review of allene synthesis\n\n## 1. Introduction\n\n### 1.1 Scope\n\n## 2. Methods\n"
    result = extract_outline(headings_from_markdown(markdown))
    assert [section.title for section in result.sections] == ["1. Introduction", "1.1 Scope", "2. Methods"]
    assert [section.level for section in result.sections] == [2, 3, 2]
    assert "A review of allene synthesis" in result.skipped


def test_two_headings_at_the_top_level_keep_both():
    # Nothing here is a document title: the paper simply has two top sections.
    result = extract_outline([Heading(1, "Mechanism study"), Heading(1, "Catalyst scope")])
    assert [section.title for section in result.sections] == [
        "Introduction",
        "Mechanism study",
        "Catalyst scope",
    ]


def test_docx_uses_word_heading_styles_and_outline_levels():
    raw = docx_bytes(
        paragraph("Review title", "Title")
        + paragraph("1. Introduction", "Heading1")
        + paragraph("1.1 Scope", "Heading2")
        + paragraph("Body text without a style")
        + paragraph("Deep heading", "Heading 3")
        + '<w:p><w:pPr><w:outlineLvl w:val="3"/></w:pPr><w:r><w:t>Outline level heading</w:t></w:r></w:p>'
    )
    assert [(h.level, h.title) for h in headings_from_docx(raw)] == [
        (0, "Review title"),
        (1, "1. Introduction"),
        (2, "1.1 Scope"),
        (3, "Deep heading"),
        (4, "Outline level heading"),
    ]
    # The Word title style never becomes one of the review's sections.
    imported = extract_outline(headings_from_docx(raw))
    assert [section.title for section in imported.sections] == [
        "1. Introduction",
        "1.1 Scope",
        "Deep heading",
        "Outline level heading",
    ]
    assert "Review title" in imported.skipped


def test_plain_text_only_trusts_numbered_or_short_title_lines():
    text = "1. Introduction\n\n2.1 Catalysts\n\nThis is a sentence of body prose that ends.\n"
    headings = headings_from_plain_text(text)
    assert [(h.level, h.title) for h in headings] == [(1, "Introduction"), (2, "Catalysts")]


def test_front_matter_and_back_matter_never_become_sections():
    headings = [
        Heading(1, "Abstract"),
        Heading(2, "Keywords: allene, copper"),
        Heading(1, "1. Introduction"),
        Heading(2, "1.1 Scope"),
        Heading(1, "5. Conclusion"),
        Heading(1, "Acknowledgements"),
        Heading(1, "References"),
        Heading(1, "Appendix A"),
    ]
    result = extract_outline(headings)
    assert [section.title for section in result.sections] == [
        "1. Introduction",
        "1.1 Scope",
        "5. Conclusion",
    ]
    assert [section.role for section in result.sections] == ["introduction", "body", "conclusion"]
    assert "Abstract" in result.skipped and "References" in result.skipped


def test_levels_are_compressed_so_the_editor_never_sees_a_jump():
    # The document only uses levels 1 and 3; the outline must become 2 and 3.
    result = extract_outline([Heading(1, "Introduction"), Heading(3, "Deep topic")])
    assert [(section.level, section.title) for section in result.sections] == [
        (2, "Introduction"),
        (3, "Deep topic"),
    ]
    # Nesting order is preserved for a deeper document.
    deeper = extract_outline(
        [Heading(1, "Introduction"), Heading(2, "Body"), Heading(3, "Sub"), Heading(2, "Body two")]
    )
    assert [section.level for section in deeper.sections] == [2, 3, 4, 3]


def test_introduction_is_inserted_only_when_the_document_has_none():
    without = extract_outline([Heading(1, "1. Copper catalysis"), Heading(1, "2. Conclusion")])
    assert without.introduced_heading is True
    assert without.sections[0].title == "Introduction"
    assert without.sections[0].role == "introduction"
    assert [section.title for section in without.sections[1:]] == ["1. Copper catalysis", "2. Conclusion"]

    with_intro = extract_outline([Heading(1, "1. Introduction"), Heading(1, "2. Conclusion")])
    assert with_intro.introduced_heading is False
    assert with_intro.sections[0].title == "1. Introduction"


def test_repeated_running_heads_are_dropped_once():
    result = extract_outline(
        [Heading(1, "1. Introduction"), Heading(1, "1. Introduction"), Heading(2, "2. Methods")]
    )
    assert [section.title for section in result.sections] == ["1. Introduction", "2. Methods"]
    assert result.dropped_duplicates == ["1. Introduction"]


def test_sections_are_capped_and_the_rest_is_reported():
    headings = [Heading(1, "1. Introduction")] + [Heading(2, f"{index}. Topic {index}") for index in range(2, 100)]
    result = extract_outline(headings, max_sections=10)
    assert len(result.sections) == 10
    assert result.truncated == len(headings) - 10


def test_rendered_markdown_carries_roles_and_unverified_notes():
    markdown = render_outline_markdown(
        [
            OutlineSection(2, "1. Introduction", role="introduction"),
            OutlineSection(3, "1.1 Scope"),
            OutlineSection(2, "5. Conclusion", role="conclusion", unverified=True),
        ]
    )
    assert markdown.splitlines() == [
        "## 1. Introduction",
        "Section role: introduction",
        "",
        "### 1.1 Scope",
        "",
        "## 5. Conclusion",
        "Section role: conclusion",
        "Verification: 标题未能在原文中逐字定位，请核对后再使用。",
    ]


def test_a_proposed_title_must_be_locatable_in_the_document():
    source = "2.1  Catalyst   screening\nWe screened several catalysts."
    assert title_present_verbatim("2.1 Catalyst screening", source) is True
    assert title_present_verbatim("2.2 Invented heading", source) is False


def test_empty_document_yields_no_sections_instead_of_an_invented_outline():
    result = extract_outline([])
    assert result.sections == []
    assert render_outline_markdown(result.sections) == ""
