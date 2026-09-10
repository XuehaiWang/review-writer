"""Focused regression tests for repair routing and evidence-only updates."""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from review_writer_api.tests.test_feedback_loop_batching import feedback_loop as loop
from review_writer_core.draft_issue_routing import repair_capability
from review_writer_core.stages.draft.source_corrections import verified_corrections


def test_query_variants_keep_each_claim_instead_of_only_highest_scoring_topic():
    document = {'blocks': [{'page': 1, 'text': 'Room temperature reaction needs a gold catalyst.'},
                           {'page': 2, 'text': 'The range includes esters with 96% ee.'},
                           {'page': 3, 'text': 'Unrelated discussion of another topic.'}]}
    hits = loop.retrieve_original_passages('P1', 'gold catalyst and 96% ee', document,
                                          queries=['gold catalyst', '96% ee'])
    assert {h['page'] for h in hits} >= {1, 2}


def test_conflict_with_existing_context_gets_one_bounded_recheck(tmp_path):
    p, raw, expanded, _ = _views()
    raw['paragraph_scores'][0]['source_check_status'] = 'contradicted'
    evidence = {p['paragraph_id']: deepcopy(expanded)}
    calls = []
    def scorer(*args):
        calls.append(args)
        return deepcopy(raw)
    _recheck(tmp_path, p, raw, expanded, evidence, scorer)
    assert len(calls) == 1  # no new passages are required to resolve attribution
    history = {'entries': [{'paragraph_id': p['paragraph_id'],
                           'targeted_source_recheck': evidence[p['paragraph_id']]['targeted_source_recheck']}]}
    _recheck(tmp_path, p, raw, expanded, evidence, scorer, history)
    assert len(calls) == 1  # identical unresolved input does not loop forever


def _views():
    paragraph = {'paragraph_id': 'S1-p1', 'text': 'The result was 96% ee [1].'}
    raw = {'paragraph_scores': [{'paragraph_id': 'S1-p1', 'source_check_status': 'partially_supported',
                                'unsupported_claims': ['The result was 96% ee']}], 'dimension_scores': []}
    expanded = {'paper_ids': ['P1'], 'evidence': [{'paper_id': 'P1', 'source_content_hash': 'v1',
                   'original_passages': [{'ref': 'P1:p2:b3', 'text': 'The result was 96% ee.'}]}]}
    evidence = {'S1-p1': {'evidence': []}}
    return paragraph, raw, expanded, evidence


def _recheck(project, p, raw, expanded, evidence, scorer, history=None):
    with patch.object(loop, 'paragraph_metadata', return_value={}), \
         patch.object(loop, 'matrix_rows', return_value={}), \
         patch.object(loop, 'claim_evidence_contract', return_value={}), \
         patch.object(loop, 'source_evidence', return_value=deepcopy(expanded)), \
         patch.object(loop, 'read_json', return_value=history or {}), \
         patch.object(loop, 'update_status'):
        return loop.targeted_source_recheck(project, [p], raw, evidence, scorer)


def test_evidence_only_recheck_preserves_text_and_serializes_support(tmp_path):
    p, raw, expanded, evidence = _views()
    original = deepcopy(p)
    checked = {'paragraph_scores': [{'paragraph_id': p['paragraph_id'], 'source_check_status': 'verified',
                                    'source_evidence_refs': ['P1:p2:b3'], 'unsupported_claims': []}],
               'dimension_scores': []}
    result = _recheck(tmp_path, p, raw, expanded, evidence, lambda *_: checked)
    assert p == original
    assert result[0][1] == checked
    row = evidence[p['paragraph_id']]
    assert row['targeted_source_recheck']['status'] == 'supported_without_prose_change'
    first = tmp_path / '04_first_draft'
    first.mkdir()
    (first / 'first_draft.md').write_text(p['text'], encoding='utf-8')
    report = loop.original_source_check_report(tmp_path, checked, evidence)
    assert report['entries'][0]['paragraph_text_hash'] == hashlib.sha256(p['text'].encode()).hexdigest()
    restored = loop.evidence_from_source_check_report(report)
    assert restored[p['paragraph_id']]['evidence'][0]['source_content_hash'] == 'v1'
    assert restored[p['paragraph_id']]['targeted_source_recheck'] == row['targeted_source_recheck']


