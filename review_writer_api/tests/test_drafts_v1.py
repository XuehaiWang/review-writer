from __future__ import annotations

import json
import re
import time
import uuid

from fastapi.testclient import TestClient

from review_writer_api.domain_services.drafts import (
    DRAFT_DOCUMENT,
    DRAFT_QUALITY,
    SECTION_EVIDENCE,
    DraftsService,
)
from review_writer_api.errors import WorkflowConflict
from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase
from review_writer_api.workflow_models import WorkflowApproval


class DraftsV1Tests(NativeFigureApiTestCase):
    def test_empty_paragraph_deletes_only_prose_and_preserves_history(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            before = client.get(base).json()
            paragraph = before["paragraphs"][0]
            url = base + "/paragraphs/" + paragraph["paragraph_id"]
            payload = {"revision": before["revision"], "text": "", "base_text_sha256": "0" * 64}
            self.assertEqual(409, client.put(url, json=payload).status_code)
            payload["base_text_sha256"] = paragraph["text_sha256"]
            saved = client.put(url, json=payload)
            self.assertEqual(200, saved.status_code, saved.text)
            after = client.get(base).json()
            self.assertNotIn(paragraph["paragraph_id"], [p["paragraph_id"] for p in after["paragraphs"]])
            self.assertEqual([p["paragraph_id"] for p in before["paragraphs"][1:]], [p["paragraph_id"] for p in after["paragraphs"]])
            for line in before["first_draft_md"].splitlines():
                if line.startswith(("![", "*Figure", "## References")):
                    self.assertIn(line, after["first_draft_md"])
            old = client.get(f"/api/v1/artifacts/{before['draft_artifact_id']}/content")
            self.assertIn(paragraph["text"], old.text)
            self.assertEqual(404, client.put(url, json=payload).status_code)

    def test_figure_only_change_allows_manual_save_without_approving_stale_draft(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            before = service.get(self.first, self.project_id)
            state = service.repository.get_stage_state(self.first.user_id, self.project_id, "figure-review")
            response = client.put(f"/api/v1/projects/{self.project_id}/figures/review/P001",
                json={"revision": state.revision, "candidate_index": 0, "review_note": "change image"},
                headers=self.headers("edit-figure-before-prose"))
            self.assertEqual(200, response.status_code, response.text)
            current = service.get(self.first, self.project_id)
            self.assertTrue(current["freshness"]["upstream_stale"])
            self.assertFalse(current["freshness"]["editing_blocked"])
            paragraph = current["paragraphs"][0]
            payload = {"text": paragraph["text"] + " Manual correction.", "revision": current["revision"],
                       "base_text_sha256": paragraph["text_sha256"]}
            url = f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph['paragraph_id']}"
            saved = client.put(url, json=payload)
            self.assertEqual(200, saved.status_code, saved.text)
            after = service.get(self.first, self.project_id)
            self.assertIn("Manual correction.", after["first_draft_md"])
            self.assertTrue(after["freshness"]["upstream_stale"])
            self.assertFalse(after["draft_approval_current"])
            self.assertEqual(409, client.put(url, json=payload).status_code)
            self.assertEqual(200, client.get(f"/api/v1/artifacts/{before['draft_artifact_id']}/content").status_code)

    def test_empty_draft_guides_to_figure_approval_before_auto_assembly(self):
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/projects/{self.project_id}/draft")
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual("WORKFLOW_STAGE_NOT_READY", response.json()["error"]["code"])

    def setUp(self) -> None:
        self.noop_rewrite = False
        self.hard_gate_failures: list[str] = []
        self.accept_rewrite_model_calls = 0
        self.rewrite_source_evidence_refs: list[str] = []
        self.rewrite_source_check_entry: dict = {}
        self.rewrite_unsupported_claims_before: list[str] = []
        self.rewrite_unsupported_claims_after: list[str] = []
        super().setUp()

    def extra_native_workflow_overrides(self) -> dict:
        def evaluate(_context, payload):
            paragraph_id = payload["paragraphs"][0]["paragraph_id"]
            return {
                "score": getattr(self, "evaluation_score", 72.5),
                "goal": float(payload.get("goal") or 90),
                "decision": "REVISE",
                "dimension_scores": [{"id": "evidence", "score": 72.5}],
                "paragraph_scores": [
                    {
                        "paragraph_id": paragraph_id,
                        "score": 60,
                        "severity": "major",
                        "route": "section_rewrite",
                        "failed_dimensions": ["P01"] if getattr(self, "evaluation_style_only", False) else [],
                    }
                ],
                "issues": [
                    {
                        "issue_id": "issue-1",
                        "paragraph_id": paragraph_id,
                        "severity": "major",
                        "message": "Improve sentence rhythm." if getattr(self, "evaluation_style_only", False) else "Strengthen the evidence comparison.",
                        "rule": "P01" if getattr(self, "evaluation_style_only", False) else "C01" if getattr(self, "evaluation_substantive", False) else "",
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
                        "source_check_status": "partially_supported",
                        "unsupported_claims": list(
                            self.rewrite_unsupported_claims_before
                        ),
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
                        "source_evidence_refs": list(
                            self.rewrite_source_evidence_refs
                        ),
                        "unsupported_claims": list(
                            self.rewrite_unsupported_claims_after
                        ),
                    },
                    "local_dimension_scores": [],
                    "local_hard_gate_failures": [],
                    "local_preflight": {
                        "paragraph_checks": [],
                        "paragraph_findings": [],
                    },
                    "source_check_entry": (
                        dict(self.rewrite_source_check_entry)
                        if self.rewrite_source_check_entry
                        else {
                            "paragraph_id": payload["paragraph_id"],
                            "source_check_status": "verified",
                        }
                    ),
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

    def test_evaluation_snapshot_tracks_blueprint_and_figures_and_rejects_stale_sections(self):
        from review_writer_core.workflow.artifacts import BLUEPRINT, FIGURE_MANIFEST

        service = self.app.state.drafts_service
        repository = self.app.state.workflow_repository
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            queued = service.evaluation_payload(self.first, self.project_id, goal=90)
            for field, logical_name in (
                ("source_blueprint_artifact_id", BLUEPRINT),
                ("source_figure_manifest_artifact_id", FIGURE_MANIFEST),
            ):
                current = repository.get_current_artifact(self.first.user_id, self.project_id, logical_name)
                self.assertEqual(current.id if current else "", queued[field])
            self.publish_changed_sections()
            with self.assertRaises(WorkflowConflict):
                service.validate_task_inputs(self.first, self.project_id, queued)
            with self.assertRaises(WorkflowConflict):
                service.publish_evaluation(self.first, self.project_id, queued, {"score": 100})

    def test_stale_upstream_state_cannot_be_reapproved_using_old_quality(self):
        service = self.app.state.drafts_service
        repository = self.app.state.workflow_repository
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            queued = service.evaluation_payload(self.first, self.project_id, goal=90)
            service.publish_evaluation(self.first, self.project_id, queued, {"score": 95, "issues": []})
            before = service.get(self.first, self.project_id)
            self.assertTrue(before["quality"]["current"])
            state = repository.compare_and_set_stage(
                self.first.user_id, self.project_id, "draft", before["revision"], status="stale",
            )
            after = service.get(self.first, self.project_id)
            self.assertFalse(after["quality"]["current"])
            self.assertTrue(after["freshness"]["upstream_stale"])
            with self.assertRaises(WorkflowConflict):
                service.validate_task_inputs(self.first, self.project_id, {
                    "source_draft_artifact_id": after["draft_artifact_id"],
                    "expected_revision": state.revision,
                })
            with self.assertRaises(WorkflowConflict):
                service.approve(self.first, self.project_id, revision=state.revision,
                                override_low_score=False, override_reason="")

    def seed_section_evidence_package(self) -> str:
        repository = self.app.state.workflow_repository
        state = repository.get_stage_state(
            self.first.user_id, self.project_id, "sections"
        )
        run = repository.create_stage_run(
            self.first.user_id, self.project_id, "sections", status="succeeded"
        )
        package = {
            "schema_version": 2,
            "project_id": self.project_id,
            "evidence_registry": [],
            "sections": [
                {
                    "section_id": "S1",
                    "status": "insufficient_evidence",
                    "hits": [],
                    "primary_paper_states": [
                        {
                            "paper_id": "P001",
                            "status": "unresolved",
                            "diagnostic": "missing_direct_evidence",
                        }
                    ],
                    "query_plans": [
                        {
                            "question_id": "section_focus",
                            "status": "insufficient",
                            "required_for_section": True,
                            "coverage_policy": "all_primary",
                        }
                    ],
                }
            ],
        }
        artifact = self._publish(
            run.id,
            SECTION_EVIDENCE,
            "evidence-package.json",
            (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode(),
            "json",
        )
        repository.promote_stage_artifacts_atomically(
            self.first.user_id,
            self.project_id,
            "sections",
            artifact_ids={SECTION_EVIDENCE: artifact.id},
            run_id=run.id,
            expected_revision=state.revision,
            status="approved",
            invalidate_stages=("figure-review", "figures", "draft", "final"),
        )
        return artifact.id

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

    def test_assembly_preserves_grouped_citations_and_bound_transitions(self) -> None:
        paragraph = {
            "paragraph_id": "S01-p1", "paper_id": "P002",
            "text": "We next compare the methods. First result. Second result. [16] Other study. [7]",
            "claim_realizations": [
                {"claim_id": "a", "text": "First result.", "citation_group": ["P002"], "claim_kind": "reported_finding"},
                {"claim_id": "b", "text": "Second result.", "citation_group": ["P002"], "claim_kind": "reported_finding"},
                {"claim_id": "c", "text": "Other study.", "citation_group": ["P001"], "claim_kind": "reported_finding"},
            ],
        }
        markdown = self.app.state.drafts_service._assemble_markdown(
            "Review", {"sections": [{"section_id": "S01", "heading": "Methods", "paragraphs": [paragraph]}]},
            {"figures": []}, {"rows": [{"paper_id": "P002", "title": "Two"}, {"paper_id": "P001", "title": "One"}]},
        )
        self.assertIn("We next compare the methods. First result. Second result. [1] Other study. [2]", markdown)
        self.assertNotIn("[16]", markdown)

    def test_current_insertion_plan_skips_unplaced_pool_assets(self) -> None:
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
                                "text": "The study reports a transformation.",
                            }
                        ],
                    }
                ]
            },
            {
                "insertion_plan": [
                    {
                        "figure_id": "P001-F01",
                        "include": False,
                        "skip_reason": "no_supported_paragraph",
                    }
                ],
                "figures": [
                    {
                        "figure_id": "P001-F01",
                        "paper_id": "P001",
                        "target_paragraph_id": "S01-p1",
                        "output_artifact_id": "artifact-1",
                        "status": "redrawn",
                    }
                ],
            },
            {"rows": [{"paper_id": "P001", "title": "Study one"}]},
        )
        self.assertNotIn("artifact-1", markdown)
        self.assertNotIn("Figure 1", markdown)

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
        self.assertIn("(Figure 1)", prose)
        self.assertNotIn("visual context", prose)
        self.assertIn(
            "Figure 1. Pd₂(dba)₃·CHCl₃, (S)-(−)-MeO-MOP, CHCl₃; −78 °C",
            figure_block,
        )
        self.assertNotIn("$", figure_block)


    def test_legacy_quality_issue_gets_consistent_response_only_repair_route(self) -> None:
        legacy = {
            "issue_id": "legacy-1",
            "paragraph_id": "S01-p2",
            "source_check_status": "needs_human_review",
            "route": "local_source_recheck",
            "recommended_return_stage": "planning",
            "recommended_action": "Correct Matrix before continuing.",
            "message": "A required claim is not supported by the local source.",
        }

        public = DraftsService._public_issue_repair_metadata(legacy)

        self.assertNotIn("repair_stage", legacy)
        self.assertEqual("evidence_package", public["repair_stage"])
        self.assertEqual("draft", public["recommended_return_stage"])
        self.assertFalse(public["rewrite_eligible"])
        self.assertNotEqual(legacy["recommended_action"], public["recommended_action"])

    def test_current_quality_issue_keeps_persisted_repair_route(self) -> None:
        current = {
            "issue_id": "current-1",
            "repair_stage": "figures",
            "repair_action": "custom_current_action",
            "recommended_return_stage": "draft",
        }

        public = DraftsService._public_issue_repair_metadata(current)

        self.assertEqual(current, public)

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

    def test_paragraph_token_merges_unrelated_edits_and_rejects_same_paragraph_conflict(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            current = client.get(base).json()
            # Guarantee two distinct paragraph identities without depending on
            # the scientific topic used by the fixture.
            added = client.put(base, json={"revision": current["revision"],
                "text": current["first_draft_md"] + "\n\nSecond paragraph.\n<!-- paragraph_id: independent-p2 -->\n"})
            self.assertEqual(200, added.status_code, added.text)
            snapshot = client.get(base).json()
            first = snapshot["paragraphs"][0]
            second = next(p for p in snapshot["paragraphs"] if p["paragraph_id"] == "independent-p2")
            def save(p, suffix):
                return client.put(f"{base}/paragraphs/{p['paragraph_id']}", json={
                    "revision": snapshot["revision"], "text": p["text"] + suffix,
                    "base_text_sha256": p["text_sha256"],
                })
            self.assertEqual(200, save(first, " First change.").status_code)
            saved_second = save(second, " Second change.")
            self.assertEqual(200, saved_second.status_code, saved_second.text)
            conflict = save(first, " Stale overwrite.")
            self.assertEqual(409, conflict.status_code, conflict.text)
            after = client.get(base).json()
            self.assertIn("First change.", after["first_draft_md"])
            self.assertIn("Second change.", after["first_draft_md"])
            self.assertNotIn("Stale overwrite.", after["first_draft_md"])

    def test_noop_paragraph_save_preserves_approval_and_version(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            snapshot = client.get(base).json()
            approved = client.post(f"{base}/approve", json={"revision": snapshot["revision"]})
            self.assertEqual(200, approved.status_code, approved.text)
            snapshot = client.get(base).json()
            paragraph = snapshot["paragraphs"][0]
            saved = client.put(f"{base}/paragraphs/{paragraph['paragraph_id']}", json={
                "revision": snapshot["revision"], "text": paragraph["text"],
                "base_text_sha256": paragraph["text_sha256"],
            })
            self.assertEqual(200, saved.status_code, saved.text)
            self.assertFalse(saved.json()["changed"])
            after = client.get(base).json()
            self.assertEqual(snapshot["revision"], after["revision"])
            self.assertEqual(snapshot["draft_artifact_id"], after["draft_artifact_id"])
            self.assertTrue(after["draft_approval_current"])
            self.app.state.final_service._approved_draft(self.first, self.project_id)

    def test_approval_does_not_expire_an_in_flight_analysis(self):
        service = self.app.state.drafts_service
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            payload = service.evaluation_payload(self.first, self.project_id, goal=90)
            approved = service.approve(self.first, self.project_id,
                revision=payload["expected_revision"], override_low_score=False, override_reason="")
            service.validate_task_inputs(self.first, self.project_id, payload)
            result = service.publish_evaluation(self.first, self.project_id, payload,
                {"score": 70, "goal": 90, "issues": [], "hard_gate_failures": []})
            self.assertGreater(result["revision"], approved["revision"])
            self.assertTrue(service.get(self.first, self.project_id)["draft_approval_current"])

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
