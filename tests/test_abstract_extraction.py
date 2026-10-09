import pytest
from review_writer_core.abstract_extraction import (
    build_opening_candidates,
    extract_abstract,
    recover_opening_abstract,
)


@pytest.mark.parametrize('quote', [
    'We investigate a new catalyst and report selective conversion under mild conditions, with limitations for bulky substrates.',
    '本文研究催化反应中的选择性调控，提出一种新的配体设计方法，并通过实验验证其适用范围与局限性。',
])
def test_unlabelled_opening_is_recovered_only_after_source_bound_identification(quote):
    sources = [{'evidence_key': 'front', 'opening_candidate': True, 'content': 'Title. ' + quote, 'section_path': []}]
    raw = {'decision': 'abstract', 'spans': [{'evidence_key': 'front', 'quote': quote}]}
    result = recover_opening_abstract(raw, sources)
    assert result['value'] == quote
    assert result['human_checked'] is False
    assert recover_opening_abstract(raw, sources, existing='Existing abstract') == {}
    assert recover_opening_abstract(raw, sources, existing={'value': 'Reviewed abstract', 'human_checked': True}) == {}
    assert recover_opening_abstract(raw, [{**sources[0], 'opening_candidate': False}]) == {}
    assert recover_opening_abstract(raw, [{**sources[0], 'section_path': ['1 Introduction']}]) == {}
    assert recover_opening_abstract({**raw, 'spans': [{'evidence_key': 'front', 'quote': quote + ' Invented result.'}]}, sources) == {}
    # Position alone is never a positive verdict.
    for decision in ('uncertain', 'not_abstract', '', None):
        assert recover_opening_abstract({**raw, 'decision': decision}, sources) == {}


@pytest.mark.parametrize('raw', [None, [], 'abstract', {}, {'decision': 'abstract', 'spans': [None]},
    {'decision': 'abstract', 'spans': [{'evidence_key': [], 'quote': 'bad'}]}])
def test_bad_optional_identification_is_not_a_failure(raw):
    assert recover_opening_abstract(raw, []) == {}


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


# A real Elsevier front-matter layout: the abstract is labelled with letter-spaced
# characters and MinerU emits no heading for the following body section, so the
# publisher's rights line is the only end-of-abstract marker.
ELSEVIER_FRONT_MATTER = """# A nickel-catalyzed electrochemical carboxylation of propargylic esters

![](../extracted/p001/images/x.jpg)

Yuqing Zhong, Mengmeng Jiang

## a r t i c l e i n f o

Article history: Received 22 January 2025 Revised 18 March 2025 Accepted 4 April 2025

Keywords: CO Nickel-catalysis Electro-carboxylation

## a b s t r a c t

Here, we report a novel nickel-catalyzed electrochemical carboxylation of propargylic esters with carbon dioxide, which proceeds under mild conditions and tolerates a broad range of functional groups.

© 2025 Published by Elsevier B.V. on behalf of Chinese Chemical Society.

Allenes are common structural motifs in a diverse range of natural products, pharmaceuticals and agrochemicals, and their preparation has attracted sustained attention.

Notably, the carboxylation of propargylic alcohol derivatives represents a long-standing challenge in this field of study.
"""


def test_letter_spaced_abstract_stops_at_the_publisher_rights_line():
    field = extract_abstract([], ELSEVIER_FRONT_MATTER)
    assert field["source"] == "mineru_markdown_abstract_region"
    assert field["value"].startswith("Here, we report a novel nickel-catalyzed")
    # The article body and the rights line are not part of the abstract.
    assert "Allenes are common structural motifs" not in field["value"]
    assert "Published by Elsevier" not in field["value"]
    assert "Article history" not in field["value"]


