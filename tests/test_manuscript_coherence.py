import pytest
import json

from review_writer_core.manuscript_coherence import (
    manuscript_snapshot, plan_manuscript, audit_candidate, candidate_check_current,
    paragraph_records, current_records, assembly_records,
)


def test_records_expire_only_for_changed_prose_and_rebind_only_at_assembly():
    original = rows()
    writing = {'paragraphs': [{**p, 'reader_takeaway': 'Bounded finding',
                              'evidence_uses': [{'source_refs': ['A:1'], 'purpose': 'Scope'}]}
                             for p in original]}
    records = paragraph_records(original, writing)
    rendered = [{**p, 'text': p['text'] + ' [1]'} for p in original]
    rebound = assembly_records(rendered, [{'paragraphs': original, 'content_records': records}])
    assert len(current_records(rendered, rebound)) == 2
    changed = [{**rendered[0], 'text': 'User revised this paragraph.'}, rendered[1]]
    assert list(current_records(changed, rebound)) == ['S2-p1']
    assert assembly_records(rendered, [{'paragraphs': changed, 'content_records': records}]) == {}
    assert current_records(original, {}) == {}
    assert paragraph_records(original, {}) == {}


def test_long_snapshot_covers_later_chapters_without_repeating_prose():
    paragraphs = [{'paragraph_id': f'S{s}-p{p}', 'paragraph_key': f'{s}-{p}',
                   'text': f'Chapter {s} point {p}. ' + 'bounded result ' * 45}
                  for s in range(1, 8) for p in range(1, 11)]
    sections = [{'section_id': f'S{s}', 'title': f'Chapter {s}',
                 'paragraphs': paragraphs[(s-1)*10:s*10]} for s in range(1, 8)]
    snapshot = manuscript_snapshot(paragraphs, sections, limit=9000, repetition={'hints': []})
    assert all(c['included'] >= 1 for c in snapshot['coverage'])
    assert len(snapshot['paragraphs']) < len(paragraphs)
    assert len(json.dumps(snapshot, ensure_ascii=False)) <= 9000
    assert len({p['paragraph_id'] for p in snapshot['paragraphs']}) == len(snapshot['paragraphs'])
    original = {p['paragraph_id']: p['text'] for p in paragraphs}
    assert all(p['text'] == original[p['paragraph_id']] for p in snapshot['paragraphs'])


def test_full_short_snapshot_uses_prose_not_duplicate_summaries_and_refs_only_hints():
    records = paragraph_records(rows(), {'paragraphs': [dict(p, reader_takeaway='Same study, different role') for p in rows()]})
    hint = {'kind': 'wording_overlap', 'paragraph_id': 'S1-p1', 'other_paragraph_id': 'S2-p1',
            'excerpt': 'Duplicated text should not be transmitted'}
    snapshot = manuscript_snapshot(rows(), records=records, repetition={'hints': [hint, hint]})
    assert snapshot['records'] == {}
    assert len(snapshot['repetition_groups']) == 1
    assert 'excerpt' not in snapshot['repetition_groups'][0]
    prompts = []
    def call(prompt, **kwargs):
        prompts.append(prompt)
        return {'issues': []}
    assert plan_manuscript(snapshot, call)['issues'] == []
    assert 'shared paper or terminology alone is not duplication' in prompts[0]
    assert 'do not force transitions' in prompts[0]


def test_empty_snapshot_never_calls_model_and_duplicate_issues_are_coalesced():
    assert plan_manuscript(manuscript_snapshot([]), lambda *a, **k: pytest.fail('no call'))['status'] == 'complete'
    issue = {'paragraph_id': 'S1-p1', 'excerpt': rows()[0]['text'], 'instruction': 'Keep conditions.', 'dependencies': []}
    assert len(plan_manuscript(manuscript_snapshot(rows()), lambda *a, **k: {'issues': [issue, issue]})['issues']) == 1


def rows():
    return [{"paragraph_id": "S1-p1", "paragraph_key": "a", "text": "Only under these conditions."},
            {"paragraph_id": "S2-p1", "paragraph_key": "b", "text": "Results differ across substrates."}]


def test_plan_requires_exact_location_and_known_dependencies():
    snapshot = manuscript_snapshot(rows())
    issue = {"paragraph_id": "S1-p1", "excerpt": "Only under these conditions.",
             "instruction": "Keep scope explicit.", "dependencies": ["S2-p1"]}
    assert plan_manuscript(snapshot, lambda *a, **k: {"issues": [issue]})["status"] == "complete"
    for change in ({"excerpt": "invented"}, {"dependencies": ["unknown"]}, {"paragraph_id": "unknown"}):
        with pytest.raises(ValueError):
            plan_manuscript(snapshot, lambda *a, **k: {"issues": [{**issue, **change}]})


def test_long_manuscript_plan_cannot_claim_full_coverage():
    paragraphs = [dict(p, text=p['text'] * 18) for p in rows()]
    snapshot = manuscript_snapshot(paragraphs, limit=1200)
    assert len(snapshot["paragraphs"]) == 1
    assert plan_manuscript(snapshot, lambda *a, **k: {"issues": []})["status"] == "partial"


def test_correct_ref_does_not_override_semantic_failure_and_checks_are_text_bound():
    sources = [{"ref": "A:1", "text": "Only under these conditions."}]
    for response in ({"status": "unsupported", "preserves_information": True, "source_refs": ["A:1"]},
                     {"status": "supported", "preserves_information": False, "source_refs": ["A:1"]},
                     {"status": "supported", "preserves_information": True, "source_refs": ["fake"]}, {}):
        assert audit_candidate("Only under these conditions.", "Always works.", sources,
                               lambda *a, **k: response)["status"] == "unresolved"
    candidate = "Under these conditions only."
    check = audit_candidate("Only under these conditions.", candidate, sources,
                            lambda *a, **k: {"status": "supported", "preserves_information": True, "source_refs": ["A:1"]})
    saved = {"candidate_text": candidate, "sources": sources, "automatic_source_check": check}
    assert candidate_check_current(saved)
    assert not candidate_check_current({**saved, "candidate_text": "Always works."})
    assert not candidate_check_current({**saved, "sources": [{"ref": "A:1", "text": "Changed source"}]})
