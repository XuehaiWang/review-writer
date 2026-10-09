from review_writer_core.narrative_repetition import scan_repetition_hints


def test_empty_manuscript_is_not_a_failure():
    assert scan_repetition_hints([]) == {
        "status": "not_ready", "method": "surface_clues_v1",
        "covered_paragraph_count": 0, "total_paragraph_count": 0, "hints": [],
    }


def test_repeated_scientific_sentence_is_located_without_changing_text():
    sentence = (
        "The reported ligand changed the product distribution under the stated "
        "conditions, while the substrate scope remained restricted to the examined class."
    )
    paragraphs = [
        {"paragraph_id": "S01-p1", "text": sentence + " [1]"},
        {"paragraph_id": "S02-p2", "text": sentence + " [3]"},
    ]
    result = scan_repetition_hints(paragraphs, [
        {"section_id": "S01", "paragraphs": [paragraphs[0]]},
        {"section_id": "S02", "paragraphs": [paragraphs[1]]},
    ])
    assert result["covered_paragraph_count"] == 2
    assert result["hints"][0]["paragraph_id"] == "S02-p2"
    assert result["hints"][0]["other_paragraph_id"] == "S01-p1"
    assert result["hints"][0]["section_id"] == "S02"
    assert result["hints"][0]["basis"] == "surface_wording_only"
    assert paragraphs[1]["text"].endswith("[3]")


def test_numbers_and_negation_are_not_collapsed():
    baseline = "This substrate gave a 91% yield in the reported experiment under the stated catalyst conditions."
    paragraphs = [
        {"paragraph_id": "A", "text": baseline},
        {"paragraph_id": "B", "text": baseline.replace("91%", "19%")},
        {"paragraph_id": "C", "text": baseline.replace("gave", "did not give")},
    ]
    assert not [hint for hint in scan_repetition_hints(paragraphs)["hints"] if hint["kind"] == "wording_overlap"]


def test_repeated_closing_is_an_advisory_clue_not_a_semantic_verdict():
    paragraphs = [
        {"paragraph_id": str(index), "text": f"Taken together, this is separate conclusion number {index}."}
        for index in range(3)
    ]
    result = scan_repetition_hints(paragraphs)
    assert {hint["kind"] for hint in result["hints"]} == {"repeated_closing"}
    assert result["hints"][0]["phrase"] == "taken together"


def test_long_manuscript_reports_full_coverage_without_truncation():
    paragraphs = [
        {"paragraph_id": f"S{i // 50}-p{i}", "text": (
            f"The investigated transformation in system {i} had a distinct reported outcome "
            "under its stated experimental conditions and was not compared with other systems."
        )}
        for i in range(550)
    ]
    report = scan_repetition_hints(paragraphs)
    assert report["status"] == "complete"
    assert report["covered_paragraph_count"] == report["total_paragraph_count"] == 550
    assert report["hints"] == []
