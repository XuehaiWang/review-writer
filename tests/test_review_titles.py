from __future__ import annotations

from review_writer_core.review_titles import (
    build_publication_review_title,
    generated_title_is_acceptable,
    generated_title_needs_rewrite,
)


ATA_TOPIC = (
    'Please write a review on the topic “allenation-of-terminal-alkynes (ATA)”, '
    'focusing on the development of terminal alkyne allenation with different '
    'substrates to access mono-, 1,3-di-, and trisubstituted allenes. Organize '
    'the review by reaction type and catalytic/promoting system (Cu, Zn, Cd, Ti), '
    'and separately discuss racemic ATA and enantioselective ATA (EATA).'
)


def test_instruction_topic_becomes_a_concise_publication_title() -> None:
    assert build_publication_review_title(ATA_TOPIC) == (
        "Allenation of Terminal Alkynes (ATA): Reaction Classes and Catalytic Strategies"
    )


def test_repeated_manuscript_subject_is_enriched_from_topic_scope() -> None:
    assert build_publication_review_title(
        ATA_TOPIC,
        manuscript_title="allenation-of-terminal-alkynes (ATA)",
    ) == (
        "Allenation of Terminal Alkynes (ATA): Reaction Classes and Catalytic Strategies"
    )


def test_genuine_manuscript_title_is_preserved() -> None:
    assert build_publication_review_title(
        ATA_TOPIC,
        manuscript_title="Terminal Alkyne Allenation: Catalytic Systems and Selectivity",
    ) == "Terminal Alkyne Allenation: Catalytic Systems and Selectivity"


def test_scientific_hyphenation_is_not_flattened() -> None:
    assert build_publication_review_title(
        "Cu-catalyzed C–H functionalization"
    ) == "Cu-catalyzed C–H Functionalization"


def test_complete_topic_and_request_titles_require_rewrite() -> None:
    assert generated_title_needs_rewrite(ATA_TOPIC, ATA_TOPIC)
    assert not generated_title_is_acceptable(ATA_TOPIC)
    assert generated_title_is_acceptable(
        "Terminal Alkyne Allenation: Catalytic Systems and Selectivity"
    )
