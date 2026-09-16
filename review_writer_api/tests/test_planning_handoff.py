"""Extraction/planning handoff accepts only the expected immutable publication."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from review_writer_api.domain_services.actions.planning.blueprint import PlanningBlueprintActionsMixin
from review_writer_api.errors import WorkflowConflict
from review_writer_api.planning_jobs import queue_matrix_enrichment
from review_writer_api.job_handlers.stage_execution import register_planning_handlers


class PlanningHandoffTests(TestCase):
    def setUp(self):
        self.principal = SimpleNamespace(user_id="owner")
        self.service = Mock()
        self.prepared = {"await_matrix_job_id": "job", "blueprint_state_exists": True,
            "blueprint_revision": 2, "matrix_revision": 3, "base_blueprint_artifact_id": "old-plan",
            "section_blueprint": {"source_matrix_artifact_id": "before", "source_outline_artifact_id": "outline"}}
        self.job = SimpleNamespace(project_id="project", job_type="matrix.enrich", status="succeeded",
            payload={"source_matrix_artifact_id": "before"},
            result={"matrix_artifact_id": "after", "matrix_revision": 4, "changed_paper_ids": ["P1"]})
        self.service.repository.get_job.return_value = self.job
        self.service.repository.get_current_job.return_value = None
        self.service.blueprint_job_payload.return_value = {"fresh": True}

    def resume(self):
        return PlanningBlueprintActionsMixin.resume_blueprint_after_matrix(
            self.service, self.principal, "project", self.prepared)

    def test_reprepares_using_published_matrix_and_preserves_original_request(self):
        before = deepcopy(self.prepared)
        self.assertEqual({"fresh": True}, self.resume())
        checked = self.service.validate_prepared_blueprint.call_args.args[-1]
        self.assertEqual("after", checked["section_blueprint"]["source_matrix_artifact_id"])
        self.assertEqual(3, checked["blueprint_revision"])
        self.assertEqual(4, checked["matrix_revision"])
        self.assertIsNone(checked["base_blueprint_artifact_id"])
        self.assertEqual("outline", checked["section_blueprint"]["source_outline_artifact_id"])
        self.assertEqual(before, self.prepared)

    def test_failure_cancellation_or_foreign_dependency_cannot_start_planner(self):
        for status in ("failed", "cancelled", "running", "queued"):
            self.job.status = status
            with self.assertRaises(WorkflowConflict):
                self.resume()
        self.job.status = "succeeded"
        self.job.project_id = "another"
        with self.assertRaises(WorkflowConflict):
            self.resume()
        self.service.blueprint_job_payload.assert_not_called()

    def test_user_edit_conflict_is_not_silently_rebased(self):
        self.service.validate_prepared_blueprint.side_effect = WorkflowConflict("Inputs changed")
        with self.assertRaises(WorkflowConflict):
            self.resume()
        self.service.blueprint_job_payload.assert_not_called()

    def test_recovered_extraction_can_resume_the_original_planner(self):
        self.job.id = "job"
        self.job.status = "failed"
        retry = SimpleNamespace(id="retry", retry_of_job_id="job", project_id="project",
            job_type="matrix.enrich", status="succeeded", payload=self.job.payload, result=self.job.result)
        self.service.repository.get_current_job.return_value = retry
        self.assertEqual({"fresh": True}, self.resume())

    def test_current_facts_do_not_increment_revisions(self):
        self.job.result = {"matrix_artifact_id": "before", "status": "current"}
        self.resume()
        checked = self.service.validate_prepared_blueprint.call_args.args[-1]
        self.assertEqual(2, checked["blueprint_revision"])
        self.assertEqual(3, checked["matrix_revision"])

    def test_fact_verification_publication_also_rebases_blueprint(self):
        self.job.result = {"matrix_artifact_id": "after", "matrix_revision": 4,
            "blueprint_invalidated": True}
        self.resume()
        checked = self.service.validate_prepared_blueprint.call_args.args[-1]
        self.assertEqual(3, checked["blueprint_revision"])

    def test_queue_is_deferred_and_reuses_active_job(self):
        jobs = Mock()
        self.service._matrix.return_value = ({}, SimpleNamespace(id="before"))
        jobs.repository.get_current_job.return_value = None
        queue_matrix_enrichment(self.service, jobs, self.principal, "project")
        payload = jobs.submit.call_args.kwargs["payload"]
        self.assertTrue(payload["prepare_on_start"])
        self.service.matrix_enrichment_payload.assert_not_called()
        jobs.submit.reset_mock()
        self.job.status = "running"
        jobs.repository.get_current_job.return_value = self.job
        self.assertIs(self.job, queue_matrix_enrichment(self.service, jobs, self.principal, "project"))
        jobs.submit.assert_not_called()

    def test_worker_reuses_current_facts_without_model_calls(self):
        jobs, builder = Mock(), Mock()
        register_planning_handlers(self.service, jobs, {"matrix.enrich": builder})
        handler = jobs.register_handler.call_args.args[1]
        self.service.matrix_enrichment_payload.return_value = {
            "source_matrix_artifact_id": "before", "pending_paper_count": 0}
        context = Mock(user_id="owner", project_id="project", retry_of_job_id=None)
        result = handler(context, {"prepare_on_start": True, "source_matrix_artifact_id": "before",
            "selected_paper_ids": ["P1"], "force_refresh": False})
        self.assertEqual("current", result["status"])
        self.assertEqual("before", result["matrix_artifact_id"])
        builder.assert_not_called()
        self.service.publish_matrix_enrichment.assert_not_called()
        self.assertEqual(["P1"], self.service.matrix_enrichment_payload.call_args.kwargs["selected_paper_ids"])

    def test_resume_queue_only_reuses_checkpoint_for_same_matrix_without_force(self):
        jobs = Mock()
        self.service._matrix.return_value = ({}, SimpleNamespace(id="before"))
        self.job.id = "previous"
        self.job.status = "failed"
        self.job.result = {"section_checkpoint": {"papers": {"P1": {}}}}
        jobs.repository.get_current_job.return_value = self.job
        queue_matrix_enrichment(self.service, jobs, self.principal, "project", paper_ids=["P1"])
        self.assertEqual("previous", jobs.submit.call_args.kwargs["payload"]["resume_from_job_id"])
        queue_matrix_enrichment(self.service, jobs, self.principal, "project", force=True)
        self.assertNotIn("resume_from_job_id", jobs.submit.call_args.kwargs["payload"])
        self.job.payload = {"source_matrix_artifact_id": "older"}
        queue_matrix_enrichment(self.service, jobs, self.principal, "project")
        self.assertNotIn("resume_from_job_id", jobs.submit.call_args.kwargs["payload"])

    def test_worker_passes_queued_resume_checkpoint_to_builder(self):
        jobs, builder = Mock(), Mock()
        register_planning_handlers(self.service, jobs, {"matrix.enrich": builder})
        handler = jobs.register_handler.call_args.args[1]
        checkpoint = {"papers": {"P1": {"status": "partial"}}}
        context = Mock(user_id="owner", project_id="project", retry_of_job_id=None)
        context.repository.get_job.return_value = SimpleNamespace(result={"section_checkpoint": checkpoint})
        self.service.matrix_enrichment_payload.return_value = {
            "source_matrix_artifact_id": "before", "pending_paper_count": 1,
            "fulltext_indexed_paper_count": 1,
            "papers": [{"paper_id": "P1", "evidence_candidates": [{"text": "Evidence"}]}]}
        handler(context, {"prepare_on_start": True, "source_matrix_artifact_id": "before",
            "resume_from_job_id": "previous", "selected_paper_ids": ["P1"]})
        context.repository.get_job.assert_called_once_with("owner", "previous")
        self.assertEqual(checkpoint, builder.call_args.args[1]["resume_checkpoint"])

    def test_worker_missing_sources_is_not_reported_as_success(self):
        jobs, builder = Mock(), Mock()
        register_planning_handlers(self.service, jobs, {"matrix.enrich": builder})
        handler = jobs.register_handler.call_args.args[1]
        context = Mock(user_id="owner", project_id="project", retry_of_job_id=None)
        with self.assertRaises(WorkflowConflict):
            handler(context, {"source_matrix_artifact_id": "before", "pending_paper_count": 1,
                "papers": [{"paper_id": "P1", "evidence_candidates": []}]})
        builder.assert_not_called()
