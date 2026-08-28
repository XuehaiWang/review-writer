from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase
from review_writer_api.workflow_models import WorkflowApproval


class FiguresV1Tests(NativeFigureApiTestCase):
    def test_default_review_uses_highest_scoring_anchored_image_backed_candidate(self) -> None:
        with TestClient(self.app) as client:
            response = client.get(
                f"/api/v1/projects/{self.project_id}/figures/review"
            )
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        paper = payload["papers"][0]
        self.assertEqual("P001", payload["paper_display_labels"][paper["paper_id"]])
        self.assertEqual(1, paper["selected_candidate_index"])
        selected = next(
            row for row in paper["candidates"] if row["candidate_index"] == 1
        )
        self.assertEqual("table", selected["source_type"])
        self.assertEqual("S1-p1", selected["target_paragraph_id"])
        self.assertTrue(selected["source_image_url"].startswith("/api/v1/artifacts/"))

    def test_opening_redraw_materializes_defaults_once_and_is_idempotent(self) -> None:
        with TestClient(self.app) as client:
            review = client.get(
                f"/api/v1/projects/{self.project_id}/figures/review"
            ).json()
            first = client.post(
                f"/api/v1/projects/{self.project_id}/figures/review/sync",
                json={"revision": review["revision"]},
                headers=self.headers("sync-default-selection"),
            )
            self.assertEqual(200, first.status_code, first.text)
            figures = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            )
            second = client.post(
                f"/api/v1/projects/{self.project_id}/figures/review/sync",
                json={"revision": first.json()["revision"]},
                headers=self.headers("sync-default-selection-again"),
            )
        self.assertEqual(200, figures.status_code, figures.text)
        self.assertEqual(1, len(figures.json()["figure_candidates"]))
        figure = figures.json()["figure_candidates"][0]
        self.assertEqual(
            "P001",
            figures.json()["paper_display_labels"][figure["paper_id"]],
        )
        self.assertFalse(first.json()["unchanged"])
        self.assertTrue(second.json()["unchanged"])
        self.assertEqual(
            first.json()["selected_figures_artifact_id"],
            second.json()["selected_figures_artifact_id"],
        )

    def test_selection_allows_paper_level_candidate_and_derives_current_placement(self) -> None:
        with TestClient(self.app) as client:
            response = client.put(
                f"/api/v1/projects/{self.project_id}/figures/review/P001",
                json={"revision": 0, "candidate_index": 2, "review_note": "paper-level"},
                headers=self.headers(),
            )
            figures = client.get(f"/api/v1/projects/{self.project_id}/figures")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(200, figures.status_code, figures.text)
        selected = figures.json()["figure_candidates"][0]
        self.assertEqual("P001", selected["paper_id"])
        self.assertEqual("S1-p1", selected["target_paragraph_id"])
        self.assertEqual("semantic_role_matched", selected["placement_status"])

    def test_selection_immediately_updates_redraw_source_without_confirmation(self) -> None:
        with TestClient(self.app) as client:
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/figures/review/P001",
                json={
                    "revision": 0,
                    "candidate_index": 0,
                    "review_note": "live source",
                },
                headers=self.headers("live-source-selection"),
            )
            figures = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            )
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(200, figures.status_code, figures.text)
        self.assertEqual(1, saved.json()["selected_count"])
        self.assertTrue(saved.json()["selection_complete"])
        self.assertEqual(
            ["P001-F01"],
            [row["figure_id"] for row in figures.json()["figure_candidates"]],
        )
        self.assertEqual(
            "live source",
            figures.json()["figure_candidates"][0]["source_review_note"],
        )

    def test_selection_versions_review_and_confirmation_builds_exact_inputs(self) -> None:
        with TestClient(self.app) as client:
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/figures/review/P001",
                json={"revision": 0, "candidate_index": 0, "review_note": "use scheme"},
                headers=self.headers(),
            )
            self.assertEqual(200, saved.status_code, saved.text)
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/figures/review/confirm",
                json={"revision": saved.json()["revision"]},
                headers=self.headers(),
            )
            figures = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual("approved", confirmed.json()["status"])
        self.assertEqual(["P001-F01"], [row["figure_id"] for row in figures["figure_candidates"]])

    def test_confirmation_does_not_block_on_paper_without_usable_figure(self) -> None:
        with TestClient(self.app) as client:
            review = client.get(
                f"/api/v1/projects/{self.project_id}/figures/review"
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/figures/review/confirm",
                json={"revision": review["revision"]},
                headers=self.headers("skip-no-figure"),
            )
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual(1, confirmed.json()["selected_count"])
        self.assertFalse(
            next(row for row in review["papers"] if row["paper_id"] == "P002")[
                "review_required"
            ]
        )

    def test_individual_redraw_job_publishes_immutable_output(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            first = self.start_redraw(client, "redraw-1")
            first_payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            first_id = first_payload["redrawn_manifest"]["figures"][0][
                "output_artifact_id"
            ]
            second = self.start_redraw(client, "redraw-2")
            second_payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            second_id = second_payload["redrawn_manifest"]["figures"][0][
                "output_artifact_id"
            ]
        self.assertEqual("succeeded", first["status"])
        self.assertEqual("succeeded", second["status"])
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(
            second_payload["redrawn_manifest"]["figures"][0][
                "redrawn_image_url"
            ].startswith("/api/v1/artifacts/")
        )

    def test_two_different_individual_figures_can_redraw_concurrently(self) -> None:
        self.block_redraw = True
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.add_second_confirmed_figure()
            first = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F01/jobs",
                json={},
                headers=self.headers("parallel-redraw-one"),
            )
            second = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/jobs",
                json={},
                headers=self.headers("parallel-redraw-two"),
            )
            self.assertEqual(202, first.status_code, first.text)
            self.assertEqual(202, second.status_code, second.text)
            self.assertTrue(self.redraw_both_started.wait(2))

            duplicate = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F01/jobs",
                json={},
                headers=self.headers("parallel-redraw-one-again"),
            )
            self.assertEqual(409, duplicate.status_code, duplicate.text)
            self.redraw_release.set()
            first_done = self.wait_job(client, first.json()["id"])
            second_done = self.wait_job(client, second.json()["id"])

        self.assertEqual("succeeded", first_done["status"])
        self.assertEqual("succeeded", second_done["status"])

    def test_finished_redraw_remains_visible_in_batch_and_per_figure_status(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "visible-status")
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual("succeeded", payload["batch_redraw"]["status"])
        self.assertEqual(1, payload["batch_redraw"]["succeeded"])
        self.assertEqual(0, payload["batch_redraw"]["failed"])
        self.assertEqual(
            "completed", payload["figure_redraw_states"]["P001-F02"]["status"]
        )

    def test_chemical_warning_requires_append_only_human_approval(self) -> None:
        self.integrity_status = "failed"
        with TestClient(self.app) as client:
            self.confirm_review(client)
            job = self.start_redraw(client, "warning")
            before = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            figure_id = before["figure_candidates"][0]["figure_id"]
            approved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/{figure_id}/approve",
                headers=self.headers(),
            )
            after = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(0, before["freshness"]["usable_count"])
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertEqual(1, after["freshness"]["usable_count"])
        with self.sessions() as session:
            approvals = session.query(WorkflowApproval).filter_by(
                project_id=uuid.UUID(self.project_id), subject_id=figure_id
            ).all()
        self.assertEqual(1, len(approvals))

    def test_missing_structured_chemistry_result_fails_closed(self) -> None:
        self.include_chemistry_integrity = False
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "missing-chemistry")
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        row = payload["redrawn_manifest"]["figures"][0]
        self.assertEqual("failed", row["chemistry_integrity"]["status"])
        self.assertTrue(row["requires_human_approval"])
        self.assertFalse(row["usable"])
        self.assertEqual(0, payload["freshness"]["usable_count"])

    def test_ai_canvas_mismatch_can_be_manually_approved(self) -> None:
        self.output_size = (10, 10)
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "mismatch")
            figures = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            figure_id = figures["figure_candidates"][0]["figure_id"]
            approved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/{figure_id}/approve",
                headers=self.headers(),
            )
            current = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual(200, approved.status_code, approved.text)
        row = current["redrawn_manifest"]["figures"][0]
        self.assertTrue(row["usable"])
        self.assertTrue(row["human_approval"]["manual_canvas_override"])

    def test_provider_adult_content_block_has_stable_job_error(self) -> None:
        self.redraw_error = "Image request rejected: adult content safety policy"
        with TestClient(self.app) as client:
            self.confirm_review(client)
            job = self.start_redraw(client, "safety")
        self.assertEqual("failed", job["status"])
        self.assertEqual("FIGURE_SAFETY_BLOCKED", job["error_code"])

    def test_failed_redraw_can_be_retried_after_provider_recovers(self) -> None:
        self.redraw_error = "Image request rejected: adult content safety policy"
        with TestClient(self.app) as client:
            self.confirm_review(client)
            failed = self.start_redraw(client, "retry-safety")
            self.redraw_error = ""
            retried = client.post(
                f"/api/v1/jobs/{failed['id']}/retry",
                headers=self.headers("retry-safety-job"),
            )
            self.assertEqual(202, retried.status_code, retried.text)
            finished = self.wait_job(client, retried.json()["id"])
        self.assertEqual("succeeded", finished["status"])

    def test_running_batch_redraw_can_be_cancelled_cooperatively(self) -> None:
        self.block_redraw = True
        with TestClient(self.app) as client:
            self.confirm_review(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/figures/jobs",
                json={},
                headers=self.headers("cancel-batch"),
            )
            self.assertEqual(202, started.status_code, started.text)
            self.assertTrue(self.redraw_started.wait(2), "redraw handler did not start")
            cancelled = client.post(
                f"/api/v1/jobs/{started.json()['id']}/cancel",
                headers=self.headers("cancel-batch-job"),
            )
            self.assertEqual(200, cancelled.status_code, cancelled.text)
            finished = self.wait_job(client, started.json()["id"])
        self.assertEqual("cancelled", finished["status"])

    def test_cancelled_batch_reports_outputs_completed_before_cancellation(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.add_second_confirmed_figure()
            self.block_figure_id = "P001-F01"
            started = client.post(
                f"/api/v1/projects/{self.project_id}/figures/jobs",
                json={},
                headers=self.headers("cancel-partial-batch"),
            )
            self.assertEqual(202, started.status_code, started.text)
            self.assertTrue(self.redraw_started.wait(2), "second redraw did not start")
            cancelled = client.post(
                f"/api/v1/jobs/{started.json()['id']}/cancel",
                headers=self.headers("cancel-partial-batch-job"),
            )
            self.assertEqual(200, cancelled.status_code, cancelled.text)
            finished = self.wait_job(client, started.json()["id"])
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual("cancelled", finished["status"])
        self.assertEqual(1, len(finished["result"]["outputs"]))
        self.assertEqual(1, payload["batch_redraw"]["succeeded"])
        self.assertEqual(
            "completed", payload["figure_redraw_states"]["P001-F02"]["status"]
        )
        self.assertEqual(
            "cancelled", payload["figure_redraw_states"]["P001-F01"]["status"]
        )

    def test_cancelled_batch_does_not_claim_a_later_single_retry_output(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.add_second_confirmed_figure()
            self.block_figure_id = "P001-F01"
            started = client.post(
                f"/api/v1/projects/{self.project_id}/figures/jobs",
                json={},
                headers=self.headers("cancel-at-second-item"),
            )
            self.assertTrue(self.redraw_started.wait(2))
            client.post(
                f"/api/v1/jobs/{started.json()['id']}/cancel",
                headers=self.headers("cancel-at-second-item-request"),
            )
            cancelled = self.wait_job(client, started.json()["id"])
            self.block_figure_id = ""
            retried = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F01/jobs",
                json={"retry_of_job_id": cancelled["id"]},
                headers=self.headers("retry-cancelled-item"),
            )
            self.assertEqual(202, retried.status_code, retried.text)
            self.assertEqual("succeeded", self.wait_job(client, retried.json()["id"])["status"])
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual(1, payload["batch_redraw"]["succeeded"])
        self.assertEqual(1, len(cancelled["result"]["outputs"]))

    def test_batch_redraw_continues_after_one_figure_fails(self) -> None:
        self.redraw_errors_by_figure["P001-F02"] = "provider unavailable"
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.add_second_confirmed_figure()
            job = self.start_redraw(client, "partial-batch")
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            self.redraw_errors_by_figure.clear()
            retry = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/jobs",
                json={"retry_of_job_id": job["id"]},
                headers=self.headers("partial-item-retry"),
            )
            self.assertEqual(202, retry.status_code, retry.text)
            retried = self.wait_job(client, retry.json()["id"])
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(1, job["result"]["figure_count"])
        self.assertEqual(1, len(job["result"]["errors"]))
        self.assertEqual(1, payload["batch_redraw"]["succeeded"])
        self.assertEqual(1, payload["batch_redraw"]["failed"])
        self.assertEqual(
            "failed", payload["figure_redraw_states"]["P001-F02"]["status"]
        )
        self.assertEqual(
            "completed", payload["figure_redraw_states"]["P001-F01"]["status"]
        )
        self.assertEqual("succeeded", retried["status"])
        self.assertEqual(job["id"], retried["retry_of_job_id"])

    def test_approve_all_human_approves_generated_canvas_warnings(self) -> None:
        self.output_size = (10, 10)
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "bulk-mismatch")
            approved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/approve-successful",
                headers=self.headers("bulk-mismatch-approve"),
            )
            current = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertEqual(1, approved.json()["approved_count"])
        self.assertEqual(0, approved.json()["skipped_count"])
        row = current["redrawn_manifest"]["figures"][0]
        self.assertTrue(row["usable"])
        self.assertTrue(row["human_approval"]["manual_canvas_override"])

    def test_approve_all_does_not_duplicate_current_human_approval(self) -> None:
        self.integrity_status = "failed"
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "bulk-approve")
            first = client.post(
                f"/api/v1/projects/{self.project_id}/figures/approve-successful",
                headers=self.headers("bulk-approve-first"),
            )
            second = client.post(
                f"/api/v1/projects/{self.project_id}/figures/approve-successful",
                headers=self.headers("bulk-approve-second"),
            )
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(1, first.json()["approved_count"])
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(0, second.json()["approved_count"])
        self.assertEqual(1, second.json()["already_approved_count"])
        with self.sessions() as session:
            approvals = session.query(WorkflowApproval).filter_by(
                project_id=uuid.UUID(self.project_id), subject_id="P001-F02"
            ).all()
        self.assertEqual(1, len(approvals))

    def test_preserve_all_sources_uses_originals_without_ai_redraw(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            preserved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/preserve-sources",
                headers=self.headers("preserve-all-sources"),
            )
            current = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/figures/confirm",
                json={"revision": current["revision"]},
                headers=self.headers("confirm-preserved-sources"),
            )

        self.assertEqual(200, preserved.status_code, preserved.text)
        self.assertEqual(1, preserved.json()["preserved_count"])
        self.assertEqual(0, self.redraw_calls)
        self.assertTrue(current["source_preservation"]["all_selected"])
        self.assertEqual(1, current["source_preservation"]["preserved_count"])
        row = current["redrawn_manifest"]["figures"][0]
        self.assertTrue(row["source_preserved"])
        self.assertFalse(row["ai_redraw_performed"])
        self.assertEqual("source-original", row["render_mode"])
        self.assertEqual(row["source_artifact_id"], row["output_artifact_id"])
        self.assertEqual(
            current["figure_candidates"][0]["source_image_url"],
            row["redrawn_image_url"],
        )
        self.assertTrue(row["usable"])
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual("draft", confirmed.json()["next_stage"])

    def test_preserve_all_sources_is_idempotent(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            first = client.post(
                f"/api/v1/projects/{self.project_id}/figures/preserve-sources",
                headers=self.headers("preserve-all-sources-first"),
            )
            second = client.post(
                f"/api/v1/projects/{self.project_id}/figures/preserve-sources",
                headers=self.headers("preserve-all-sources-second"),
            )

        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(0, second.json()["preserved_count"])
        self.assertEqual(1, second.json()["already_preserved_count"])
        self.assertEqual(
            first.json()["manifest_artifact_id"],
            second.json()["manifest_artifact_id"],
        )

    def test_preserve_sources_keeps_existing_ai_redraw_unchanged(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.add_second_confirmed_figure()
            redraw = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/jobs",
                json={},
                headers=self.headers("redraw-before-source-fill"),
            )
            self.assertEqual(202, redraw.status_code, redraw.text)
            self.assertEqual(
                "succeeded",
                self.wait_job(client, redraw.json()["id"])["status"],
            )
            before = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            before_rows = {
                row["figure_id"]: row
                for row in before["redrawn_manifest"]["figures"]
            }
            ai_output_id = before_rows["P001-F02"]["output_artifact_id"]

            preserved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/preserve-sources",
                headers=self.headers("fill-only-unprocessed-sources"),
            )
            after = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()

        self.assertEqual(200, preserved.status_code, preserved.text)
        self.assertEqual(1, preserved.json()["preserved_count"])
        self.assertEqual(1, preserved.json()["retained_generated_count"])
        after_rows = {
            row["figure_id"]: row
            for row in after["redrawn_manifest"]["figures"]
        }
        self.assertEqual(ai_output_id, after_rows["P001-F02"]["output_artifact_id"])
        self.assertFalse(after_rows["P001-F02"].get("source_preserved", False))
        self.assertTrue(after_rows["P001-F01"]["source_preserved"])
        self.assertEqual(
            after_rows["P001-F01"]["source_artifact_id"],
            after_rows["P001-F01"]["output_artifact_id"],
        )
        self.assertEqual(1, after["source_preservation"]["generated_count"])
        self.assertEqual(1, after["source_preservation"]["preserved_count"])
        self.assertEqual(0, after["source_preservation"]["unprocessed_count"])

    def test_approval_audit_rolls_back_when_manifest_promotion_conflicts(self) -> None:
        self.integrity_status = "failed"
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "approval-conflict")
            repository = self.app.state.workflow_repository
            original = repository.promote_stage_artifacts_atomically

            def conflict(*args, **kwargs):
                if kwargs.get("approval_events"):
                    repository.set_current_artifact(
                        self.first.user_id,
                        self.project_id,
                        "figures/manifest.json",
                        kwargs["artifact_ids"]["figures/manifest.json"],
                    )
                return original(*args, **kwargs)

            repository.promote_stage_artifacts_atomically = conflict
            try:
                response = client.post(
                    f"/api/v1/projects/{self.project_id}/figures/P001-F02/approve",
                    headers=self.headers("approval-conflict-save"),
                )
            finally:
                repository.promote_stage_artifacts_atomically = original
        self.assertEqual(409, response.status_code, response.text)
        with self.sessions() as session:
            approvals = session.query(WorkflowApproval).filter_by(
                project_id=uuid.UUID(self.project_id), subject_id="P001-F02"
            ).all()
        self.assertEqual([], approvals)

    def test_confirm_blocks_until_every_selected_output_is_usable(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            before = client.post(
                f"/api/v1/projects/{self.project_id}/figures/confirm",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.start_redraw(client, "gate")
            current = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            after = client.post(
                f"/api/v1/projects/{self.project_id}/figures/confirm",
                json={"revision": current["revision"]},
                headers=self.headers(),
            )
        self.assertEqual(409, before.status_code, before.text)
        self.assertEqual("FIGURE_OUTPUTS_INCOMPLETE", before.json()["error"]["code"])
        self.assertEqual(200, after.status_code, after.text)
        self.assertEqual("draft", after.json()["next_stage"])

    def test_figure_api_and_container_are_user_isolated(self) -> None:
        self.current = self.second
        with TestClient(self.app) as client:
            response = client.get(
                f"/api/v1/projects/{self.project_id}/figures/review"
            )
        self.assertEqual(404, response.status_code, response.text)


if __name__ == "__main__":
    import unittest

    unittest.main()
