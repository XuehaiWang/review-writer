import pytest
from review_writer_core.stages.planning.topic_recommendation import validate_recommendation, normalize_recommended_outline


def test_recommendation_keeps_domain_specific_questions_without_a_fixed_taxonomy():
    result = validate_recommendation({"organization": "Measurement scales", "sections": [
        {"title": "Spatial resolution", "role": "body", "question": "Which scales can be resolved?",
         "paper_ids": ["P1"], "rationale": "The selected study reports scale-dependent measurements."}
    ]}, [{"paper_id": "P1"}])
    assert "Spatial resolution" in result["outline_md"]
    assert "Which scales" in result["outline_md"]
    assert result["topic_outline_intent"]["system_recommended"]


def test_recommended_title_and_classification_label_use_the_same_normalized_title():
    result = validate_recommendation({"organization": "Synthesis routes", "sections": [
        {"title": "Copper-catalyzed   alkyne routes", "role": "body",
         "question": "How do ligands affect selectivity and substrate scope?",
         "paper_ids": ["P1"], "rationale": "Compare supported results and boundaries."},
    ]}, [{"paper_id": "P1"}])
    assert "## Copper-catalyzed alkyne routes" in result["outline_md"]
    assert "Purpose: How do ligands affect selectivity and substrate scope?" in result["outline_md"]
    assert result["topic_outline_intent"]["classification_contract"]["axes"][0]["partitions"][0]["label"] == "Copper-catalyzed alkyne routes"


def test_duplicate_recommended_titles_keep_a_distinct_supported_question():
    result = validate_recommendation({"organization": "Methods", "sections": [
        {"title": "Catalysis", "role": "body", "question": "What is established for copper?", "paper_ids": ["P1"], "rationale": "Copper evidence."},
        {"title": "Catalysis", "role": "body", "question": "What is established for palladium?", "paper_ids": ["P2"], "rationale": "Palladium evidence."},
    ]}, [{"paper_id": "P1"}, {"paper_id": "P2"}])
    assert "## What is established for palladium?" in result["outline_md"]
    labels = [partition["label"] for partition in result["topic_outline_intent"]["classification_contract"]["axes"][0]["partitions"]]
    assert labels == ["Catalysis", "What is established for palladium?"]


def test_recommendation_rejects_unknown_papers_instead_of_inventing_a_template():
    with pytest.raises(ValueError):
        validate_recommendation({"sections": [{"title": "Results", "role": "body", "question": "Why?", "paper_ids": ["unknown"]}]}, [{"paper_id": "P1"}])
    with pytest.raises(ValueError):
        validate_recommendation({}, [{"paper_id": "P1"}])


def test_recommendation_structure_keeps_body_comparison_but_leaves_conclusion_to_final():
    source = "# Outline\n## Evidence limitations\nSection role: body\nPurpose: compare methods.\n## Background\nSection role: introduction\nPurpose: define scope.\n## Outlook\nSection role: conclusion\nPurpose: conclude."
    result = normalize_recommended_outline(source)
    assert result.index("## Introduction") < result.index("## Evidence limitations")
    assert "define scope" in result
    assert "## Outlook" not in result
    assert normalize_recommended_outline(result) == result


def test_missing_introduction_is_a_plan_not_invented_scientific_history():
    result = normalize_recommended_outline("# Outline\n## Methods\nSection role: body\nPurpose: compare the studies.")
    assert "## Introduction\nSection role: introduction" in result
    assert "supported source context" in result
    assert result.index("## Introduction") < result.index("## Methods")
