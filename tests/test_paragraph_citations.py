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


def test_ambiguity_and_paragraph_boundaries_preserve_citations():
    for second in [('An interpretation.', 'author_inference'), ('The authors said “A”.', 'reported_finding'), ('B.', '')]:
        assert render_paragraph_citations([('A.', '[1]', 'reported_finding'),
                                          (second[0], '[1]', second[1])]).count('[1]') == 2
    assert render_paragraph_citations([('A.', '[1]', 'reported_finding')]) == 'A. [1]'
    assert render_paragraph_citations([('B.', '[1]', 'reported_finding')]) == 'B. [1]'
    assert render_paragraph_citations([]) == ''


def test_long_runs_keep_nearby_markers():
    sentence = 'word ' * 70 + 'end.'
    assert render_paragraph_citations([(sentence, '[1]', 'reported_finding')] * 2).count('[1]') == 2
