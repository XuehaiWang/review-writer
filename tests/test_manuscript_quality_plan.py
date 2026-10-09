"""Offline contracts for the manuscript quality plan; no provider spending."""
from copy import deepcopy
import json

import pytest

from review_writer_core.bibliography_audit import audit_bibliography
from review_writer_core.evidence_queries import build_question_query_plans
from review_writer_core.section_narrative_contracts import build_question_evidence_navigation
from review_writer_core.publication_tables import _result_with_units, _standalone_conditions
from review_writer_core.stages.sections.authoring import style_findings
from review_writer_core.stages.sections import source_writing as writer
from review_writer_core.paper_sources.base import SourceSearchResult


def test_question_changes_retrieval_without_adding_plans_or_required_gates():
    base = dict(review_topic="Clinical interventions", heading="Outcomes", required_fact_roles=["scope"])
    old = build_question_query_plans(**base)
    a = build_question_query_plans(**base, review_problem="Long-term safety")
    b = build_question_query_plans(**base, review_problem="External cohort applicability")
    assert len(old) == len(a) == len(b)
    assert a[0]["websearch_query"] != b[0]["websearch_query"]
    assert a[0]["required_for_section"] == old[0]["required_for_section"]


def test_navigation_preserves_opposing_and_unmatched_sources_and_input():
    task = {"review_problem": "Safety", "questions_to_answer": ["Applicability", "Applicability"]}
    sources = [dict(evidence_key="E1", content="Safety did not improve.", question_ids=["topic_partition"]),
               dict(evidence_key="E2", content="Applicability remains limited."),
               dict(evidence_key="E3", content="A separate finding.")]
    before = deepcopy((task, sources))
    navigation = build_question_evidence_navigation(task, sources)
    assert navigation["questions"][0]["source_refs"] == ["E1"]
    assert navigation["questions"][1]["source_refs"] == ["E2"]
    assert navigation["other_source_refs"] == ["E3"]
    assert "content" not in json.dumps(navigation)
    assert (task, sources) == before
    assert build_question_evidence_navigation({}, [])['questions'] == []


def test_chinese_question_is_not_discarded_and_retains_negative_evidence():
    task = {'review_problem': '适用范围有哪些限制'}
    source = {'evidence_key': 'E1', 'content': '底物适用范围有限，不能推广至其他类别。'}
    assert build_question_evidence_navigation(task, [source])['questions'][0]['source_refs'] == ['E1']
    assert build_question_query_plans(review_topic='化学研究', review_problem=task['review_problem'])[0]['question_term_groups']


def test_actual_writer_receives_navigation_once_and_resumes_without_calls():
    saved, calls = {}, []
    source = {"evidence_key": "owned", "paper_id": "P1", "content": "Safety did not improve in the cohort.",
              "claim_eligible": True, "source_lineage_hash": "v1", "question_ids": ["section_focus"]}
    def model(prompt, schema, label):
        calls.append(label)
        payload = json.loads(prompt[prompt.index('{"task":'):])
        group = payload['question_evidence_navigation']['questions'][0]
        assert group['question'] == 'Safety'
        assert group['source_refs'] == ['E001']
        assert len(payload['sources']) == 1
        text = 'Safety did not improve in the cohort.'
        return {"paragraphs": [{"role": "anchor_case", "text": text, "claims": [{"text": text,
            "claim_kind": "reported_finding", "review_reasons": [], "fact_ids": [], "result_context": [],
            "support_spans": [{"evidence_key": "E001", "quote": source['content']}]}]}]}
    options = dict(section_id='S1', task={'review_problem': 'Safety', 'primary_papers': ['P1']},
                   evidence=[source], context='', call=model, audit_mode='selective',
                   save_state=lambda state: saved.update(deepcopy(state)))
    first = writer.write_from_sources(**options)
    again = writer.write_from_sources(**{**options, 'resume_state': saved,
        'call': lambda *args: pytest.fail('Must reuse completed request')})
    assert first == again
    assert calls == ['section-source-writing']


META = {'title': {'value': 'Reliable clinical interventions in adult cohorts', 'confidence': .95},
        'authors': {'value': ['A. Writer']}, 'doi': {'value': '10.1000/primary', 'confidence': .4}}


def candidate(doi='10.1000/primary', **overrides):
    return {'title': META['title']['value'], 'authors': ['A. Writer'],
            'identifiers': {'doi': doi}, 'journal': 'Clinical Journal', 'volume': '12',
            'pages': '34-41', 'year': 2024, **overrides}


class Connector:
    name = 'crossref'
    def search(self, request):
        return SourceSearchResult(source=self.name, status='completed', candidates=[candidate()])


def test_provider_locator_fields_reach_canonical_updates():
    result = audit_bibliography(META, connectors=[Connector()], network_mode='force')
    assert result['canonical_updates']['volume']['value'] == '12'
    assert result['canonical_updates']['pages']['value'] == '34-41'


