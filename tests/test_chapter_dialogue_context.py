from review_writer_core.chapter_dialogue_context import chapter_outline, normalize_related, read_cross_chapter
from review_writer_core.paragraph_revision import exact_hash


def sections():
    return [{"section_id": f"S{s}", "title": f"Chapter {s}", "paragraphs": [
        {"paragraph_id": f"S{s}-p{p}", "text": f"Chapter {s}, paragraph {p}. " + "Evidence. " * 40}
        for p in range(1, 6)]} for s in range(1, 4)]


def test_bounded_locators_cover_other_chapters_opening_and_ending_without_current_text():
    outline = chapter_outline(sections(), "S1", budget=1500)
    assert outline[0]["locators"] == []
    assert [[p["position"] for p in s["locators"]] for s in outline[1:]] == [[1, 5], [1, 5]]
    assert outline[1]["paragraph_count"] == 5
    assert all(len(p["excerpt"]) < 250 for s in outline for p in s["locators"])


def test_related_read_uses_exact_current_paragraphs_and_not_whole_chapters():
    data = sections()
    context = {"paragraphs": data[0]["paragraphs"], "outline": chapter_outline(data, "S1")}
    related = normalize_related([{"paragraph_id": p, "reason": "Compare claims"} for p in
        ["S1-p1", "S2-p5", "S2-p5", "S3-p3", "foreign-p1"]], context)
    assert [r["paragraph_id"] for r in related] == ["S1-p1", "S2-p5", "S3-p3"]
    read, hashes = read_cross_chapter(data, "S1", related)
    assert read["read_only"] is True
    assert [p["paragraph_id"] for p in read["paragraphs"]] == ["S2-p5", "S3-p3"]
    assert read["paragraphs"][0]["text"] == data[1]["paragraphs"][-1]["text"]
    assert hashes == {p["paragraph_id"]: exact_hash(p["text"]) for p in read["paragraphs"]}


def test_limits_and_missing_context_are_explicit_and_legacy_context_works():
    data = sections()
    context = {"paragraphs": data[0]["paragraphs"], "outline": chapter_outline(data, "S1")}
    related = normalize_related([{"paragraph_id": p["paragraph_id"], "reason": "Comparison"}
        for s in data[1:] for p in s["paragraphs"]], context)
    assert len(related) == 6
    read, hashes = read_cross_chapter(data, "S1", related, budget=10)
    assert not read["paragraphs"] and not hashes
    assert len(read["omitted_paragraph_ids"]) == 6
    assert normalize_related(related, {"paragraphs": data[0]["paragraphs"]}) == []
    assert chapter_outline([], "S1") == []