def test_runaway_capture_falls_back_to_the_first_paragraph():
    body = "\n\n".join(
        f"Body paragraph {index} discusses the wider literature at length." for index in range(40)
    )
    markdown = "## a b s t r a c t\n\nThe abstract states the question, approach and findings of this study clearly.\n\n" + body
    field = extract_abstract([], markdown)

    assert field["source"] == "mineru_markdown_abstract_region"
    assert field["value"] == "The abstract states the question, approach and findings of this study clearly."
    assert "Body paragraph" not in field["value"]


def test_abstract_graphic_between_label_and_text_is_skipped():
    markdown = (
        "# A bimetallic approach to axially chiral allenes\n\n"
        "ORGANIC LETTERS\n\n2013 Vol. 15, No. 9 2254-2257\n\n"
        "Ruizhi Lu, Juntao Ye\n\nABSTRACT\n\n"
        "![](../extracted/p001/images/6470cac1dd5384edfc5f86a848b8ef6d2b28aaf67a7d779b8d82b9703cb0e40b.jpg)\n\n"
        "An efficient bimetallic Zn(II)/Cu(I)-mediated asymmetric synthesis of simple axially "
        "chiral allenes from terminal alkynes and aldehydes was realized by taking advantage of "
        "the chiral amine (S)-alpha,alpha-diphenylprolinol.\n\n"
        + "\n\n".join(
            f"Body paragraph {index} discusses the wider literature at length and in detail."
            for index in range(20)
        )
    )
    field = extract_abstract([], markdown)

    assert field["source"] == "mineru_markdown_abstract_region"
    assert field["value"].startswith("An efficient bimetallic Zn(II)/Cu(I)-mediated")
    assert "extracted/p001/images" not in field["value"]
    assert "Body paragraph" not in field["value"]


def test_single_over_long_paragraph_is_cut_at_a_sentence():
    abstract = "This sentence reports the first finding of the study in a complete clause. "
    long_tail = "This trailing sentence belongs to the article body and is not abstract text. " * 40
    field = extract_abstract([], "ABSTRACT\n\n" + abstract * 3 + long_tail)

    # A single blob has no paragraph structure, so the cap is the only guard:
    # keep whole sentences and never the whole page.
    assert field["source"] == "mineru_markdown_abstract_region"
    assert len(field["value"]) <= 1_500
    assert field["value"].endswith(".")
    assert field["value"].startswith("This sentence reports the first finding")


def test_two_paragraph_abstract_is_kept_when_it_stays_short():
    markdown = (
        "## Abstract\n"
        "The first paragraph sets out the problem and the approach taken in this work.\n\n"
        "The second paragraph reports the main findings and their practical implications.\n\n"
        "## 1 Introduction\nThe body follows.\n"
    )
    value = extract_abstract([], markdown)["value"]
    assert "first paragraph" in value and "second paragraph" in value
    assert "body follows" not in value


def test_opening_candidates_are_quotable_windows():
    candidates = build_opening_candidates(ELSEVIER_FRONT_MATTER, "PDF first page text here")
    assert candidates and all(item["opening_candidate"] is True for item in candidates)
    keys = {item["evidence_key"] for item in candidates}
    assert "opening:pdf_page_1" in keys
    assert len(keys) == len(candidates)


def test_recovered_opening_abstract_must_quote_the_owned_source():
    candidates = build_opening_candidates(ELSEVIER_FRONT_MATTER)
    paragraph = next(
        item for item in candidates if item["content"].startswith("Here, we report")
    )
    quote = paragraph["content"]
    raw = {
        "decision": "abstract",
        "spans": [{"evidence_key": paragraph["evidence_key"], "quote": quote}],
    }
    assert recover_opening_abstract(raw, candidates)["value"] == quote
    # An invented sentence can never be stored, because it is not in the source.
    forged = {
        "decision": "abstract",
        "spans": [
            {
                "evidence_key": paragraph["evidence_key"],
                "quote": quote + " We additionally claim an unprecedented result.",
            }
        ],
    }
    assert recover_opening_abstract(forged, candidates) == {}