def test_cached_wrong_identity_is_rechecked_not_trusted_by_status_or_doi():
    previous = {'sources': {'crossref': {'status': 'verified', 'match': {'doi_exact': True},
        'candidate': candidate(title='Optical measurements of distant galaxies')}}}
    before = deepcopy(previous)
    result = audit_bibliography(META, connectors=[], previous_audit=previous, network_mode='force')
    assert result['sources']['crossref']['status'] == 'not_same_publication'
    assert not result['canonical_updates']
    assert previous == before


def test_similar_titles_with_different_dois_cannot_supply_mixed_identity():
    previous = {'sources': {name: {'status': 'verified', 'candidate': candidate(doi)}
                           for name, doi in [('crossref', '10.1000/primary'), ('openalex', '10.1000/secondary')]}}
    result = audit_bibliography(META, connectors=[], previous_audit=previous, network_mode='force')
    assert len(result['sources']) == 2
    assert all(row['status'] == 'conflict' for row in result['sources'].values())
    assert not result['canonical_updates']


@pytest.mark.parametrize('result,units,expected', [('96–99% ee or de', 'yield', '96–99% ee or de'),
    ('96%', 'ee', '96% ee'), ('12', 'ms', '12 ms'), ('82% yield', '%', '82% yield')])
def test_result_units_do_not_relabel_existing_metrics(result, units, expected):
    assert _result_with_units(result, units) == expected


@pytest.mark.parametrize('value', ['entries 7 and 8', 'optimized conditions', 'under standard conditions'])
def test_conditions_do_not_publish_bare_source_pointers(value):
    assert _standalone_conditions(value) == ''


def test_prose_findings_are_nonmutating_hints_not_sentence_rewriters():
    rows = [{'claim_id': 'c', 'paragraph_id': 'p', 'claim': 'The route gives allenes. and selectivity varies.',
             'evidence_refs': [{'paper_id': 'P1', 'evidence_key': 'e', 'quote': 'Selectivity varies.'}]}]
    before = deepcopy(rows)
    assert 'possible_broken_sentence_boundary' in style_findings(rows)['c']
    assert rows == before


def test_short_caption_recovers_only_explicitly_linked_context():
    from review_writer_core.figure_caption import caption_fields
    row = {'source_caption_text': 'Scheme 1. Proposed mechanism.',
           'caption_source_text': 'Scheme 1. Proposed pathway for formation of the reported product.',
           'caption_source_ref': {'method': 'adjacent_caption', 'block_index': 4}}
    assert 'formation' in caption_fields(row)['publication_caption_text']
    row.pop('caption_source_ref')
    assert caption_fields(row)['publication_caption_text'] == 'Proposed mechanism'


def test_english_manuscript_keeps_chinese_author_font_and_english_labels():
    from review_writer_core.manuscript_state import build_manuscript_state
    from review_writer_core.latex_renderer import render_tex
    from pathlib import Path
    state = build_manuscript_state('# Review\n\nAuthors: 王明\n\n## Abstract\n\nA review.\n\n## Introduction\n\nText.')
    template = Path('review_writer_core/resources/pdf/modern-survey.tex').read_text(encoding='utf-8')
    rendered = render_tex(state, profile='en', template=template)
    assert 'setCJKmainfont' in rendered
    assert 'Abstract' in rendered and '王明' in rendered


def test_table_does_not_force_a_float_barrier_before_the_next_section():
    from review_writer_core.latex_renderer import render_body
    state = {'blocks': [{'kind': 'heading', 'text': 'First', 'level': 2},
        {'kind': 'table', 'header': ['Method', 'Outcome'], 'rows': [['A', 'Feasible']]},
        {'kind': 'heading', 'text': 'Detail', 'level': 3},
        {'kind': 'paragraph', 'text': 'Discuss the table.'},
        {'kind': 'heading', 'text': 'Second', 'level': 2}]}
    body = render_body(state)
    # A forced barrier used to flush the wide table to a fresh page and leave a
    # nearly blank page in the two-column layout; floats now flow naturally and
    # are drained once before the bibliography.
    assert 'FloatBarrier' not in body
    assert body.index('Discuss the table.') < body.index('Second')


def test_small_table_uses_column_width_but_wide_table_keeps_full_width():
    from review_writer_core.latex_renderer import render_body
    compact = render_body({'blocks': [{'kind': 'table', 'header': ['Method', 'Outcome'],
                                      'rows': [['A', 'Feasible']]}]})
    wide = render_body({'blocks': [{'kind': 'table', 'header': ['Method', 'Input', 'Conditions', 'Outcome'],
                                   'rows': [['A', 'B', 'C', 'D']]}]})
    assert r'\begin{table}[' in compact and r'\columnwidth' in compact
    assert r'\begin{table*}[' in wide and r'\textwidth' in wide


def test_conclusion_same_call_has_no_paragraph_or_challenge_quota():
    from review_writer_core.draft_synthesis import generate
    calls = []
    def model(prompt, **kwargs):
        calls.append(prompt)
        assert 'each 150-300 words' not in prompt and '2-3 specific challenges' not in prompt
        assert 'counterexamples' in prompt
        return {'text': 'The evidence establishes feasibility within the tested conditions.'}
    result = generate({'draft_text': '# Review\n\n## Results\n\nFeasibility was tested.',
                       'roles': ['conclusion']}, call=model)
    assert len(calls) == 1 and result['sections']['conclusion']
