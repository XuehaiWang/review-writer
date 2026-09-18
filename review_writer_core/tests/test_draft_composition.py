import pytest

from review_writer_core.draft_composition import (
    replace_section, section_text, source_signature, body_source, add_keywords,
)
from review_writer_core.paragraph_revision import dialogue_sections
from review_writer_core.draft_synthesis import generate, synthesis_source, publication_title

BODY = '# Review\n\n## Introduction\n\nSaved body.\n\n<!-- paragraph_id: S01-p1 -->\n\n## References\n\n[1] Source.\n'


def test_whole_section_replacement_preserves_other_sections_and_keywords():
    text = replace_section(BODY, 'conclusion', 'First.\n\nSecond.\n\nThird.')
    text = add_keywords(replace_section(text, 'abstract', 'Summary.'), ['one', 'two'])
    text = replace_section(text, 'conclusion', 'New one.\n\nNew two.')
    assert text.count('## Conclusion') == 1
    assert 'Third.' not in text
    assert 'Summary.' in text and '**Keywords:** one, two' in text
    assert text.index('## Conclusion') < text.index('## References')
    assert body_source(text) == body_source(BODY)
    sections = dialogue_sections(text, {}, 'artifact')
    assert [s['section_id'] for s in sections] == ['synthesis:abstract', 'S01', 'synthesis:conclusion']
    assert len(sections[-1]['paragraphs']) == 2


def test_dependency_signatures_are_role_scoped():
    before = replace_section(BODY, 'conclusion', 'Old conclusion.')
    after = replace_section(before, 'abstract', 'New abstract.')
    assert source_signature(before, 'conclusion') == source_signature(after, 'conclusion')
    assert source_signature(before, 'abstract') == source_signature(after, 'abstract')
    updated = replace_section(after, 'conclusion', 'Changed conclusion.')
    assert source_signature(updated, 'abstract') != source_signature(after, 'abstract')
    assert source_signature(updated, 'conclusion') == source_signature(after, 'conclusion')


def test_synthesis_order_and_partial_failure():
    calls = []
    def call(prompt, **kwargs):
        calls.append(kwargs['label'])
        if kwargs['label'] == 'draft-conclusion':
            return {'text': 'Bounded conclusion.'}
        assert 'Bounded conclusion.' in prompt
        raise ValueError('invalid response')
    result = generate({'draft_text': BODY, 'roles': ['conclusion', 'abstract'], 'initial': True}, call)
    assert calls == ['draft-conclusion', 'draft-abstract']
    assert result['sections'] == {'conclusion': 'Bounded conclusion.'}
    assert result['warnings'][0]['section'] == 'abstract'


def test_empty_markup_response_is_not_a_section():
    with pytest.raises(ValueError):
        replace_section(BODY, 'abstract', '## Abstract\n<!-- empty -->')


def test_missing_section_can_be_inserted_once():
    text = replace_section(BODY, 'abstract', 'Summary.')
    assert text.index('## Abstract') < text.index('## Introduction')
    text = replace_section(text, 'abstract', 'Revised.', identity='changed')
    assert text.count('## Abstract') == 1
    assert 'Summary.' not in section_text(text, 'abstract')


def test_explicit_identity_beats_heading_alias_and_numbered_legacy_is_supported():
    assert 'Legacy.' in section_text('## 6. Conclusions and Outlook\n\nLegacy.\n', 'conclusion')
    text = '## Abstract\n\nA quoted example.\n\n<!-- draft_role: abstract -->\n## Custom heading\n\nActual summary.\n'
    assert 'A quoted example.' not in section_text(text, 'abstract')
    assert 'Actual summary.' in section_text(text, 'abstract')


def test_retry_reuses_completed_section_without_second_model_call():
    def call(prompt, **kwargs):
        assert kwargs['label'] == 'draft-abstract'
        assert 'Completed conclusion.' in prompt
        return {'text': 'Summary.', 'keywords': ['one']}
    result = generate({'draft_text': BODY, 'roles': ['conclusion', 'abstract'], 'initial': True}, call,
        completed={'sections': {'conclusion': 'Completed conclusion.'}})
    assert set(result['sections']) == {'conclusion', 'abstract'}


def test_abstract_ignores_legacy_full_evidence_and_preserves_saved_prose():
    text = replace_section(BODY, 'abstract', 'Old summary.')
    text = replace_section(text, 'conclusion', 'Supported synthesis. Outlook is speculative.')
    text = text.replace('Saved body.', 'Saved body.\n\n![A [3+2] reaction](/api/image)\n\n*Figure 1. Caption.*')
    calls = []
    def call(prompt, **kwargs):
        calls.append(prompt)
        assert 'Saved body.' in prompt and 'Supported synthesis.' in prompt
        for excluded in ('Old summary.', '[1] Source.', '/api/image', 'Caption.', 'ARCHIVE_SENTINEL', 'SOURCE EVIDENCE', 'paragraph_id'):
            assert excluded not in prompt
        assert '120-250' in prompt
        return {'text': 'New summary.'}
    result = generate({'draft_text': text, 'roles': ['abstract'],
                       'source_evidence': {'sections': 'ARCHIVE_SENTINEL' * 10000}}, call)
    assert result['sections']['abstract'] == 'New summary.'
    assert len(calls) == 1 and len(calls[0]) < 2000


