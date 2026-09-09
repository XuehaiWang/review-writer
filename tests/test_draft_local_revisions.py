"""Subject-independent tests for local argument revisions and atomic groups."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_core.stages.draft.revisions import (
    accept_argument_revisions, claim_index, effective_writing_plan, fingerprint,
    prepare_argument_revisions, revision_groups,
)


def plan():
    return {"sections": [{"section_id": "alpha", "claims": [{
        "claim_id": "C", "paragraph_id": "alpha-p1", "proposition": "Broad assertion",
        "citation_group": ["paper"], "required_for_section": True, "fact_ids": ["F"],
        "argument_basis": {"verification": {"status": "verified"}, "premises": ["F"]},
    }], "paragraphs": [{"paragraph_id": pid, "claim_ids": ["C"]}
                        for pid in ("alpha-p1", "alpha-p2")]}]}


def delta():
    return {"claim_id": "C", "group_id": "g", "paragraph_ids": ["alpha-p1", "alpha-p2"],
            "base_claim_sha256": fingerprint(claim_index(plan())["C"]),
            "original_proposition": "Broad assertion", "proposition": "Bounded assertion",
            "source_evidence_refs": ["R"], "reason": "Supported within the tested population"}


def test_effective_view_preserves_source_identity_and_never_reuses_verification():
    baseline = plan()
    overlays = {"entries": {}, "argument_revisions": {"C": delta()}}
    effective = effective_writing_plan(baseline, overlays)
    claim = claim_index(effective)["C"]
    assert claim["proposition"] == claim["allowed_assertion"] == "Bounded assertion"
    assert claim["citation_group"] == ["paper"] and claim["fact_ids"] == ["F"]
    assert claim["required_for_section"] is True
    assert "verification" not in claim["argument_basis"]
    assert baseline == plan() and overlays["argument_revisions"]["C"] == delta()
    assert effective_writing_plan(baseline, {"entries": {}}) == baseline
    baseline["sections"][0]["claims"][0]["fact_ids"] = ["different"]
    with pytest.raises(ValueError, match="conflicts"):
        effective_writing_plan(baseline, overlays)


def test_grouping_includes_dependent_paragraphs_without_absorbing_independent_work():
    paragraphs = [{"paragraph_id": p} for p in ("alpha-p1", "alpha-p2", "beta-p1")]
    groups = revision_groups([
        {"paragraph_id": "alpha-p1", "repair_class": "planning_adjustment", "claim_ids": ["C"]},
        {"paragraph_id": "alpha-p2", "repair_class": "planning_adjustment", "claim_ids": ["C"]},
        {"paragraph_id": "beta-p1", "repair_class": "draft_rewrite", "rewrite_eligible": True},
    ], plan(), paragraphs)
    assert len(groups) == 2
    assert groups[0]["paragraph_ids"] == ["alpha-p1", "alpha-p2"]
    assert groups[1]["claim_ids"] == []


def test_model_cannot_change_roles_sources_or_unregistered_claims():
    group = {"claim_ids": ["C"], "paragraph_ids": ["alpha-p1"], "group_id": "g"}
    row = {**delta(), "required_for_section": False, "citation_group": ["invented"]}
    built = prepare_argument_revisions([row], group=group, baseline=plan(), effective=plan(),
                                       source_overlays={}, evidence_refs={"R"})
    assert "citation_group" not in built["C"] and "required_for_section" not in built["C"]
    for bad in ({**row, "claim_id": "unknown"}, {**row, "source_evidence_refs": ["fake"]}):
        with pytest.raises(ValueError):
            prepare_argument_revisions([bad], group=group, baseline=plan(), effective=plan(),
                                       source_overlays={}, evidence_refs={"R"})


def test_acceptance_cannot_split_group_and_preserves_other_namespaces():
    old = {"entries": {"previous": {"rewritten_text": "kept"}}, "unknown_legacy": {"keep": True}}
    candidate = {"argument_revisions": {"C": delta()}}
    changes = [{"paragraph_id": p, "group_id": "g"} for p in ("alpha-p1", "alpha-p2")]
    with pytest.raises(ValueError, match="complete"):
        accept_argument_revisions(old, candidate, {"alpha-p1"}, changes)
    accepted = accept_argument_revisions(old, candidate, {"alpha-p1", "alpha-p2"}, changes)
    assert accepted["entries"] == old["entries"] and accepted["unknown_legacy"] == old["unknown_legacy"]
    assert accepted["argument_revisions"] == candidate["argument_revisions"]
    assert "argument_revisions" not in old


def test_partial_acceptance_updates_unchanged_dependent_scores_only_once():
    from review_writer_api.domain_services.drafts import DraftsService
    service = object.__new__(DraftsService)
    def evaluation(pid, score):
        return {'evaluation_scope': 'single_paragraph', 'paragraph_id': pid,
                'paragraph_score': {'paragraph_id': pid, 'score': score, 'route': 'pass'}}
    source = {'score': 70, 'paragraph_scores': [
        {'paragraph_id': 'p1', 'score': 70}, {'paragraph_id': 'p2', 'score': 60},
        {'paragraph_id': 'p3', 'score': 80}]}
    change = {'paragraph_id': 'p1', 'candidate_evaluation': evaluation('p1', 80),
              'dependent_evaluations': {'p2': evaluation('p2', 90)}}
    quality, count = service._optimization_quality_from_scored_changes({'source_quality': source}, [change])
    assert count == 1
    assert {r['paragraph_id']: r['score'] for r in quality['paragraph_scores']} == {'p1': 80, 'p2': 90, 'p3': 80}
    assert quality['score'] == 83.33
    assert source['paragraph_scores'][1]['score'] == 60


def test_joint_candidate_is_never_auto_accepted():
    from review_writer_api.domain_services.drafts import DraftsService
    service = object.__new__(DraftsService)
    service._read_json = Mock(return_value=({'entries': {'proposal': {'status': 'pending', 'changes': [
        {'paragraph_id': 'p1', 'requires_manual_confirmation': True, 'argument_revisions': [delta()]}]}}}, None))
    service._read_text = Mock(return_value=('Draft', SimpleNamespace(metadata={})))
    result = service.auto_apply_optimization_proposal(SimpleNamespace(), 'project', 'proposal', revision=1)
    assert result == {'auto_applied': False, 'auto_apply_status': 'manual_review_required'}


def test_published_draft_points_to_matching_overlay_for_restore(tmp_path):
    from review_writer_api.domain_services.drafts import DraftsService, DRAFT_DOCUMENT, DRAFT_OVERLAYS, DRAFT_QUALITY
    service = object.__new__(DraftsService)
    service.repository = Mock()
    service.repository.create_stage_run.return_value = SimpleNamespace(id='run')
    service.artifacts = Mock()
    service.artifacts.stage_run_directory.return_value = tmp_path
    records = {}
    def publish(*args, **kwargs):
        name = kwargs['logical_name']
        records[name] = kwargs['metadata']
        return SimpleNamespace(id='new:' + name)
    service.artifacts.publish.side_effect = publish
    service._publish_files(SimpleNamespace(user_id='owner'), 'project', {
        DRAFT_DOCUMENT: (b'Draft', 'markdown'), DRAFT_OVERLAYS: (b'{}', 'json'),
        DRAFT_QUALITY: (b'{}', 'json')}, expected_revision=1,
        metadata={'source_rewrite_overlay_artifact_id': 'previous'})
    assert records[DRAFT_DOCUMENT]['source_rewrite_overlay_artifact_id'] == 'new:' + DRAFT_OVERLAYS
    assert records[DRAFT_QUALITY]['source_rewrite_overlay_artifact_id'] == 'new:' + DRAFT_OVERLAYS
    assert service.repository.promote_stage_artifacts_atomically.call_count == 1


def load_script(name):
    path = Path(__file__).resolve().parents[1] / 'skills/review-first-draft-feedback-loop/scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location('test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_consumes_effective_claims_and_preserves_overlays_after_normal_rewrite(tmp_path):
    fb = load_script('feedback_loop')
    section = tmp_path / '02_section_drafting'
    first = tmp_path / '04_first_draft'
    section.mkdir(); first.mkdir()
    fb.write_json(section / 'writing_plan.json', plan())
    fb.write_json(first / 'feedback_loop_rewrites.json', {'argument_revisions': {'C': delta()}})
    assert fb.claim_evidence_contract(tmp_path)['claims']['C']['proposition'] == 'Bounded assertion'
    fb.record_rewrite_overlay(tmp_path, 'other-p1', 'original', 'edited')
    assert fb.claim_evidence_contract(tmp_path)['claims']['C']['proposition'] == 'Bounded assertion'
    assert fb.draft_writing_plan(tmp_path, effective=False) == plan()
    # Hosted tasks also materialize an effective plan for Final consumers.
    # Scoring must start from the separate baseline, not apply the delta twice.
    fb.write_json(section / 'baseline_writing_plan.json', plan())
    fb.write_json(section / 'writing_plan.json', fb.draft_writing_plan(tmp_path))
    assert fb.claim_evidence_contract(tmp_path)['claims']['C']['proposition'] == 'Bounded assertion'


@pytest.mark.parametrize('bad_first', [False, 'provider', 'invalid'])
def test_joint_worker_retains_failed_group_and_continues_independent_paragraph(tmp_path, bad_first):
    fb = load_script('feedback_loop')
    runner = load_script('local_revision')
    project = tmp_path / 'review-projects' / 'isolated'
    first, section = project / '04_first_draft', project / '02_section_drafting'
    first.mkdir(parents=True); section.mkdir()
    fb.write_json(section / 'writing_plan.json', plan())
    paragraphs = [{'paragraph_id': p, 'text': 'Original ' + p + '.'} for p in ('alpha-p1', 'alpha-p2', 'beta-p1')]
    original = '# Review\n\n' + '\n\n'.join(p['text'] + '\n<!-- paragraph_id: ' + p['paragraph_id'] + ' -->' for p in paragraphs) + '\n'
    (first / 'first_draft.md').write_text(original, encoding='utf-8')
    fb.write_json(first / 'local_revision_issues.json', [
        {'paragraph_id': 'alpha-p1', 'repair_class': 'planning_adjustment', 'claim_ids': ['C']},
        {'paragraph_id': 'beta-p1', 'repair_class': 'draft_rewrite', 'rewrite_eligible': True},
    ])
    evidence = {p['paragraph_id']: {'paper_ids': ['paper'], 'original_source_ready': True,
        'evidence': [{'paper_id': 'paper', 'original_passages': [{'ref': 'R', 'text': 'Bounded assertion'}]}]} for p in paragraphs}
    scores = [{'paragraph_id': p['paragraph_id'], 'score': 80, 'source_check_status': 'verified', 'source_evidence_refs': ['R']} for p in paragraphs]
    source = {'total_score': 80, 'paragraph_scores': scores}
    fb.evaluate_current_draft = Mock(return_value=({}, source, {}, paragraphs, evidence))
    fb.evaluate_changed_paragraphs = Mock(side_effect=lambda *args, **kw: ({}, {'paragraph_scores': [s for s in scores if s['paragraph_id'] in args[4]]}, evidence))
    # This orchestration test stubs syntax integrity only; the existing guard
    # suite tests chemical numbers, figure metadata and protected references.
    fb.validate_rewrite_report = Mock(return_value=([], []))
    first_response = {'argument_revisions': [delta()], 'paragraphs': [{'paragraph_id': 'alpha-p1', 'text': 'Bounded result.'}]}
    if bad_first == 'invalid':
        first_response['argument_revisions'][0]['claim_id'] = 'unregistered'
    fb.call_json_model = Mock(side_effect=[RuntimeError('provider unavailable') if bad_first == 'provider' else first_response,
        {'paragraphs': [{'paragraph_id': 'beta-p1', 'text': 'Independent improvement.'}]}])
    args = SimpleNamespace(review_root=str(tmp_path), project_id='isolated', min_case_words=1, max_case_words=100)
    rubric = fb.read_json(Path(fb.__file__).parents[1] / 'references/unified_rubric.json', {})
    runner.run(args, rubric, fb)
    report = fb.read_json(first / 'batch_review_candidates.json', {})
    assert fb.call_json_model.call_count == 2
    assert len(report['changes']) == (1 if bad_first else 2)
    assert 'Independent improvement.' in report['candidate_draft_text']
    assert (section / 'writing_plan.json').read_text(encoding='utf-8') == json.dumps(plan(), ensure_ascii=False, indent=2) + '\n'
    assert bool(report['excluded']) == bool(bad_first)
    history = fb.read_json(fb.status_path(project), {}).get('repair_history') or {}
    assert bool(history) == (bad_first == 'invalid')
    assert report['full_draft_evaluated'] is True
