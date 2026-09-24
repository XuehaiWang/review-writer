from review_writer_core.stages.planning.outline import (
    normalize_outline_section_ids,
    outline_sections,
)


def test_duplicate_outline_ids_keep_first_and_preserve_section_content():
    source = (
        "## First\n<!-- section_id: S01 -->\nAssigned papers: P1.\n"
        "### Child\n<!-- section_id: S01 -->\nPurpose: Compare evidence.\n"
        "## Later\n<!-- section_id: S02 -->\nNotes: Keep this explanation.\n"
    )
    normalized, repairs = normalize_outline_section_ids(source)
    assert repairs == [{"section_index": 2, "title": "Child", "old_id": "S01", "new_id": "S03"}]
    assert normalized == source.replace(
        "### Child\n<!-- section_id: S01 -->",
        "### Child\n<!-- section_id: S03 -->",
    )
    assert normalize_outline_section_ids(normalized) == (normalized, [])
    sections = outline_sections(source)  # Old saved outlines also parse safely.
    assert [section["section_id"] for section in sections] == ["S01", "S03", "S02"]
    assert sections[1]["parent_section_id"] == "S01"
    assert sections[1]["purpose"] == "Compare evidence."


def test_multiple_duplicates_are_stable_and_skip_later_reserved_ids():
    source = (
        "## One\n<!-- section_id: S01 -->\n"
        "## Two\n<!-- section_id: S01 -->\n"
        "## Three\n<!-- section_id: S01 -->\n"
        "## Four\n<!-- section_id: S02 -->\n"
    )
    normalized, repairs = normalize_outline_section_ids(source)
    assert [repair["new_id"] for repair in repairs] == ["S03", "S04"]
    assert [section["section_id"] for section in outline_sections(normalized)] == ["S01", "S03", "S04", "S02"]