def test_saved_support_invalidated_when_citation_mapping_changes(tmp_path):
    paragraph = {'paragraph_id': 'S1-p1', 'text': 'Supported claim [1].'}
    blocks = [{'text': 'Supported claim', 'page': 1}]
    digest = hashlib.sha256(json.dumps(blocks, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    report = {'entries': [{'paragraph_id': 'S1-p1', 'paragraph_text_hash': hashlib.sha256(paragraph['text'].encode()).hexdigest(),
               'citation_binding': {'1': 'P1'}, 'source_check_status': 'verified',
               'targeted_source_recheck': {'status': 'supported_without_prose_change'}, 'paper_ids': ['P1'],
               'papers': [{'paper_id': 'P1', 'source_content_hash': digest,
                           'passages': [{'ref': 'P1:p1:b0', 'text': 'Supported claim'}]}]}]}
    with patch.object(loop, 'read_json', return_value=report), \
         patch.object(loop, 'claim_bound_evidence', return_value={}), \
         patch.object(loop, 'paragraph_paper_hint', return_value=''), \
         patch.object(loop, 'metadata_record', return_value={}), \
         patch.object(loop, 'load_original_source', return_value={'blocks': blocks}):
        with patch.object(loop, 'citation_entries', return_value=[{'callout': 1, 'paper_id': 'P1'}]):
            restored = loop.source_evidence(tmp_path, tmp_path, paragraph, {}, {})
            assert restored['targeted_source_recheck']['status'] == 'supported_without_prose_change'
        with patch.object(loop, 'citation_entries', return_value=[{'callout': 1, 'paper_id': 'P2'}]):
            rebuilt = loop.source_evidence(tmp_path, tmp_path, paragraph, {}, {})
            assert rebuilt['paper_ids'] == ['P2']
            assert not rebuilt.get('targeted_source_recheck')


def test_targeted_lookup_without_citations_does_not_use_old_structured_hint(tmp_path):
    with patch.object(loop, 'citation_entries', return_value=[]), \
         patch.object(loop, 'claim_bound_evidence', return_value={}), \
         patch.object(loop, 'load_original_source') as loader:
        view = loop.source_evidence(tmp_path, tmp_path, {'paragraph_id': 'P1-p1', 'text': 'Claim without citation.'},
                                    {'cited_paper_ids': ['P1']}, {}, queries=['Claim'])
    assert view['paper_ids'] == []
    loader.assert_not_called()


def test_unresolved_saved_passages_survive_evaluation_but_changed_sources_do_not(tmp_path):
    paragraph = {'paragraph_id': 'S1-p1', 'text': 'An observation [1] and another observation [2].'}
    blocks = [{'text': 'Original source observation.', 'page': 1}]
    digest = hashlib.sha256(json.dumps(blocks, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    report = {'entries': [{'paragraph_id': 'S1-p1',
        'paragraph_text_hash': hashlib.sha256(paragraph['text'].encode()).hexdigest(),
        'citation_binding': {'1': 'P1', '2': 'P2'}, 'source_check_status': 'needs_human_review',
        'unsupported_claims': ['The abstract attribution needs checking.'],
        'targeted_source_recheck': {'status': 'checked_no_new_support'}, 'paper_ids': ['P1', 'P2'],
        'papers': [{'paper_id': pid, 'source_content_hash': digest,
                    'passages': [{'ref': f'{pid}:p1:b0', 'text': blocks[0]['text']}]}
                   for pid in ['P1', 'P2']]}]}
    with patch.object(loop, 'read_json', return_value=report), \
         patch.object(loop, 'claim_bound_evidence', return_value={'P1': [{'ref': 'P1:p1:b0', 'text': blocks[0]['text']}]}), \
         patch.object(loop, 'metadata_record', return_value={}), \
         patch.object(loop, 'citation_entries', return_value=[{'callout': 1, 'paper_id': 'P1'}, {'callout': 2, 'paper_id': 'P2'}]), \
         patch.object(loop, 'load_original_source', return_value={'blocks': blocks, 'source_path': 'current/source.json'}) as loader:
        restored = loop.source_evidence(tmp_path, tmp_path, paragraph, {}, {})
        assert restored['paper_ids'] == ['P1', 'P2']
        assert len(restored['evidence']) == 2
        assert restored['local_source_available']
        assert restored['evidence'][1]['source_path'] == 'current/source.json'
        assert restored['targeted_source_recheck']['status'] == 'checked_no_new_support'
        assert report['entries'][0]['source_check_status'] == 'needs_human_review'
        loader.return_value = {'blocks': [{'text': 'Changed source.', 'page': 1}]}
        rebuilt = loop.source_evidence(tmp_path, tmp_path, paragraph, {}, {})
        assert not rebuilt.get('targeted_source_recheck')


def test_targeted_retrieval_is_not_dominated_by_unrelated_paragraph_numbers():
    paragraph = 'One experiment used 10% and 20% and 30% and 40%. The abstract reports a second catalyst.'
    blocks = [{'page': 1, 'text': 'Abstract: the second catalyst is active at room temperature.'}]
    blocks += [{'page': i + 2, 'text': 'Data: 10% and 20% and 30% and 40% catalyst.'} for i in range(8)]
    hits = loop.retrieve_original_passages('P1', paragraph, {'blocks': blocks}, queries=['abstract second catalyst'])
    assert hits[0]['page'] == 1


def test_phantom_reference_or_missing_core_cannot_be_saved_as_pure_support(tmp_path):
    for extra in ({'source_evidence_refs': ['P1:p2:b3', 'invented']}, {'missing_core_claim_ids': ['claim1']}):
        p, raw, expanded, evidence = _views()
        checked = {'paragraph_scores': [{'paragraph_id': p['paragraph_id'], 'source_check_status': 'verified',
                    'source_evidence_refs': ['P1:p2:b3'], **extra}]}
        _recheck(tmp_path, p, raw, expanded, evidence, lambda *_: checked)
        assert evidence[p['paragraph_id']]['targeted_source_recheck']['status'] != 'supported_without_prose_change'


def test_no_progress_cache_does_not_call_model_again_until_source_changes(tmp_path):
    p, raw, expanded, evidence = _views()
    calls = []
    def scorer(*_):
        calls.append(1)
        return raw
    _recheck(tmp_path, p, raw, expanded, evidence, scorer)
    history = {'entries': [{'paragraph_id': p['paragraph_id'],
                           'targeted_source_recheck': evidence[p['paragraph_id']]['targeted_source_recheck']}]}
    raw['paragraph_scores'][0]['diagnosis'] = 'A differently worded diagnosis of the same claim.'
    cached_evidence = {p['paragraph_id']: {'evidence': []}}
    _recheck(tmp_path, p, raw, expanded, cached_evidence, scorer, history)
    assert len(calls) == 1
    assert cached_evidence[p['paragraph_id']]['paper_ids'] == ['P1']
    assert cached_evidence[p['paragraph_id']]['evidence'][0]['original_passages']
    # A different disputed assertion must not inherit another check's failure.
    raw['paragraph_scores'][0]['unsupported_claims'] = ['The abstract reports this result']
    _recheck(tmp_path, p, raw, expanded, deepcopy(cached_evidence), scorer, history)
    assert len(calls) == 2
    raw['paragraph_scores'][0]['unsupported_claims'] = ['The result was 96% ee']
    expanded['evidence'][0]['source_content_hash'] = 'v2'
    _recheck(tmp_path, p, raw, expanded, {p['paragraph_id']: {'evidence': []}}, scorer, history)
    assert len(calls) == 3


def test_provider_failure_is_deferred_not_cached_as_missing_science(tmp_path):
    p, raw, expanded, evidence = _views()
    with patch.object(loop, 'recoverable_paragraph_provider_failure', return_value=True):
        result = _recheck(tmp_path, p, raw, expanded, evidence,
                          lambda *_: (_ for _ in ()).throw(RuntimeError('provider unavailable')))
    assert result[0][1]['paragraph_scores'] == raw['paragraph_scores']
    assert evidence[p['paragraph_id']]['targeted_source_recheck'] == {'status': 'provider_deferred'}
    assert evidence[p['paragraph_id']]['evidence'][0]['original_passages']


def test_invalid_recheck_keeps_baseline_instead_of_failing_batch(tmp_path):
    p, raw, expanded, evidence = _views()
    result = _recheck(tmp_path, p, raw, expanded, evidence,
                      lambda *_: {'paragraph_scores': [{'paragraph_id': 'wrong-id'}]})
    assert result[0][1]['paragraph_scores'] == raw['paragraph_scores']
    assert evidence[p['paragraph_id']]['targeted_source_recheck']['status'] == 'response_invalid'


def test_length_only_is_advisory_but_real_conflict_is_not():
    base = {'failed_dimensions': ['P01']}
    repair = {'repair_stage': 'draft', 'repair_target': {'paragraph_id': 'p1'},
              'rewrite_eligible': True, 'auto_repairable': True}
    result = repair_capability(base, repair, source_status='verified', source_ready=True)
    assert result['repair_class'] == 'advisory'
    assert not result['blocking'] and not result['rewrite_eligible']
    conflict = repair_capability({**base, 'unsupported_claims': ['wrong catalyst']}, repair,
                                 source_status='contradicted', source_ready=True)
    assert conflict['repair_class'] == 'human_confirmation'


def test_exact_source_correction_only_unlocks_target_value():
    text = 'CuBr gave 82% yield [1].'
    evidence = {'evidence': [{'original_passages': [{'ref': 'P1:p1:b1',
                                                    'text': 'Divalent CuBr2 gave 82% yield.'}]}]}
    proposals = [{'before': 'CuBr', 'after': 'CuBr2', 'source_ref': 'P1:p1:b1',
                  'source_quote': 'Divalent CuBr2 gave 82% yield.', 'unambiguous': True}]
    corrections = verified_corrections(text, proposals, evidence)
    assert corrections
    errors, _ = loop.validate_rewrite_report(text, 'CuBr2 gave 82% yield [1].', 1, 100,
                                            source_corrections=corrections)
    assert not errors
    errors, _ = loop.validate_rewrite_report(text, 'CuBr2 gave 99% yield [2].', 1, 100,
                                            source_corrections=corrections)
    assert 'protected_numbers_changed' in errors and 'protected_callouts_changed' in errors
    assert not verified_corrections(text, [{**proposals[0], 'source_quote': 'Invented CuBr2'}], evidence)
    assert not verified_corrections(text, [{**proposals[0], 'unambiguous': False}], evidence)


def test_evaluation_policy_does_not_demand_verbatim_common_definitions():
    prompt = loop.evaluation_prompt({'dimensions': []}, [], {}, {}, 90, 85)
    assert 'Basic terminology definitions' in prompt
    assert 'experiment-specific numbers' in prompt
    assert 'never padding' in prompt or 'never padding' in prompt.lower() or 'padding' in prompt


def test_reused_baseline_rechecks_only_target_and_updates_score_aliases(tmp_path):
    first = tmp_path / '04_first_draft'
    first.mkdir()
    markdown = 'The result was 96% ee [1].\n\n<!-- paragraph_id: S1-p1 -->\n'
    (first / 'first_draft.md').write_text(markdown, encoding='utf-8')
    rubric = {'dimensions': [{'id': 'P01', 'weight': 40, 'scope': 'paragraph'},
                            {'id': 'M01', 'weight': 60, 'scope': 'global'}]}
    baseline = {'quality_scope': loop.FULL_DRAFT_QUALITY_SCOPE,
                'evaluation_rule_version': loop.DRAFT_QUALITY_RULE_VERSION,
                'evaluation_input_sha256': loop.sha256_file(first / 'first_draft.md'),
                'paragraph_coverage': ['S1-p1'], 'score': 65, 'total_score': 65,
                'dimension_scores': [{'id': 'P01', 'level': 2}, {'id': 'M01', 'level': 3}],
                'paragraph_scores': [{'paragraph_id': 'S1-p1', 'source_check_status': 'partially_supported'}]}
    (first / 'baseline_quality.json').write_text(json.dumps(baseline), encoding='utf-8')
    _, _, expanded, _ = _views()
    expanded.update(original_source_ready=True, local_source_available=True)
    checked = {'dimension_scores': [{'id': 'P01', 'level': 4}],
               'paragraph_scores': [{'paragraph_id': 'S1-p1', 'score': 95, 'route': 'pass', 'severity': 'none',
                   'source_check_status': 'verified', 'source_evidence_refs': ['P1:p2:b3']}]}
    def recheck(_project, paragraphs, _raw, evidence, score_one):
        evidence['S1-p1'] = expanded
        return [(paragraphs, score_one(paragraphs[0], expanded))]
    with patch.object(loop, 'targeted_source_recheck', side_effect=recheck), \
         patch.object(loop, 'call_json_model', return_value=checked) as model, \
         patch.object(loop, 'queue_artifacts', return_value={'gate_decision': 'pass'}), \
         patch.object(loop, 'update_status'):
        _, result, _, _, _ = loop.reusable_baseline_evaluation(tmp_path, artifact_dir=tmp_path / 'audit',
            status_iteration=1, args=SimpleNamespace(goal=90, paragraph_goal=85), rubric=rubric)
    assert model.call_count == 1
    assert 'Targeted source recheck S1-p1' == model.call_args.kwargs['label']
    assert result['score'] == result['total_score'] == 85
    assert result['dimension_score_basis']['global'] == 'retained_baseline'
