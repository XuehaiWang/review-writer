from review_writer_core.overview_composition import overview_block, insert_before_introduction, compose_overview


def test_overview_after_abstract_and_before_introduction():
    body = '# Title\n\n## Abstract\n\nSummary.\n\n## 1. Introduction\n\nBody.'
    block = overview_block('image-id', {'title': 'Overview', 'subtitle': 'Strategies', 'labels': ['A', 'B']})
    preview = insert_before_introduction(body, block)
    assert preview.index('Summary.') < preview.index('![Overview') < preview.index('## 1. Introduction')
    assert '*Figure 1. Overview. Strategies — A, B.*' in preview
    assert insert_before_introduction(body, '') == body


def test_chinese_and_no_introduction_fallback():
    block = overview_block('image-id', {})
    assert '*Figure 1. Review overview.*' in block
    preview = insert_before_introduction('# 标题\n\n## 摘要\n摘要内容\n\n## 引言\n正文', block)
    assert preview.index('摘要内容') < preview.index('![Overview') < preview.index('## 引言')
    assert insert_before_introduction('# Title\n\nBody.', block).startswith('# Title\n\n![Overview')


def test_overview_shifts_captions_callouts_and_published_metadata_only():
    body = '''# Title

## Introduction
Compare Figure 1, Figure 2 and Figures 1–2; Fig. 1a. [1]
<!-- inserted_figure: {"figure_id":"P001-F01","published_label":"Figure 1","source_label":"Figure 1","output_artifact_id":"image1"} -->
![Figure 1](/api/v1/artifacts/image1/content)
*Figure 1. First reaction.*
<!-- paragraph_id: S01-p1 -->
![Figure 2](/api/v1/artifacts/image2/content)
*Figure 2. Second reaction.*
## References
[1] A paper called Figure 1.
'''
    out = compose_overview(body, 'overview', {'title': 'Summary'})
    assert '*Figure 1. Summary.*' in out
    assert 'Compare Figure 2, Figure 3 and Figures 2–3; Fig. 2a. [1]' in out
    assert '*Figure 2. First reaction.*' in out
    assert '*Figure 3. Second reaction.*' in out
    assert '"published_label":"Figure 2"' in out
    assert '"source_label":"Figure 1"' in out
    assert '"figure_id":"P001-F01"' in out
    assert '<!-- paragraph_id: S01-p1 -->' in out
    assert '[1] A paper called Figure 1.' in out
    assert compose_overview(body, '', {}) == body
    assert compose_overview(body, 'overview', {'title': 'Summary'}) == out
    assert '*Figure 2. First reaction.*' in compose_overview(body, 'replacement', {'title': 'New overview'})
