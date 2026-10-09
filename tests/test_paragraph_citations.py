from review_writer_core.paragraph_citations import render_paragraph_citations


def test_contiguous_equal_sources_share_marker_without_rewriting_prose():
    rows = [("Conditions B were used.", "[11]", "reported_finding"),
            ("Three scale-up experiments were reported.", "[11]", "reported_finding")]
    assert render_paragraph_citations(rows) == "Conditions B were used. Three scale-up experiments were reported. [11]"
    assert rows[0][0] == "Conditions B were used."


def test_source_changes_and_noncontiguous_repeats_keep_local_citations():
    rows = [("A.", "[11]", "reported_finding"), ("B.", "[12]", "reported_finding"),
            ("C.", "[11]", "reported_finding"), ("D.", "[11, 12]", "reported_finding")]
    assert render_paragraph_citations(rows) == "A. [11] B. [12] C. [11] D. [11, 12]"


def test_direct_report_labels_do_not_split_the_same_source_scope():
    rows = [("A finding.", "[1]", "reported_finding"), ("Yield was reported.", "[1]", "reported_data")]
    assert render_paragraph_citations(rows) == "A finding. Yield was reported. [1]"


def test_same_source_groups_regardless_of_assertion_kind():
    assert render_paragraph_citations([('A.', '[1]', 'reported_finding'),
                                      ('An interpretation.', '[1]', 'author_inference')]) == 'A. An interpretation. [1]'
    assert render_paragraph_citations([('A.', '[1]', 'reported_finding'),
                                      ('B.', '[1]', '')]) == 'A. B. [1]'


def test_explicit_quotations_keep_local_attribution():
    assert render_paragraph_citations([('A.', '[1]', 'reported_finding'),
                                      ('The authors said “A”.', '[1]', 'reported_finding')]).count('[1]') == 2
    assert render_paragraph_citations([('A.', '[1]', 'reported_finding')]) == 'A. [1]'
    assert render_paragraph_citations([('B.', '[1]', 'reported_finding')]) == 'B. [1]'
    assert render_paragraph_citations([]) == ''


def test_long_runs_keep_nearby_markers():
    sentence = 'word ' * 70 + 'end.'
    # 2 x 71 words is still one marker: it stays inside the grouping window...
    assert render_paragraph_citations([(sentence, '[1]', 'reported_finding')] * 2).count('[1]') == 1
    # ...but a run past the window re-cites so the marker stays near its prose.
    long_sentence = 'word ' * 110 + 'end.'
    assert render_paragraph_citations([(long_sentence, '[1]', 'reported_finding')] * 2).count('[1]') == 2


def test_the_grouping_window_is_the_documented_value():
    from review_writer_core.paragraph_citations import MAX_GROUPED_WORDS

    # Raising the window merges more same-source sentences and removes markers;
    # it is a deliberate, measured trade-off rather than a magic literal.
    assert MAX_GROUPED_WORDS == 180
    sentence = 'word ' * 89 + 'end.'  # 90 words
    assert render_paragraph_citations([(sentence, '[1]', 'reported_finding')] * 2).count('[1]') == 1
    ninety_five = 'word ' * 94 + 'end.'  # 95 words: 95 + 95 > 180
    assert render_paragraph_citations([(ninety_five, '[1]', 'reported_finding')] * 2).count('[1]') == 2


def test_repetition_hints_span_paragraphs_without_merging_different_results():
    from review_writer_core.stages.sections.authoring import style_findings
    text = "The same method retained its original selectivity during the subsequent product derivatization."
    def claim(cid, paragraph, value, paper="p"):
        return {"claim_id": cid, "paragraph_id": paragraph, "claim": value,
                "evidence_refs": [{"paper_id": paper, "evidence_key": paper, "quote": "Source."}]}
    result = style_findings([claim("a", "p1", text), claim("b", "p2", text),
                             claim("c", "p3", text, "another")])
    assert result == {"b": ["repeated_wording_across_paragraphs"]}
    assert not style_findings([claim("a", "p1", text + " Yield 70%."),
                               claim("b", "p2", text + " Yield 80%.")])
