from review_writer_core.abstract_extraction import extract_abstract


def test_mineru_short_split_abstract_stops_at_real_section_heading():
    blocks = [
        {"type": "text", "text": "ABSTRACT.", "page_idx": 0},
        {"type": "text", "text": "We report", "page_idx": 0},
        {"type": "text", "text": "a practical synthesis of substituted allenes using mild conditions.", "page_idx": 0},
        {"type": "text", "text": "Introduction of a ligand improved selectivity.", "page_idx": 0},
        {"type": "text", "text": "1.5-H transfer was observed in the reaction sequence.", "page_idx": 0},
        {"type": "text", "text": "1 Introduction", "page_idx": 0},
        {"type": "text", "text": "This is introductory context and not part of the abstract.", "page_idx": 0},
    ]
    field = extract_abstract(blocks, "")

    assert field["source"] == "content_list_abstract_region"
    assert field["source_page"] == 1
    assert field["source_block_index"] == 0
    assert "Introduction of a ligand" in field["value"]
    assert "1.5-H transfer" in field["value"]
    assert "introductory context" not in field["value"]


def test_markdown_summary_after_long_front_matter_is_recovered():
    markdown = "# Title\n" + ("Publisher front matter\n" * 1900) + (
        "## Summary\n"
        "A concise route to allenes uses common starting materials and gives a useful "
        "comparison across catalyst classes.\n\n"
        "## Introduction\nThe background is separate.\n"
    )
    field = extract_abstract([], markdown)

    assert field["source"] == "mineru_markdown_abstract_region"
    assert field["value"].startswith("A concise route")
    assert "background" not in field["value"]


def test_first_page_pdf_fallback_and_no_introduction_impersonation():
    field = extract_abstract(
        [],
        "# Introduction\nWe report a valuable synthesis of allenes with high selectivity.\n",
        "Abstract:\nWe describe a selective synthesis that uses readily available reagents "
        "and provides a broad substrate scope.\nKeywords: allene, catalysis\n",
    )
    assert field["source"] == "pdf_first_page_abstract_region"
    assert "Keywords" not in field["value"]

    missing = extract_abstract([], "# Introduction\nWe report an interesting synthesis.\n")
    assert missing["value"] == ""
    assert missing["source"] == "rule_not_found"


def test_conclusion_summary_is_not_mistaken_for_front_matter_abstract():
    markdown = (
        "# Study title\n# Introduction\nThe body starts here.\n"
        "# Summary\nThis is a conclusion section with many observations and "
        "should not be entered as the paper's original abstract.\n"
    )
    assert extract_abstract([], markdown)["value"] == ""


def test_acs_conspectus_is_an_explicit_abstract_label():
    first_page = (
        "Article title\nCONSPECTUS: Allenes are useful starting materials for synthesis.\n"
        "This Account reviews the development of allenation methods using terminal alkynes, "
        "aldehydes, and ketones.\n1. INTRODUCTION Background follows.\n"
    )
    field = extract_abstract([], "", first_page)
    assert field["source"] == "pdf_first_page_abstract_region"
    assert "INTRODUCTION" not in field["value"]
