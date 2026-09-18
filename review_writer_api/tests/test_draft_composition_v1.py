from fastapi.testclient import TestClient
from unittest.mock import patch

from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase
from review_writer_api.tests import test_drafts_v1
from review_writer_api.errors import WorkflowConflict
from review_writer_core.draft_composition import replace_section, section_text, section_span, replace_manuscript_fields


class DraftCompositionTests(NativeFigureApiTestCase):
    prepare_draft = test_drafts_v1.DraftsV1Tests.prepare_draft

    def extra_native_workflow_overrides(self):
        return {'draft.synthesis': lambda context, payload: {
            'sections': {r: 'Generated '+r+'.' for r in payload['roles']},
            'keywords': ['review', 'synthesis'], 'title': 'Comparative Strategies and Evidence in Synthesis', 'warnings': []}}

    def initialize(self, client):
        result = self.prepare_draft(client)
        self.assertEqual('succeeded', self.wait_job(client, result['synthesis_job_id'])['status'])
        return self.app.state.drafts_service

    def test_abstract_payload_does_not_load_full_evidence_archive(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            with patch.object(service, 'compatibility_payload', side_effect=AssertionError('Archive must not be loaded')):
                payload = service.synthesis_payload(self.first, self.project_id, 'abstract')
            self.assertNotIn('source_evidence', payload)
            self.assertTrue(payload['draft_text'])
            self.assertIn('abstract', payload['source_signatures'])

    def test_overview_preview_history_requires_explicit_adoption(self):
        from PIL import Image
        from review_writer_core.workflow.artifacts import FINAL_OVERVIEW_IMAGE
        with TestClient(self.app) as client:
            draft = self.initialize(client)
            final = self.app.state.final_service
            path = self.app.state.artifact_service.workspace_manager.user_root(self.first.user_id) / 'overview-preview.png'
            Image.new('RGB', (64, 32), 'green').save(path)
            payload = {**final.overview_payload(self.first, self.project_id), 'preview_only': True,
                       'generation_instructions': 'Emphasize strategies'}
            built = {'output_path': str(path), 'editable_text': {'title': 'Strategies'}, 'report': {}}
            result = final.publish_overview(self.first, self.project_id, payload, built)
            self.assertTrue(result['candidate_pending'])
            self.assertIsNone(final._artifact(self.first, self.project_id, FINAL_OVERVIEW_IMAGE))
            history = final.overview_history(self.first, self.project_id)
            self.assertEqual('Emphasize strategies', history[0]['instructions'])
            response = client.post(f'/api/v1/projects/{self.project_id}/draft/overview/adopt', json={
                'image_id': result['overview_artifact_id'], 'title': 'Edited caption', 'revision': final._revision(self.first, self.project_id)})
            self.assertEqual(200, response.status_code, response.text)
            self.assertIn('Edited caption', draft.get(self.first, self.project_id)['manuscript_preview_md'])
            self.assertTrue(final.overview_history(self.first, self.project_id)[0]['selected'])
            self.assertEqual(1, len(final.overview_history(self.first, self.project_id)))
            selected_id = final._artifact(self.first, self.project_id, FINAL_OVERVIEW_IMAGE).id
            Image.new('RGB', (64, 32), 'blue').save(path)
            another = final.publish_overview(self.first, self.project_id,
                {**final.overview_payload(self.first, self.project_id), 'preview_only': True}, built)
            self.assertEqual(selected_id, final._artifact(self.first, self.project_id, FINAL_OVERVIEW_IMAGE).id)
            self.assertEqual(2, len(final.overview_history(self.first, self.project_id)))
            self.assertNotIn(another['overview_artifact_id'], draft.get(self.first, self.project_id)['manuscript_preview_md'])
            response = client.post(f'/api/v1/projects/{self.project_id}/draft/overview/adopt', json={
                'image_id': result['overview_artifact_id'], 'title': 'Stale overwrite', 'revision': 0})
            self.assertEqual(409, response.status_code)
            response = client.post(f'/api/v1/projects/{self.project_id}/draft/overview/adopt', json={
                'image_id': 'not-this-project', 'title': 'Invalid', 'revision': final._revision(self.first, self.project_id)})
            self.assertEqual(422, response.status_code)

    def test_synthesis_candidate_can_be_edited_when_adopted(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            payload = service.synthesis_payload(self.first, self.project_id, 'abstract')
            result = service.publish_synthesis(self.first, self.project_id, payload,
                {'sections': {'abstract': 'Generated replacement.'}})
            candidate = next(c for c in service.get(self.first, self.project_id)['rewrite_candidates'] if c['candidate_id'] == result['candidate_ids'][0])
            response = client.post(f'/api/v1/projects/{self.project_id}/draft/synthesis-candidates/{candidate["candidate_id"]}/accept',
                json={'text': 'Author edited summary.', 'base_text_sha256': candidate['base_text_sha256']})
            self.assertEqual(200, response.status_code, response.text)
            self.assertIn('Author edited summary.', service.get(self.first, self.project_id)['first_draft_md'])

    def test_conclusion_payload_does_not_load_retrieval_archive(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            with patch.object(service, 'compatibility_payload', side_effect=AssertionError('Archive must not be loaded')):
                payload = service.synthesis_payload(self.first, self.project_id, 'conclusion')
            self.assertNotIn('source_evidence', payload)
            self.assertIn('sections', payload['conclusion_context'])

    def test_missing_keywords_fill_without_overwriting_abstract_or_manual_clear(self):
        from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT
        with TestClient(self.app) as client:
            service = self.initialize(client)
            state = service.get(self.first, self.project_id)
            artifact = service._artifact(self.first, self.project_id, DRAFT_MANUSCRIPT)
            # Old machine-produced draft with an empty Keywords line (not an explicit user omission).
            empty = replace_manuscript_fields(state['first_draft_md'], state['manuscript_fields']['title'], [])
            service._publish_files(self.first, self.project_id, {DRAFT_MANUSCRIPT: (empty.encode(), 'markdown')},
                expected_revision=state['revision'], metadata=artifact.metadata)
            payload = service.synthesis_payload(self.first, self.project_id, 'abstract')
            self.assertTrue(payload['generate_keywords'])
            service.publish_synthesis(self.first, self.project_id, payload,
                {'sections': {'abstract': 'New candidate summary.'}, 'keywords': ['evidence', 'synthesis']})
            state = service.get(self.first, self.project_id)
            self.assertEqual(['evidence', 'synthesis'], state['manuscript_fields']['keywords'])
            self.assertIn('Generated abstract.', state['first_draft_md'])
            self.assertFalse(state['synthesis_stale'])
            self.assertFalse(service.synthesis_payload(self.first, self.project_id, 'abstract')['generate_keywords'])
            response = client.put(f'/api/v1/projects/{self.project_id}/draft/manuscript-fields', json={
                'revision': state['revision'], 'title': state['manuscript_fields']['title'], 'keywords': []})
            self.assertEqual(200, response.status_code)
            self.assertFalse(service.synthesis_payload(self.first, self.project_id, 'abstract')['generate_keywords'])
            # A result from before the manual clear cannot fill the field back in.
            payload['candidate_seed'] = 'late-keywords'
            service.publish_synthesis(self.first, self.project_id, payload,
                {'sections': {'abstract': 'Late summary.'}, 'keywords': ['unwanted']})
            self.assertEqual([], service.get(self.first, self.project_id)['manuscript_fields']['keywords'])

    def test_explicit_empty_keywords_save_records_omission_even_without_text_change(self):
        from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT
        with TestClient(self.app) as client:
            service = self.initialize(client)
            state = service.get(self.first, self.project_id)
            artifact = service._artifact(self.first, self.project_id, DRAFT_MANUSCRIPT)
            empty = replace_manuscript_fields(state['first_draft_md'], state['manuscript_fields']['title'], [])
            service._publish_files(self.first, self.project_id, {DRAFT_MANUSCRIPT: (empty.encode(), 'markdown')},
                expected_revision=state['revision'], metadata=artifact.metadata)
            state = service.get(self.first, self.project_id)
            response = client.put(f'/api/v1/projects/{self.project_id}/draft/manuscript-fields', json={
                'revision': state['revision'], 'title': state['manuscript_fields']['title'], 'keywords': []})
            self.assertEqual(200, response.status_code)
            self.assertTrue(service._artifact(self.first, self.project_id, DRAFT_MANUSCRIPT).metadata['keywords_user_omitted'])
            self.assertFalse(service.synthesis_payload(self.first, self.project_id, 'abstract')['generate_keywords'])

    def test_legacy_topic_title_is_replaced_with_abstract_but_custom_title_is_preserved(self):
        from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT
        with TestClient(self.app) as client:
            service = self.initialize(client)
            state = service.get(self.first, self.project_id)
            project = service._owned_project(self.first, self.project_id)
            topic = str(project.topic or project.slug or self.project_id)
            old = replace_manuscript_fields(state['first_draft_md'], topic, ['review'])
            artifact = service._artifact(self.first, self.project_id, DRAFT_MANUSCRIPT)
            service._publish_files(self.first, self.project_id, {DRAFT_MANUSCRIPT: (old.encode(), 'markdown')},
                expected_revision=state['revision'], metadata={**artifact.metadata, 'title_user_modified': False})
            payload = service.synthesis_payload(self.first, self.project_id, 'abstract')
            self.assertTrue(payload['generate_title'])
            service.publish_synthesis(self.first, self.project_id, payload,
                {'sections': {'abstract': 'Updated summary.'}, 'title': 'Evidence and Strategies Across Reviewed Studies'})
            state = service.get(self.first, self.project_id)
            self.assertEqual('Evidence and Strategies Across Reviewed Studies', state['manuscript_fields']['title'])
            self.assertIn('Generated abstract.', state['first_draft_md'])
            self.assertFalse(service.synthesis_payload(self.first, self.project_id, 'abstract')['generate_title'])
            service.save_text(self.first, self.project_id, revision=state['revision'],
                text=replace_manuscript_fields(state['first_draft_md'], topic, ['review']))
            self.assertFalse(service.synthesis_payload(self.first, self.project_id, 'abstract')['generate_title'])

    def test_initial_title_does_not_overwrite_concurrent_user_title(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            state = service.get(self.first, self.project_id)
            text = state['first_draft_md']
            start, end = section_span(text, 'abstract')
            service.save_text(self.first, self.project_id, revision=state['revision'], text=text[:start]+text[end:])
            payload = service.synthesis_payload(self.first, self.project_id, 'initial', initial=True)
            state = service.get(self.first, self.project_id)
            service.save_text(self.first, self.project_id, revision=state['revision'],
                              text=replace_manuscript_fields(state['first_draft_md'], 'My Carefully Edited Review Title', ['review']))
            service.publish_synthesis(self.first, self.project_id, payload,
                                      {'sections': {'abstract': 'Late summary.'}, 'title': 'Machine Generated Review Title'})
            self.assertEqual('My Carefully Edited Review Title', service.get(self.first, self.project_id)['manuscript_fields']['title'])

    def test_initial_completion_idempotency_editability_and_reassembly(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            state = service.get(self.first, self.project_id)
            self.assertFalse(state['synthesis_stale'])
            self.assertEqual('Comparative Strategies and Evidence in Synthesis', state['manuscript_fields']['title'])
            self.assertEqual(1, state['first_draft_md'].count('## Abstract'))
            self.assertEqual(1, state['first_draft_md'].count('## Conclusion'))
            self.assertIn('synthesis:abstract', [s['section_id'] for s in state['sections']])
            repeated = client.post(f'/api/v1/projects/{self.project_id}/draft/assemble')
            self.assertEqual(200, repeated.status_code, repeated.text)
            self.assertNotIn('synthesis_job_id', repeated.json())
            self.assertEqual(1, service.get(self.first, self.project_id)['first_draft_md'].count('## Abstract'))

    def test_manual_update_is_candidate_and_rejects_changed_target(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            payload = service.synthesis_payload(self.first, self.project_id, 'conclusion')
            before = service.get(self.first, self.project_id)
            result = service.publish_synthesis(self.first, self.project_id, payload,
                {'sections': {'conclusion': 'First new paragraph.\n\nSecond new paragraph.'}})
            state = service.get(self.first, self.project_id)
            self.assertEqual(before['first_draft_md'], state['first_draft_md'])
            cid = result['candidate_ids'][0]
            service.decide_synthesis(self.first, self.project_id, cid, decision='accept')
            adopted = service.get(self.first, self.project_id)
            self.assertEqual(2, len(next(s for s in adopted['sections'] if s['section_id'] == 'synthesis:conclusion')['paragraphs']))
            self.assertTrue(adopted['synthesis_stale'])  # Abstract depends on conclusion.
            stale = service.publish_synthesis(self.first, self.project_id, payload, {'sections': {'conclusion': 'Older.'}})
            self.assertEqual([cid], stale['candidate_ids'])
            payload2 = service.synthesis_payload(self.first, self.project_id, 'conclusion')
            pending = service.publish_synthesis(self.first, self.project_id, payload2, {'sections': {'conclusion': 'Candidate.'}})
            current = service.get(self.first, self.project_id)
            service.save_text(self.first, self.project_id, revision=current['revision'],
                text=replace_section(current['first_draft_md'], 'conclusion', 'Human revision.'))
            with self.assertRaises(WorkflowConflict):
                service.decide_synthesis(self.first, self.project_id, pending['candidate_ids'][0], decision='accept')

    def test_manual_endpoint_replays_key_and_does_not_overwrite(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            before = service.get(self.first, self.project_id)['first_draft_md']
            path = f'/api/v1/projects/{self.project_id}/draft/synthesis/abstract'
            a = client.post(path, headers=self.headers('same-synthesis'))
            self.assertEqual(202, a.status_code, a.text)
            self.wait_job(client, a.json()['id'])
            b = client.post(path, headers=self.headers('same-synthesis'))
            self.assertEqual(a.json()['id'], b.json()['id'])
            self.assertEqual(before, service.get(self.first, self.project_id)['first_draft_md'])

    def test_initial_result_does_not_replace_concurrent_manual_section(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            state = service.get(self.first, self.project_id)
            # Emulate an initialization snapshot taken before Abstract existed.
            payload = service.synthesis_payload(self.first, self.project_id, 'abstract')
            payload['initial'] = True
            payload['base_sections']['abstract'] = ''
            result = service.publish_synthesis(self.first, self.project_id, payload,
                {'sections': {'abstract': 'Late background result.'}, 'title': 'Unwanted Replacement of the Current Title'})
            after = service.get(self.first, self.project_id)
            self.assertEqual(state['first_draft_md'], after['first_draft_md'])
            self.assertEqual('stale', next(c for c in after['rewrite_candidates'] if c['candidate_id'] == result['candidate_ids'][0])['status'])

    def test_overview_before_approval_preserves_selection_and_conflicting_result(self):
        from PIL import Image
        from review_writer_core.workflow.artifacts import FINAL_OVERVIEW_IMAGE
        with TestClient(self.app) as client:
            draft = self.initialize(client)
            final = self.app.state.final_service
            path = final.artifacts.workspace_manager.user_root(self.first.user_id) / 'overview-fixture.png'
            Image.new('RGB', (64, 32), 'white').save(path)
            built = {'output_path': str(path), 'editable_text': {'title': 'Overview', 'subtitle': '', 'labels': []}, 'report': {}}
            payload = final.overview_payload(self.first, self.project_id)
            first = final.publish_overview(self.first, self.project_id, payload, built)
            self.assertFalse(first['candidate_pending'])
            queued = final.overview_payload(self.first, self.project_id)
            current = draft.get(self.first, self.project_id)
            image_url = '/api/v1/artifacts/'+first['overview_artifact_id']+'/content'
            self.assertEqual(1, current['manuscript_preview_md'].count(image_url))
            self.assertIn('*Figure 1. Overview.*', current['manuscript_preview_md'])
            self.assertIn('*Figure 2.', current['manuscript_preview_md'])
            self.assertIn('(Figure 2)', current['manuscript_preview_md'])
            self.assertIn('*Figure 1.', current['first_draft_md'])
            self.assertNotIn(image_url, current['first_draft_md'])
            self.assertFalse(current['synthesis_stale'])
            draft.save_text(self.first, self.project_id, revision=current['revision'],
                text=current['first_draft_md'].replace('Grounded paragraph', 'Human-edited paragraph'))
            Image.new('RGB', (64, 32), 'blue').save(path)
            late = final.publish_overview(self.first, self.project_id, queued, built)
            self.assertTrue(late['candidate_pending'])
            preview = draft.get(self.first, self.project_id)['manuscript_preview_md']
            self.assertEqual(1, preview.count(image_url))
            self.assertNotIn(late['overview_artifact_id'], preview)
            self.assertEqual(first['overview_artifact_id'], final._artifact(self.first, self.project_id, FINAL_OVERVIEW_IMAGE).id)
            self.assertEqual(200, client.get('/api/v1/artifacts/'+late['overview_artifact_id']+'/content').status_code)
            snapshot = final.get(self.first, self.project_id)
            caption = final.save_overview_text(self.first, self.project_id, revision=snapshot['revision'],
                title='Revised caption', subtitle='', labels=[])
            self.assertTrue(caption['overview_text_artifact_id'])
            self.assertIn('Revised caption', draft.get(self.first, self.project_id)['manuscript_preview_md'])

    def test_legacy_content_is_explicit_idempotent_candidate_not_second_final_section(self):
        import json
        from review_writer_core.workflow.artifacts import FINAL_FRONT_MATTER
        with TestClient(self.app) as client:
            draft = self.initialize(client)
            final = self.app.state.final_service
            final._publish_files(self.first, self.project_id,
                {FINAL_FRONT_MATTER: (json.dumps({'abstract': 'Legacy summary.', 'keywords': ['legacy'], 'title': 'Legacy title'}).encode(), 'json')},
                expected_revision=final._revision(self.first, self.project_id), metadata={'operation': 'legacy-fixture'})
            before = draft.get(self.first, self.project_id)
            candidate = next(c for c in before['rewrite_candidates'] if c.get('legacy_artifact_id'))
            draft.decide_dialogue(self.first, self.project_id, candidate['candidate_id'], decision='accept',
                expected_base_text_sha256=candidate['base_text_sha256'])
            after = draft.get(self.first, self.project_id)
            self.assertIn('Legacy summary.', section_text(after['first_draft_md'], 'abstract'))
            self.assertEqual(1, after['first_draft_md'].count('## Abstract'))
            self.assertEqual(1, len([c for c in after['rewrite_candidates'] if c.get('legacy_artifact_id')]))
            draft.approve(self.first, self.project_id, revision=after['revision'], override_low_score=False, override_reason='')
            final.build(self.first, self.project_id)
            text = final.get(self.first, self.project_id)['final_draft_md']
            self.assertEqual(1, text.count('## Abstract'))
            self.assertEqual(1, text.count('## Conclusion'))

    def test_manuscript_fields_are_draft_owned(self):
        with TestClient(self.app) as client:
            service = self.initialize(client)
            before = service.get(self.first, self.project_id)
            response = client.put(f'/api/v1/projects/{self.project_id}/draft/manuscript-fields', json={
                'revision': before['revision'], 'title': 'Saved manuscript title', 'keywords': ['one', 'two']})
            self.assertEqual(200, response.status_code, response.text)
            after = service.get(self.first, self.project_id)
            self.assertEqual('Saved manuscript title', after['manuscript_fields']['title'])
            self.assertEqual(['one', 'two'], after['manuscript_fields']['keywords'])
            self.assertEqual(section_text(before['first_draft_md'], 'abstract'), section_text(after['first_draft_md'], 'abstract'))