def test_initial_generation_only_conclusion_receives_compact_claims():
    def call(prompt, **kwargs):
        if kwargs['label'] == 'draft-conclusion':
            assert 'CURRENT_CLAIM' in prompt
            assert 'EVIDENCE_SENTINEL' not in prompt
            assert '2-3 connected paragraphs' in prompt
            return {'text': 'New conclusion.'}
        assert 'EVIDENCE_SENTINEL' not in prompt
        assert 'CURRENT_CLAIM' not in prompt
        assert 'New conclusion.' in prompt
        return {'text': 'Abstract.', 'keywords': ['review']}
    result = generate({'draft_text': BODY, 'roles': ['conclusion', 'abstract'],
                       'initial': True, 'source_evidence': {'fact': 'EVIDENCE_SENTINEL'},
                       'conclusion_context': {'sections': [{'claims': [{'claim': 'CURRENT_CLAIM'}]}]}}, call)
    assert result['keywords'] == ['review']


def test_abstract_source_preserves_late_sections_and_chinese_text():
    text = '# 标题\n## 正文\n' + '研究内容。' * 25000 + '\n## 参考文献\n引用列表\n## 总结\n最后的结论。'
    source = synthesis_source(text, include_conclusion=True)
    assert '引用列表' not in source
    assert source.endswith('最后的结论。')
    assert '研究内容。' * 25000 in source


def test_conclusion_uses_body_not_previous_synthesis_or_archive():
    text = replace_section(replace_section(BODY, 'abstract', 'OLD_ABSTRACT'), 'conclusion', 'OLD_CONCLUSION')
    def call(prompt, **kwargs):
        assert 'Saved body.' in prompt
        for excluded in ('OLD_ABSTRACT', 'OLD_CONCLUSION', '[1] Source.', 'HUGE_ARCHIVE'):
            assert excluded not in prompt
        assert kwargs['label'] == 'draft-conclusion'
        return {'text': 'Cross-study synthesis.\n\nSpecific limits and outlook.'}
    result = generate({'draft_text': text, 'roles': ['conclusion'],
                       'source_evidence': {'sections': 'HUGE_ARCHIVE' * 10000}}, call)
    assert 'Cross-study synthesis.' in result['sections']['conclusion']


def test_initial_abstract_generates_title_in_same_call_and_checkpoints_it():
    calls, snapshots = [], []
    def call(prompt, **kwargs):
        calls.append(prompt)
        assert 'title:string' in prompt
        return {'text': 'Summary.', 'title': '轴手性联烯的合成：策略与立体控制', 'keywords': ['synthesis']}
    result = generate({'draft_text': BODY, 'roles': ['abstract'], 'initial': True}, call, checkpoint=snapshots.append)
    assert len(calls) == 1
    assert result['title'] == snapshots[-1]['title'] == '轴手性联烯的合成：策略与立体控制'
    def unexpected(*args, **kwargs):
        raise AssertionError('Completed abstract must not run again')
    resumed = generate({'draft_text': BODY, 'roles': ['abstract'], 'initial': True}, unexpected, completed=result)
    assert resumed['title'] == result['title']


def test_manual_abstract_does_not_generate_title_and_bad_titles_are_rejected():
    def call(prompt, **kwargs):
        assert 'title:string' not in prompt
        return {'text': 'Summary.', 'title': 'Unrequested New Article Title'}
    result = generate({'draft_text': BODY, 'roles': ['abstract']}, call)
    assert result['title'] == ''
    assert publication_title('<script>oops</script>') == ''
    assert publication_title(None) == ''


def test_manual_abstract_can_fill_keywords_in_same_call_and_resume():
    def call(prompt, **kwargs):
        assert 'keywords:[5-8 concise strings]' in prompt
        return {'text': 'Summary.', 'keywords': [' evidence ', '', None, 'evidence', 'synthesis']}
    payload = {'draft_text': BODY, 'roles': ['abstract'], 'generate_keywords': True}
    result = generate(payload, call)
    assert result['keywords'] == ['evidence', 'synthesis']
    def unexpected(*args, **kwargs):
        raise AssertionError('Resume must reuse completed keywords')
    assert generate(payload, unexpected, completed=result)['keywords'] == result['keywords']


def test_existing_keywords_are_not_requested_even_on_initial_generation():
    def call(prompt, **kwargs):
        assert 'keywords:[5-8' not in prompt
        return {'text': 'Summary.', 'keywords': ['unsolicited']}
    assert generate({'draft_text': BODY, 'roles': ['abstract'], 'initial': True,
                     'generate_keywords': False}, call)['keywords'] == []
