from __future__ import annotations

import json
import re
import time
import uuid

from fastapi.testclient import TestClient

from review_writer_api.domain_services.drafts import (
    DRAFT_DOCUMENT,
    DRAFT_QUALITY,
    DraftsService,
)
from review_writer_api.errors import WorkflowConflict
from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase
from review_writer_api.workflow_models import WorkflowApproval


class DraftsV1Tests(NativeFigureApiTestCase):
    def setUp(self) -> None:
        self.noop_rewrite = False
        self.hard_gate_failures: list[str] = []
        self.accept_rewrite_model_calls = 0
        super().setUp()

    def extra_native_workflow_overrides(self) -> dict:
        def evaluate(_context, payload):
            paragraph_id = payload["paragraphs"][0]["paragraph_id"]
            return {
                "score": 72.5,
                "goal": float(payload.get("goal") or 90),
                "decision": "REVISE",
                "dimension_scores": [{"id": "evidence", "score": 72.5}],
                "paragraph_scores": [
                    {
                        "paragraph_id": paragraph_id,
                        "score": 60,
                        "severity": "major",
                        "route": "section_rewrite",
                    }
                ],
                "issues": [
                    {
                        "issue_id": "issue-1",
                        "paragraph_id": paragraph_id,
                        "severity": "major",
                        "message": "Strengthen the evidence comparison.",
                    }
                ],
                "hard_gate_failures": list(self.hard_gate_failures),
            }

        def rewrite(_context, payload):
            if not payload.get("quality") or payload["quality"].get("score") != 72.5:
                raise RuntimeError("Rewrite payload did not include the evaluated quality snapshot.")
            original = payload["paragraph_text"]
            if original not in str(payload.get("draft_text") or ""):
                raise RuntimeError(
                    "Rewrite payload did not include the complete current Draft."
                )
            return {
                "candidate_text": (
                    original
                    if self.noop_rewrite
                    else original.rstrip() + " The comparison is now explicit [1]."
                ),
                "resolved_issue_ids": ["issue-1"],
                "source_paragraph_evaluation": {
                    "evaluation_scope": "single_paragraph",
                    "evaluation_mode": "stored_source_score",
                    "paragraph_id": payload["paragraph_id"],
                    "paragraph_score": {
                        "paragraph_id": payload["paragraph_id"],
                        "score": 60.0,
                        "severity": "major",
                        "route": "section_rewrite",
                    },
                },
                "candidate_evaluation": {
                    "evaluation_scope": "single_paragraph",
                    "evaluation_mode": "accepted_candidate",
                    "paragraph_id": payload["paragraph_id"],
                    "paragraph_score": {
                        "paragraph_id": payload["paragraph_id"],
                        "score": 92.0,
                        "severity": "none",
                        "route": "pass",
                        "failed_dimensions": [],
                        "diagnosis": "",
                        "source_check_status": "verified",
                        "source_evidence_refs": [],
                        "unsupported_claims": [],
                    },
                    "local_dimension_scores": [],
                    "local_hard_gate_failures": [],
                    "local_preflight": {
                        "paragraph_checks": [],
                        "paragraph_findings": [],
                    },
                    "source_check_entry": {
                        "paragraph_id": payload["paragraph_id"],
                        "source_check_status": "verified",
                    },
                    "evaluated_at": "2026-08-16T00:00:00+00:00",
                },
            }

        def accept_rewrite(_context, payload):
            self.accept_rewrite_model_calls += 1
            return {
                "evaluation_scope": "single_paragraph",
                "paragraph_id": payload["paragraph_id"],
                "paragraph_score": {
                    "paragraph_id": payload["paragraph_id"],
                    "score": 92.0,
                    "severity": "none",
                    "route": "pass",
                    "failed_dimensions": [],
                    "diagnosis": "",
                    "source_check_status": "verified",
                    "source_evidence_refs": [],
                    "unsupported_claims": [],
                },
                "local_dimension_scores": [],
                "local_hard_gate_failures": [],
                "local_preflight": {
                    "paragraph_checks": [],
                    "paragraph_findings": [],
                },
                "source_check_entry": {
                    "paragraph_id": payload["paragraph_id"],
                    "source_check_status": "verified",
                },
                "evaluated_at": "2026-08-16T00:00:00+00:00",
            }

        def optimize(_context, payload):
            paragraph = payload["paragraphs"][0]
            original = paragraph["text"]
            candidate = original.rstrip() + " Batch-safe comparison [1]."
            draft_text = payload["draft_text"].replace(original, candidate, 1)
            return {
                "draft_text": draft_text,
                "score": 91.0,
                "goal": float(payload.get("goal") or 90),
                "decision": "PASS",
                "dimension_scores": [{"id": "evidence", "score": 91.0}],
                "paragraph_scores": [
                    {
                        "paragraph_id": paragraph["paragraph_id"],
                        "score": 91.0,
                        "severity": "none",
                        "route": "pass",
                    }
                ],
                "issues": [],
                "hard_gate_failures": [],
                "feedback_status": {
                    "status": "completed",
                    "phase": "released",
                    "iteration": 2,
                    "max_iterations": int(payload.get("max_iterations") or 3),
                    "rewrite_accepted": 1,
                    "rewrite_rejected": 0,
                },
            }

        return {
            "draft.evaluate": evaluate,
            "draft.optimize": optimize,
            "draft.rewrite": rewrite,
            "draft.accept-rewrite": accept_rewrite,
        }

    def prepare_draft(self, client: TestClient) -> dict:
        self.confirm_review(client)
        redraw = self.start_redraw(client, "draft-redraw")
        self.assertEqual("succeeded", redraw["status"])
        figures = client.get(f"/api/v1/projects/{self.project_id}/figures").json()
        confirmed = client.post(
            f"/api/v1/projects/{self.project_id}/figures/confirm",
            json={"revision": figures["revision"]},
            headers=self.headers("draft-figure-confirm"),
        )
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        assembled = client.post(
            f"/api/v1/projects/{self.project_id}/draft/assemble",
            headers=self.headers("draft-assemble"),
        )
        self.assertEqual(200, assembled.status_code, assembled.text)
        return assembled.json()

    def test_claim_centered_paragraph_keeps_all_source_callouts(self) -> None:
        markdown = self.app.state.drafts_service._assemble_markdown(
            "Review",
            {
                "sections": [
                    {
                        "section_id": "S01",
                        "heading": "Evidence comparison",
                        "paragraphs": [
                            {
                                "paragraph_id": "S01-p1",
                                "paper_id": "P001",
                                "cited_paper_ids": ["P001", "P002"],
                                "text": "The two studies support different boundaries.",
                            }
                        ],
                    }
                ]
            },
            {"figures": []},
            {
                "rows": [
                    {"paper_id": "P001", "title": "Study one"},
                    {"paper_id": "P002", "title": "Study two"},
                ]
            },
        )
        self.assertIn("The two studies support different boundaries. [1, 2]", markdown)
        self.assertIn("[1] Study one", markdown)
        self.assertIn("[2] Study two", markdown)

    def test_assembly_rebuilds_claim_citations_in_one_first_appearance_ledger(self) -> None:
        markdown = self.app.state.drafts_service._assemble_markdown(
            "Review",
            {
                "sections": [
                    {
                        "section_id": "S01",
                        "heading": "Evidence comparison",
                        "paragraphs": [
                            {
                                "paragraph_id": "S01-p1",
                                "paper_id": "P002",
                                "cited_paper_ids": ["P002", "P001"],
                                "text": (
                                    "Legacy first claim [16]. Legacy comparison [7, 11]."
                                ),
                                "claim_realizations": [
                                    {
                                        "claim_id": "S01-p1-C01",
                                        "text": "The first study reports the transformation.",
                                        "citation_group": ["P002"],
                                    },
                                    {
                                        "claim_id": "S01-p1-C02",
                                        "text": "The studies support a bounded comparison.",
                                        "citation_group": ["P002", "P001"],
                                    },
                                ],
                            }
                        ],
                    }
                ]
            },
            {"figures": []},
            {
                "rows": [
                    {"paper_id": "P001", "title": "Study one"},
                    {"paper_id": "P002", "title": "Study two"},
                ]
            },
        )

        self.assertIn(
            "The first study reports the transformation. [1] "
            "The studies support a bounded comparison. [1, 2]",
            markdown,
        )
        self.assertNotIn("[16]", markdown)
        self.assertNotIn("[7, 11]", markdown)
        self.assertIn("[1] Study two", markdown)
        self.assertIn("[2] Study one", markdown)

    def test_figure_caption_is_normalized_without_leaking_conditions_into_prose(self) -> None:
        source_caption = (
            r"Scheme 1. $Pd_{2}(dba)_{3}\cdot CHCl_{3}$ , "
            r"$(S)-(-)$ -MeO-MOP, $CHCl_{3}$ ; $-78^{\circ}C$"
        )
        markdown = self.app.state.drafts_service._assemble_markdown(
            "Review",
            {
                "sections": [
                    {
                        "section_id": "S01",
                        "heading": "Methods",
                        "paragraphs": [
                            {
                                "paragraph_id": "S01-p1",
                                "paper_id": "P001",
                                "cited_paper_ids": ["P001"],
                                "text": "The study reports an asymmetric transformation.",
                            }
                        ],
                    }
                ]
            },
            {
                "figures": [
                    {
                        "figure_id": "P001-F01",
                        "paper_id": "P001",
                        "target_paragraph_id": "S01-p1",
                        "output_artifact_id": "artifact-1",
                        "status": "redrawn",
                        "source_caption_text": source_caption,
                    }
                ]
            },
            {"rows": [{"paper_id": "P001", "title": "Study one"}]},
        )

        prose, figure_block = markdown.split("<!-- paragraph_id: S01-p1 -->", 1)
        self.assertNotIn("Pd_{2}", prose)
        self.assertIn(
            "Figure 1 provides source-linked visual context for this discussion", prose
        )
        self.assertIn(
            "Figure 1. Pd₂(dba)₃·CHCl₃, (S)-(−)-MeO-MOP, CHCl₃; −78 °C",
            figure_block,
        )
        self.assertNotIn("$", figure_block)

    def test_rewrite_payload_carries_the_complete_evaluated_draft(self) -> None:
        service = object.__new__(DraftsService)
        complete_draft = (
            "# Review\n\nEvidence paragraph.\n\n"
            "<!-- paragraph_id: S01-p1 -->\n"
        )
        service.get = lambda _principal, _project_id: {  # type: ignore[method-assign]
            "first_draft_md": complete_draft,
            "draft_artifact_id": "draft-artifact",
            "quality_artifact_id": "quality-artifact",
            "revision": 4,
            "paragraphs": [
                {"paragraph_id": "S01-p1", "text": "Evidence paragraph."}
            ],
            "quality": {
                "current": True,
                "issues": [
                    {
                        "issue_id": "issue-1",
                        "paragraph_id": "S01-p1",
                        "message": "Strengthen the evidence comparison.",
                    }
                ],
            },
        }
        service.compatibility_payload = (  # type: ignore[method-assign]
            lambda _principal, _project_id: {"matrix": {"rows": []}}
        )

        payload = service.rewrite_payload(None, "project-1", "S01-p1")

        self.assertEqual(complete_draft, payload["draft_text"])
        self.assertIn(payload["paragraph_text"], payload["draft_text"])

    def publish_changed_sections(self, *, change_rendered_text: bool = True) -> str:
        repository = self.app.state.workflow_repository
        artifacts = self.app.state.artifact_service
        current = repository.get_current_artifact(
            self.first.user_id, self.project_id, "sections/section_drafts.json"
        )
        self.assertIsNotNone(current)
        resolved = artifacts.resolve_owned_artifact(self.first.user_id, current.id)
        payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        if change_rendered_text:
            payload["sections"][0]["paragraphs"][0]["text"] += " Updated upstream."
        else:
            payload["source_revision"] = str(uuid.uuid4())
        state = repository.get_stage_state(
            self.first.user_id, self.project_id, "sections"
        )
        run = repository.create_stage_run(
            self.first.user_id, self.project_id, "sections", status="succeeded"
        )
        changed = self._publish(
            run.id,
            "sections/section_drafts.json",
            "changed-sections.json",
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
            "json",
        )
        repository.promote_stage_artifacts_atomically(
            self.first.user_id,
            self.project_id,
            "sections",
            artifact_ids={changed.logical_name: changed.id},
            run_id=run.id,
            expected_revision=state.revision,
            status="approved",
            invalidate_stages=("figure-review", "figures", "draft", "final"),
            expected_current_artifacts={current.logical_name: current.id},
        )
        return changed.id

    def test_assembly_publishes_marker_stable_draft_with_current_figure(self) -> None:
        with TestClient(self.app) as client:
            assembled = self.prepare_draft(client)
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertTrue(assembled["draft_artifact_id"])
        self.assertTrue(payload["first_draft_md"].startswith("# Copper chemistry\n"))
        self.assertIn("<!-- paragraph_id:", payload["first_draft_md"])
        self.assertIn("/api/v1/artifacts/", payload["first_draft_md"])
        self.assertIn("## References", payload["first_draft_md"])
        self.assertNotIn("[1] P001", payload["first_draft_md"])
        self.assertTrue(payload["paragraphs"])
        self.assertFalse(payload["freshness"]["upstream_stale"])

    def test_assembly_rejects_upstream_change_between_gate_and_promotion(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            self.start_redraw(client, "race-redraw")
            figures = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/figures/confirm",
                json={"revision": figures["revision"]},
                headers=self.headers("race-figure-confirm"),
            )
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            repository = self.app.state.workflow_repository
            original = repository.promote_stage_artifacts_atomically
            raced = False

            def promote_with_race(user_id, project_id, stage_id, **kwargs):
                nonlocal raced
                if stage_id == "draft" and not raced:
                    raced = True
                    self.publish_changed_sections()
                return original(user_id, project_id, stage_id, **kwargs)

            repository.promote_stage_artifacts_atomically = promote_with_race
            try:
                assembled = client.post(
                    f"/api/v1/projects/{self.project_id}/draft/assemble",
                    headers=self.headers("racing-draft-assemble"),
                )
            finally:
                repository.promote_stage_artifacts_atomically = original
        self.assertEqual(409, assembled.status_code, assembled.text)

    def test_assembly_does_not_reuse_old_provenance_when_rendered_text_is_equal(self) -> None:
        with TestClient(self.app) as client:
            first = self.prepare_draft(client)
            repository = self.app.state.workflow_repository
            old_manifest = repository.get_current_artifact(
                self.first.user_id, self.project_id, "figures/manifest.json"
            )
            self.assertIsNotNone(old_manifest)
            self.publish_changed_sections(change_rendered_text=False)
            figures_state = repository.get_stage_state(
                self.first.user_id, self.project_id, "figures"
            )
            figures_run = repository.create_stage_run(
                self.first.user_id, self.project_id, "figures", status="succeeded"
            )
            repository.promote_stage_artifacts_atomically(
                self.first.user_id,
                self.project_id,
                "figures",
                artifact_ids={old_manifest.logical_name: old_manifest.id},
                run_id=figures_run.id,
                expected_revision=figures_state.revision,
                status="approved",
                invalidate_stages=("draft", "final"),
            )
            second = client.post(
                f"/api/v1/projects/{self.project_id}/draft/assemble",
                headers=self.headers("equal-render-new-provenance"),
            )
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
        self.assertEqual(200, second.status_code, second.text)
        self.assertNotEqual(first["draft_artifact_id"], second.json()["draft_artifact_id"])
        self.assertFalse(payload["freshness"]["upstream_stale"])

    def test_draft_and_final_routes_are_user_project_isolated(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            self.current = self.second
            draft = client.get(f"/api/v1/projects/{self.project_id}/draft")
            final = client.get(f"/api/v1/projects/{self.project_id}/final")
            restore = client.post(
                f"/api/v1/projects/{self.project_id}/draft/restore",
                json={"artifact_id": "00000000-0000-0000-0000-000000000000", "revision": 1},
                headers=self.headers("cross-user-restore"),
            )
        self.assertEqual(404, draft.status_code, draft.text)
        self.assertEqual(404, final.status_code, final.text)
        self.assertEqual(404, restore.status_code, restore.text)

    def test_full_and_paragraph_edits_are_immutable_and_revision_checked(self) -> None:
        with TestClient(self.app) as client:
            assembled = self.prepare_draft(client)
            before_id = assembled["draft_artifact_id"]
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            paragraph = current["paragraphs"][0]
            edited = client.put(
                f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph['paragraph_id']}",
                json={"text": paragraph["text"] + " Manual edit.", "revision": current["revision"]},
                headers=self.headers("paragraph-edit"),
            )
            stale = client.put(
                f"/api/v1/projects/{self.project_id}/draft",
                json={"text": current["first_draft_md"], "revision": current["revision"]},
                headers=self.headers("stale-draft-edit"),
            )
            after = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            old = client.get(f"/api/v1/artifacts/{before_id}/content")
        self.assertEqual(200, edited.status_code, edited.text)
        self.assertNotEqual(before_id, edited.json()["draft_artifact_id"])
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertIn(paragraph["paragraph_id"], after["draft_manual_paragraph_ids"])
        self.assertEqual(200, old.status_code)
        self.assertNotIn("Manual edit.", old.text)

    def test_manual_markdown_formatting_change_is_preserved(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            formatted = current["first_draft_md"].replace("\n\n", "\n\n\n", 1)
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/draft",
                json={"text": formatted, "revision": current["revision"]},
                headers=self.headers("draft-formatting-edit"),
            )
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertIn("\n\n\n", payload["first_draft_md"])

    def test_restore_repoints_to_an_immutable_draft_version_and_audits_it(self) -> None:
        with TestClient(self.app) as client:
            assembled = self.prepare_draft(client)
            original_id = assembled["draft_artifact_id"]
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            edited = client.put(
                f"/api/v1/projects/{self.project_id}/draft",
                json={
                    "text": current["first_draft_md"] + "\nTemporary manual text.\n",
                    "revision": current["revision"],
                },
                headers=self.headers("draft-edit-before-restore"),
            )
            restored = client.post(
                f"/api/v1/projects/{self.project_id}/draft/restore",
                json={"artifact_id": original_id, "revision": edited.json()["revision"]},
                headers=self.headers("draft-restore"),
            )
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual(200, restored.status_code, restored.text)
        self.assertEqual(original_id, restored.json()["draft_artifact_id"])
        self.assertEqual(original_id, payload["draft_artifact_id"])
        self.assertNotIn("Temporary manual text.", payload["first_draft_md"])
        self.assertGreaterEqual(len(payload["versions"]), 2)
        self.assertTrue(any(version["current"] for version in payload["versions"]))
        with self.sessions() as session:
            events = session.query(WorkflowApproval).filter_by(
                project_id=uuid.UUID(self.project_id),
                stage_id="draft",
                subject_type="draft-version",
                decision="undo",
            ).all()
        self.assertEqual(1, len(events))

    def test_restore_keeps_restored_version_provenance_after_upstream_change(self) -> None:
        with TestClient(self.app) as client:
            first = self.prepare_draft(client)
            first_id = first["draft_artifact_id"]
            self.publish_changed_sections()
            review = client.get(
                f"/api/v1/projects/{self.project_id}/figures/review"
            ).json()
            reconfirmed = client.post(
                f"/api/v1/projects/{self.project_id}/figures/review/confirm",
                json={"revision": review["revision"]},
                headers=self.headers("restore-v2-review"),
            )
            self.assertEqual(200, reconfirmed.status_code, reconfirmed.text)
            redraw = self.start_redraw(client, "restore-v2-redraw")
            self.assertEqual("succeeded", redraw["status"])
            figures = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/figures/confirm",
                json={"revision": figures["revision"]},
                headers=self.headers("restore-v2-figure-confirm"),
            )
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            second = client.post(
                f"/api/v1/projects/{self.project_id}/draft/assemble",
                headers=self.headers("restore-v2-assemble"),
            )
            self.assertEqual(200, second.status_code, second.text)
            self.assertNotEqual(first_id, second.json()["draft_artifact_id"])
            restored = client.post(
                f"/api/v1/projects/{self.project_id}/draft/restore",
                json={
                    "artifact_id": first_id,
                    "revision": second.json()["revision"],
                },
                headers=self.headers("restore-old-provenance"),
            )
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
        self.assertEqual(200, restored.status_code, restored.text)
        self.assertEqual(first_id, payload["draft_artifact_id"])
        self.assertTrue(payload["freshness"]["upstream_stale"])

    def test_live_quality_issues_expand_with_paragraph_and_matching_images(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={"goal": 90},
                headers=self.headers("draft-evaluate"),
            )
            self.assertEqual(202, started.status_code, started.text)
            job = self.wait_job(client, started.json()["id"])
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(3, job["progress_current"])
        self.assertEqual(3, job["progress_total"])
        self.assertEqual("", payload["active_feedback_job_id"])
        self.assertEqual(job["id"], payload["latest_feedback_job_id"])
        self.assertEqual("draft.evaluate", payload["latest_feedback_job_type"])
        self.assertEqual("succeeded", payload["latest_feedback_job_status"])
        self.assertEqual(72.5, payload["quality"]["score"])
        issue = payload["quality"]["issues"][0]
        self.assertTrue(issue["paragraph"]["text"])
        self.assertTrue(issue["paragraph"]["images"])
        self.assertEqual("completed", payload["quality"]["status"])

    def test_manual_save_persists_missing_paragraph_markers_before_scoring(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            without_markers = re.sub(
                r"\n*<!--\s*paragraph_id:[^>]+-->\s*", "\n\n", current["first_draft_md"]
            )
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/draft",
                json={"text": without_markers, "revision": current["revision"]},
                headers=self.headers("remove-markers"),
            )
            self.assertEqual(200, saved.status_code, saved.text)
            saved_payload = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
            self.assertIn("<!-- paragraph_id:", saved_payload["first_draft_md"])
            self.assertEqual("full-edit", saved_payload["versions"][0]["operation"])
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={},
                headers=self.headers("evaluate-normalized-markers"),
            )
            job = self.wait_job(client, started.json()["id"])
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual("succeeded", job["status"])
        self.assertIn("<!-- paragraph_id:", payload["first_draft_md"])
        self.assertTrue(payload["quality"]["issues"][0]["paragraph"]["text"])
        self.assertEqual("full-edit", payload["versions"][0]["operation"])

    def test_batch_safe_optimization_requires_review_then_publishes_atomically(self) -> None:
        with TestClient(self.app) as client:
            assembled = self.prepare_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/optimization-jobs",
                json={
                    "goal": 90,
                    "paragraph_goal": 85,
                    "max_iterations": 3,
                    "min_case_words": 140,
                    "max_case_words": 280,
                },
                headers=self.headers("batch-safe-optimize"),
            )
            self.assertEqual(202, started.status_code, started.text)
            job = self.wait_job(client, started.json()["id"])
            pending = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            proposal = next(
                item
                for item in pending["optimization_proposals"]
                if item["status"] == "pending"
            )
            accepted = client.post(
                f"/api/v1/projects/{self.project_id}/draft/optimization-proposals/{proposal['proposal_id']}/accept",
                json={"revision": pending["revision"]},
                headers=self.headers("accept-batch-safe-optimize"),
            )
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(5, job["progress_current"])
        self.assertEqual(5, job["progress_total"])
        self.assertTrue(job["result"]["proposal_created"])
        self.assertEqual(assembled["draft_artifact_id"], pending["draft_artifact_id"])
        self.assertNotIn("Batch-safe comparison", pending["first_draft_md"])
        self.assertEqual(1, len(proposal["changes"]))
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertNotEqual(assembled["draft_artifact_id"], payload["draft_artifact_id"])
        self.assertIn("Batch-safe comparison", payload["first_draft_md"])
        self.assertTrue(payload["quality"]["current"])
        self.assertEqual(payload["draft_artifact_id"], payload["quality"]["source_draft_artifact_id"])

    def test_batch_safe_optimization_can_be_discarded_without_changing_draft(self) -> None:
        with TestClient(self.app) as client:
            assembled = self.prepare_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/optimization-jobs",
                json={},
                headers=self.headers("batch-safe-discard"),
            )
            job = self.wait_job(client, started.json()["id"])
            pending = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            proposal = next(
                item
                for item in pending["optimization_proposals"]
                if item["status"] == "pending"
            )
            discarded = client.post(
                f"/api/v1/projects/{self.project_id}/draft/optimization-proposals/{proposal['proposal_id']}/reject",
                json={"revision": pending["revision"]},
                headers=self.headers("discard-batch-safe-optimize"),
            )
            after = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(200, discarded.status_code, discarded.text)
        self.assertEqual(assembled["draft_artifact_id"], after["draft_artifact_id"])
        self.assertNotIn("Batch-safe comparison", after["first_draft_md"])
        self.assertFalse(
            any(item["status"] == "pending" for item in after["optimization_proposals"])
        )

    def test_evaluation_goal_below_rubric_threshold_is_rejected_before_job_start(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            response = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={"goal": 80},
                headers=self.headers("invalid-draft-goal"),
            )
        self.assertEqual(422, response.status_code, response.text)

    def test_rewrite_is_candidate_only_rejects_noop_and_accepts_with_audit(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            evaluation = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={},
                headers=self.headers("rewrite-evaluate"),
            )
            self.wait_job(client, evaluation.json()["id"])
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            paragraph_id = current["paragraphs"][0]["paragraph_id"]
            rewrite = client.post(
                f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph_id}/rewrite-jobs",
                json={},
                headers=self.headers("rewrite-candidate"),
            )
            rewrite_job = self.wait_job(client, rewrite.json()["id"])
            candidate_id = rewrite_job["result"]["candidate_id"]
            unchanged = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            pending_candidate = next(
                item
                for item in unchanged["rewrite_candidates"]
                if item["candidate_id"] == candidate_id
            )
            accepted = client.post(
                f"/api/v1/projects/{self.project_id}/draft/rewrite-candidates/{candidate_id}/accept-jobs",
                json={"revision": unchanged["revision"]},
                headers=self.headers("accept-rewrite"),
            )
            accepted_job = self.wait_job(client, accepted.json()["id"])
            after_accept = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
            repeated = client.post(
                f"/api/v1/projects/{self.project_id}/draft/rewrite-candidates/{candidate_id}/accept-jobs",
                json={"revision": after_accept["revision"]},
                headers=self.headers("repeat-rewrite"),
            )
            reassembled = client.post(
                f"/api/v1/projects/{self.project_id}/draft/assemble",
                headers=self.headers("reassemble-accepted-rewrite"),
            )
            after_reassemble = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
        self.assertEqual("succeeded", rewrite_job["status"])
        self.assertNotIn("explicit", unchanged["first_draft_md"])
        self.assertEqual(60.0, pending_candidate["source_paragraph_score"])
        self.assertEqual(92.0, pending_candidate["candidate_paragraph_score"])
        self.assertEqual(202, accepted.status_code, accepted.text)
        self.assertEqual("succeeded", accepted_job["status"])
        self.assertEqual(0, self.accept_rewrite_model_calls)
        self.assertEqual(409, repeated.status_code, repeated.text)
        self.assertFalse(after_accept["freshness"]["upstream_stale"])
        self.assertTrue(after_accept["quality"]["current"])
        self.assertEqual("incremental_paragraph", after_accept["quality"]["quality_scope"])
        self.assertEqual(92.0, after_accept["quality"]["paragraph_scores"][0]["score"])
        self.assertGreater(after_accept["quality"]["score"], 72.5)
        self.assertEqual(200, reassembled.status_code, reassembled.text)
        self.assertIn("comparison is now explicit", after_reassemble["first_draft_md"])
        self.assertIn(
            after_reassemble["paragraphs"][0]["paragraph_id"],
            after_reassemble["overlay_replay"]["applied"],
        )

    def test_accepting_rewrite_supersedes_other_candidates_from_old_draft(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            evaluation = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={},
                headers=self.headers("multi-rewrite-evaluate"),
            )
            self.wait_job(client, evaluation.json()["id"])
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            paragraph_id = current["paragraphs"][0]["paragraph_id"]
            candidate_ids = []
            for index in range(2):
                started = client.post(
                    f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph_id}/rewrite-jobs",
                    json={},
                    headers=self.headers(f"multi-rewrite-{index}"),
                )
                job = self.wait_job(client, started.json()["id"])
                candidate_ids.append(job["result"]["candidate_id"])
            before = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            accepted = client.post(
                f"/api/v1/projects/{self.project_id}/draft/rewrite-candidates/{candidate_ids[0]}/accept-jobs",
                json={"revision": before["revision"]},
                headers=self.headers("multi-rewrite-accept"),
            )
            accepted_job = self.wait_job(client, accepted.json()["id"])
            after = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            stale_action = client.post(
                f"/api/v1/projects/{self.project_id}/draft/rewrite-candidates/{candidate_ids[1]}/accept-jobs",
                json={"revision": after["revision"]},
                headers=self.headers("multi-rewrite-stale-accept"),
            )
        self.assertEqual(202, accepted.status_code, accepted.text)
        self.assertEqual("succeeded", accepted_job["status"])
        statuses = {
            item["candidate_id"]: item["status"]
            for item in after["rewrite_candidates"]
        }
        self.assertEqual("accepted", statuses[candidate_ids[0]])
        self.assertEqual("superseded", statuses[candidate_ids[1]])
        self.assertEqual(409, stale_action.status_code, stale_action.text)

    def test_re_evaluation_makes_old_rewrite_candidate_stale(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            first_evaluation = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={}, headers=self.headers("candidate-quality-v1"),
            )
            self.wait_job(client, first_evaluation.json()["id"])
            first = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            paragraph_id = first["paragraphs"][0]["paragraph_id"]
            rewrite = client.post(
                f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph_id}/rewrite-jobs",
                json={}, headers=self.headers("candidate-before-reevaluation"),
            )
            candidate_id = self.wait_job(client, rewrite.json()["id"])["result"][
                "candidate_id"
            ]
            second_evaluation = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={}, headers=self.headers("candidate-quality-v2"),
            )
            self.wait_job(client, second_evaluation.json()["id"])
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            stale = client.post(
                f"/api/v1/projects/{self.project_id}/draft/rewrite-candidates/{candidate_id}/accept",
                json={"revision": current["revision"]},
                headers=self.headers("accept-old-quality-candidate"),
            )
        candidate = next(
            item for item in current["rewrite_candidates"]
            if item["candidate_id"] == candidate_id
        )
        self.assertEqual("stale", candidate["status"])
        self.assertEqual(409, stale.status_code, stale.text)

    def test_normalized_noop_rewrite_is_not_counted_as_candidate(self) -> None:
        self.noop_rewrite = True
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            evaluation = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={},
                headers=self.headers("noop-evaluation"),
            )
            self.wait_job(client, evaluation.json()["id"])
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            paragraph_id = current["paragraphs"][0]["paragraph_id"]
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph_id}/rewrite-jobs",
                json={},
                headers=self.headers("noop-rewrite"),
            )
            job = self.wait_job(client, started.json()["id"])
            payload = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        self.assertEqual("failed", job["status"])
        self.assertEqual([], payload["rewrite_candidates"])

    def test_rewrite_requires_a_current_evaluation_issue(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            paragraph_id = current["paragraphs"][0]["paragraph_id"]
            response = client.post(
                f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph_id}/rewrite-jobs",
                json={},
                headers=self.headers("rewrite-without-evaluation"),
            )
        self.assertEqual(409, response.status_code, response.text)

    def test_approval_is_bound_to_evaluated_current_draft(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            before = client.post(
                f"/api/v1/projects/{self.project_id}/draft/approve",
                json={"revision": 1},
                headers=self.headers("approval-before-evaluate"),
            )
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={},
                headers=self.headers("approval-evaluate"),
            )
            self.wait_job(client, started.json()["id"])
            current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            low = client.post(
                f"/api/v1/projects/{self.project_id}/draft/approve",
                json={"revision": current["revision"]},
                headers=self.headers("approval-low-score"),
            )
            approved = client.post(
                f"/api/v1/projects/{self.project_id}/draft/approve",
                json={"revision": current["revision"], "override_low_score": True, "override_reason": "Human review"},
                headers=self.headers("approval-override"),
            )
        self.assertEqual(409, before.status_code, before.text)
        self.assertEqual(409, low.status_code, low.text)
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertEqual("final", approved.json()["next_stage"])

    def test_hard_quality_findings_cannot_be_human_overridden(self) -> None:
        self.hard_gate_failures = ["citation_integrity_failed"]
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={},
                headers=self.headers("hard-gate-evaluate"),
            )
            self.assertEqual(
                "succeeded", self.wait_job(client, started.json()["id"])["status"]
            )
            current = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
            blocked = client.post(
                f"/api/v1/projects/{self.project_id}/draft/approve",
                json={"revision": current["revision"]},
                headers=self.headers("hard-gate-blocked"),
            )
            approved = client.post(
                f"/api/v1/projects/{self.project_id}/draft/approve",
                json={
                    "revision": current["revision"],
                    "override_low_score": True,
                    "override_reason": "Human verified the citations.",
                },
                headers=self.headers("hard-gate-override"),
            )
        self.assertEqual(409, blocked.status_code, blocked.text)
        self.assertEqual(409, approved.status_code, approved.text)

    def test_approval_cannot_mix_an_old_pass_score_with_a_new_quality_artifact(self) -> None:
        service = self.app.state.drafts_service
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={}, headers=self.headers("approval-snapshot-evaluate"),
            )
            self.assertEqual("succeeded", self.wait_job(client, started.json()["id"])["status"])
            old_payload = client.get(
                f"/api/v1/projects/{self.project_id}/draft"
            ).json()
        hard_fail = {
            "source_draft_artifact_id": old_payload["draft_artifact_id"],
            "score": 20,
            "goal": 90,
            "hard_gate_failures": ["citation_integrity_failed"],
            "issues": [],
        }
        _published, state = service._publish_files(
            self.first,
            self.project_id,
            {
                DRAFT_QUALITY: (
                    (json.dumps(hard_fail, ensure_ascii=False) + "\n").encode(),
                    "json",
                )
            },
            expected_revision=old_payload["revision"],
            metadata={"operation": "concurrent-hard-fail-evaluation"},
            expected_current_artifacts={
                DRAFT_DOCUMENT: old_payload["draft_artifact_id"]
            },
        )
        original_get = service.get
        service.get = lambda *_args, **_kwargs: old_payload
        try:
            with self.assertRaises(WorkflowConflict):
                service.approve(
                    self.first,
                    self.project_id,
                    revision=state.revision,
                    override_low_score=True,
                    override_reason="Human review",
                )
        finally:
            service.get = original_get


if __name__ == "__main__":
    import unittest

    unittest.main()
