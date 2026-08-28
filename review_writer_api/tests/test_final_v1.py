from __future__ import annotations

import re
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from review_writer_api.database import Project
from review_writer_api.domain_services.final import (
    FinalService,
    _clean_reference_affiliation_markup,
    _normalize_publication_markup,
)
from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_core.latex_renderer import TEMPLATE_VERSION
from review_writer_core.manuscript_state import build_manuscript_state


class FinalV1Tests(NativeFigureApiTestCase):
    def setUp(self) -> None:
        self.block_conclusion_return = False
        self.conclusion_built = threading.Event()
        self.release_conclusion = threading.Event()
        self.evaluation_hard_failures: list[str] = []
        self.evaluation_score = 95
        super().setUp()

    def extra_native_workflow_overrides(self) -> dict:
        def evaluate(_context, payload):
            return {
                "score": self.evaluation_score,
                "goal": 90,
                "decision": "PASS",
                "dimension_scores": [],
                "paragraph_scores": [],
                "issues": [],
                "hard_gate_failures": list(self.evaluation_hard_failures),
            }

        def conclusion(_context, _payload):
            if self.block_conclusion_return:
                self.conclusion_built.set()
                self.release_conclusion.wait(3)
            return {
                "markdown": "## Conclusion\n\nCopper reactivity supports a bounded outlook.\n",
                "report": {"validation": {"passes_validation": True}},
            }

        def front_matter(_context, payload):
            self.assertNotIn("## Conclusion", payload["abstract_source"])
            return {
                "abstract": "This review synthesizes the approved body evidence without using a generated conclusion.",
                "keywords": ["copper catalysis", "evidence synthesis", "selectivity"],
                "warnings": [],
            }

        def overview(context, _payload):
            output = (
                self.root
                / "users"
                / context.user_id
                / ".review-writer"
                / "test-final"
                / "overview.png"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 32), "white").save(output)
            return {
                "output_path": str(output),
                "editable_text": {
                    "title": "Copper overview",
                    "subtitle": "Mechanistic classes",
                    "labels": ["activation", "allenation"],
                },
                "report": {"template": "mechanism-overview"},
            }

        def export(context, export_payload):
            referenced = {
                part.split("/", 1)[0]
                for part in export_payload["final_markdown"].split("/api/v1/artifacts/")[1:]
            }
            missing = referenced - set(export_payload.get("figure_artifact_paths") or {})
            if missing:
                raise RuntimeError(f"DOCX payload did not resolve artifacts: {sorted(missing)}")
            output = (
                self.root
                / "users"
                / context.user_id
                / ".review-writer"
                / "test-final"
                / "review.docx"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(
                    "[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
                )
                archive.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Review</w:t></w:r></w:p></w:body></w:document>',
                )
            return {"output_path": str(output), "download_name": "review.docx"}

        def pdf(context, pdf_payload):
            output_dir = (
                self.root
                / "users"
                / context.user_id
                / ".review-writer"
                / "test-final-pdf"
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            output = output_dir / "review.pdf"
            tex = output_dir / "review.tex"
            log = output_dir / "compile.log"
            output.write_bytes(b"%PDF-1.7\n% test-pdf\n")
            tex.write_text("\\documentclass{article}\\begin{document}test\\end{document}\n", encoding="utf-8")
            log.write_text("LuaHBTeX test; shell escape disabled\n", encoding="utf-8")
            state = build_manuscript_state(
                pdf_payload["final_markdown"],
                artifact_paths=pdf_payload.get("figure_artifact_paths") or {},
            )
            profile = pdf_payload["language_profile"]
            return {
                "output_path": str(output),
                "tex_path": str(tex),
                "compile_log_path": str(log),
                "manuscript_state": state,
                "render_manifest": {
                    "schema_version": 1,
                    "template": "modern-survey",
                    "template_version": TEMPLATE_VERSION,
                    "language_profile": profile,
                    "compiler": "LuaHBTeX test",
                    "shell_escape": False,
                    "source_final_artifact_id": pdf_payload["source_final_artifact_id"],
                    "source_release_artifact_id": pdf_payload["source_release_artifact_id"],
                    "source_markdown_sha256": state["source_markdown_sha256"],
                    "semantic_sha256": state["semantic_sha256"],
                    "asset_sha256": {
                        artifact_id: "sha256:test"
                        for artifact_id in pdf_payload.get(
                            "figure_artifact_paths", {}
                        )
                    },
                },
                "pdf_qa": {
                    "schema_version": 1,
                    "status": "pass",
                    "page_count": 3,
                    "all_fonts_embedded": True,
                    "blocking_issues": [],
                    "warning_issues": [],
                },
                "download_name": f"review.{profile}.pdf",
            }

        return {
            "draft.evaluate": evaluate,
            "final.build": front_matter,
            "final.conclusion": conclusion,
            "final.overview": overview,
            "final.export": export,
            "final.pdf": pdf,
        }

    def prepare_approved_draft(self, client: TestClient) -> dict:
        self.confirm_review(client)
        self.assertEqual("succeeded", self.start_redraw(client, "final-redraw")["status"])
        figures = client.get(f"/api/v1/projects/{self.project_id}/figures").json()
        self.assertEqual(
            200,
            client.post(
                f"/api/v1/projects/{self.project_id}/figures/confirm",
                json={"revision": figures["revision"]},
                headers=self.headers("final-figure-confirm"),
            ).status_code,
        )
        assembled = client.post(
            f"/api/v1/projects/{self.project_id}/draft/assemble",
            headers=self.headers("final-assemble"),
        )
        self.assertEqual(200, assembled.status_code, assembled.text)
        evaluation = client.post(
            f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
            json={},
            headers=self.headers("final-evaluate"),
        )
        self.assertEqual("succeeded", self.wait_job(client, evaluation.json()["id"])["status"])
        draft = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        approved = client.post(
            f"/api/v1/projects/{self.project_id}/draft/approve",
            json={"revision": draft["revision"]},
            headers=self.headers("final-draft-approve"),
        )
        self.assertEqual(200, approved.status_code, approved.text)
        return approved.json()

    def revise_and_approve_draft(self, client: TestClient, key: str) -> dict:
        draft = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        paragraph = draft["paragraphs"][0]
        edited = client.put(
            f"/api/v1/projects/{self.project_id}/draft/paragraphs/{paragraph['paragraph_id']}",
            json={"text": paragraph["text"] + " Revised.", "revision": draft["revision"]},
            headers=self.headers(f"{key}-edit"),
        )
        self.assertEqual(200, edited.status_code, edited.text)
        evaluation = client.post(
            f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
            json={},
            headers=self.headers(f"{key}-evaluate"),
        )
        self.assertEqual("succeeded", self.wait_job(client, evaluation.json()["id"])["status"])
        current = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
        approved = client.post(
            f"/api/v1/projects/{self.project_id}/draft/approve",
            json={"revision": current["revision"]},
            headers=self.headers(f"{key}-approve"),
        )
        self.assertEqual(200, approved.status_code, approved.text)
        return approved.json()

    def test_final_is_blocked_until_exact_draft_is_approved(self) -> None:
        with TestClient(self.app) as client:
            blocked = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("final-blocked"),
            )
            self.prepare_approved_draft(client)
            ready = client.get(f"/api/v1/projects/{self.project_id}/final")
        self.assertEqual(409, blocked.status_code, blocked.text)
        self.assertEqual(200, ready.status_code, ready.text)
        self.assertTrue(ready.json()["draft_approval_current"])

    def test_default_front_matter_summarizes_an_instruction_style_title(self) -> None:
        topic = (
            'Please write a review on the topic “allenation-of-terminal-alkynes (ATA)”, '
            'focusing on different substrates. Organize the review by reaction type '
            'and catalytic/promoting system.'
        )

        front_matter = FinalService._default_front_matter(
            f"# {topic}\n\n## Introduction\n\nBody.",
            fallback_title=topic,
            source_draft_artifact_id="draft-1",
        )

        self.assertEqual(
            "Allenation of Terminal Alkynes (ATA): Reaction Classes and Catalytic Strategies",
            front_matter["title"],
        )
        self.assertEqual("generated", front_matter["field_states"]["title"])

    def test_final_build_job_reports_progress_and_is_recoverable(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/final/build-jobs",
                json={},
                headers=self.headers("final-build-job"),
            )
            self.assertEqual(202, started.status_code, started.text)
            completed = self.wait_job(client, started.json()["id"])
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/final"
            ).json()
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual(4, completed["progress_current"])
        self.assertEqual(4, completed["progress_total"])
        self.assertEqual(started.json()["id"], payload["latest_final_job_id"])
        self.assertEqual("final.build", payload["latest_final_job_type"])
        self.assertEqual("succeeded", payload["latest_final_job_status"])
        self.assertTrue(payload["final_current"])
        self.assertIn("## Abstract", payload["final_draft_md"])
        self.assertIn("**Authors:** First", payload["final_draft_md"])
        self.assertEqual("generated", payload["front_matter"]["field_states"]["abstract"])

    def test_overview_block_is_inserted_immediately_before_introduction(self) -> None:
        source = "# Review title\n\nAbstract text.\n\n## 1. Introduction\n\nOpening."
        marker = "![Review overview](/api/v1/artifacts/overview/content)"
        assembled = self.app.state.final_service._insert_before_introduction(
            source, marker
        )
        self.assertLess(assembled.index(marker), assembled.index("## 1. Introduction"))
        self.assertGreater(assembled.index(marker), assembled.index("Abstract text."))

    def test_reference_affiliation_residue_is_removed_without_losing_science(self) -> None:
        markdown = (
            "# Review\n\nA result <sup>2</sup> was reported.\n\n"
            "## References\n\n"
            "[1] Yuli Wang<sup></sup> and Shengming Ma<sup>, </sup>. Article.\n"
            "[2] Ogasawara, <sup>[]</sup> Yonghui Ge. Article.\n"
            "[3] Isotope-labeling with <sup>13</sup>C. Article.\n"
            "[4] Zhaoqiang Chen, <sup>∥</sup> Huanan Wang, <sup>‖</sup> Ping Du. Article.\n"
        )

        cleaned = _clean_reference_affiliation_markup(markdown)

        self.assertIn("A result <sup>2</sup> was reported.", cleaned)
        self.assertIn("Yuli Wang and Shengming Ma. Article.", cleaned)
        self.assertIn("Ogasawara, Yonghui Ge. Article.", cleaned)
        self.assertIn("<sup>13</sup>C", cleaned)
        self.assertNotIn("<sup></sup>", cleaned)
        self.assertNotIn("<sup>, </sup>", cleaned)
        self.assertNotIn("<sup>[]</sup>", cleaned)
        self.assertIn("Zhaoqiang Chen, Huanan Wang, Ping Du. Article.", cleaned)
        self.assertNotIn("<sup>∥</sup>", cleaned)
        self.assertNotIn("<sup>‖</sup>", cleaned)

    def test_publication_markup_normalization_preserves_content_and_comments(self) -> None:
        markdown = (
            "# Review\n\n"
            "<!-- paragraph_id: S01-p1 -->\n"
            "A <strong>supported</strong> result used H<sub>2</sub>O and x<sup>2</sup>."
            "<br><span class=\"note\">Further context</span>.\n\n"
            "<script>discard this</script>\n\n"
            "## References\n\n"
            "[1] Chen, <sup>∥</sup> Wang. Article.\n"
        )

        normalized = _normalize_publication_markup(markdown)

        self.assertIn("<!-- paragraph_id: S01-p1 -->", normalized)
        self.assertIn("A **supported** result used H₂O and x².", normalized)
        self.assertIn("Further context.", normalized)
        self.assertIn("[1] Chen, Wang. Article.", normalized)
        self.assertNotIn("discard this", normalized)
        visible = re.sub(r"<!--.*?-->", "", normalized, flags=re.DOTALL)
        self.assertIsNone(re.search(r"</?[A-Za-z][^>]*>", visible))

    def test_re_evaluation_invalidates_old_draft_approval_and_blocks_final(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            self.evaluation_score = 20
            self.evaluation_hard_failures = ["citation_integrity_failed"]
            re_evaluation = client.post(
                f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                json={}, headers=self.headers("re-evaluate-approved-draft"),
            )
            self.assertEqual(
                "succeeded",
                self.wait_job(client, re_evaluation.json()["id"])["status"],
            )
            draft = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            blocked = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("final-after-hard-fail-reevaluation"),
            )
        self.assertFalse(draft["draft_approval_current"])
        self.assertFalse(final["draft_approval_current"])
        self.assertEqual(409, blocked.status_code, blocked.text)

    def test_concurrent_hard_fail_re_evaluation_blocks_in_flight_final_build(self) -> None:
        validation_started = threading.Event()
        release_validation = threading.Event()
        service = self.app.state.final_service
        original_validate = service._validate_markdown

        def delayed_validate(*args, **kwargs):
            validation_started.set()
            release_validation.wait(3)
            return original_validate(*args, **kwargs)

        service._validate_markdown = delayed_validate
        build_result: dict[str, object] = {}

        def build_in_thread() -> None:
            try:
                build_result["payload"] = service.build(self.first, self.project_id)
            except Exception as exc:  # the exact conflict type is an API concern
                build_result["error"] = exc

        try:
            with TestClient(self.app) as client:
                self.prepare_approved_draft(client)
                worker = threading.Thread(target=build_in_thread, daemon=True)
                worker.start()
                self.assertTrue(validation_started.wait(2))
                self.evaluation_score = 20
                self.evaluation_hard_failures = ["citation_integrity_failed"]
                re_evaluation = client.post(
                    f"/api/v1/projects/{self.project_id}/draft/evaluation-jobs",
                    json={}, headers=self.headers("re-evaluate-during-final-build"),
                )
                self.assertEqual(
                    "succeeded",
                    self.wait_job(client, re_evaluation.json()["id"])["status"],
                )
                release_validation.set()
                worker.join(3)
                final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
        finally:
            release_validation.set()
            service._validate_markdown = original_validate
        self.assertFalse(worker.is_alive())
        self.assertIn("error", build_result)
        self.assertFalse(final["final_artifact_id"])
        self.assertFalse(final["release_artifact_id"])

    def test_concurrent_library_source_removal_does_not_block_final_release(self) -> None:
        publish_started = threading.Event()
        release_publish = threading.Event()
        service = self.app.state.final_service
        original_publish = service._publish_files

        def delayed_publish(*args, **kwargs):
            publish_started.set()
            release_publish.wait(3)
            return original_publish(*args, **kwargs)

        service._publish_files = delayed_publish
        build_result: dict[str, object] = {}

        def build_in_thread() -> None:
            try:
                build_result["payload"] = service.build(self.first, self.project_id)
            except Exception as exc:
                build_result["error"] = exc

        try:
            with TestClient(self.app) as client:
                self.prepare_approved_draft(client)
                worker = threading.Thread(target=build_in_thread, daemon=True)
                worker.start()
                self.assertTrue(publish_started.wait(2))
                with self.sessions.begin() as session:
                    paper = session.scalar(
                        select(LibraryPaper).where(
                            LibraryPaper.user_id == uuid.UUID(self.first.user_id),
                            LibraryPaper.paper_id == "P001",
                        )
                    )
                    artifacts = session.scalars(
                        select(LibraryArtifact).where(
                            LibraryArtifact.user_id == uuid.UUID(self.first.user_id),
                            LibraryArtifact.paper_id == "P001",
                        )
                    ).all()
                    paper.status = "deleted"
                    paper.deleted_at = paper.updated_at
                    for artifact in artifacts:
                        artifact.availability = "trashed"
                release_publish.set()
                worker.join(3)
                final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
        finally:
            release_publish.set()
            service._publish_files = original_publish
        self.assertFalse(worker.is_alive())
        self.assertIn("payload", build_result)
        self.assertTrue(final["release_artifact_id"])
        self.assertTrue(final["release_current"])

    def test_released_final_remains_exportable_when_a_library_source_is_removed(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("build-before-source-removal"),
            )
            self.assertEqual(200, built.status_code, built.text)
            with self.sessions.begin() as session:
                paper = session.scalar(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == uuid.UUID(self.first.user_id),
                        LibraryPaper.paper_id == "P001",
                    )
                )
                artifacts = session.scalars(
                    select(LibraryArtifact).where(
                        LibraryArtifact.user_id == uuid.UUID(self.first.user_id),
                        LibraryArtifact.paper_id == "P001",
                    )
                ).all()
                paper.status = "deleted"
                paper.deleted_at = paper.updated_at
                for artifact in artifacts:
                    artifact.availability = "trashed"
            final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            exported = client.post(
                f"/api/v1/projects/{self.project_id}/final/export-jobs",
                json={}, headers=self.headers("export-after-source-removal"),
            )
        self.assertTrue(final["release_current"])
        self.assertEqual(202, exported.status_code, exported.text)

    def test_cancel_after_builder_before_publish_admits_no_conclusion(self) -> None:
        self.block_conclusion_return = True
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                json={},
                headers=self.headers("cancel-before-publish"),
            )
            self.assertEqual(202, started.status_code, started.text)
            self.assertTrue(self.conclusion_built.wait(2))
            cancelled = client.post(
                f"/api/v1/jobs/{started.json()['id']}/cancel",
                headers={"Origin": "http://testserver"},
            )
            self.assertIn(cancelled.json()["status"], {"cancel_requested", "cancelled"})
            self.release_conclusion.set()
            job = self.wait_job(client, started.json()["id"])
            final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
        self.assertEqual("cancelled", job["status"])
        self.assertEqual("", final["conclusion_artifact_id"])

    def test_cancel_after_publication_keeps_successful_job_and_output(self) -> None:
        repository = self.app.state.workflow_repository
        original = repository.mark_job_succeeded
        publication_complete = threading.Event()
        release_completion = threading.Event()

        def delayed_completion(job_id, result=None):
            publication_complete.set()
            release_completion.wait(3)
            return original(job_id, result)

        try:
            with TestClient(self.app) as client:
                self.prepare_approved_draft(client)
                # Install the commit-point delay only after prerequisite jobs
                # have finished; otherwise their success callbacks can set the
                # event before the conclusion job reaches publication.
                repository.mark_job_succeeded = delayed_completion
                started = client.post(
                    f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                    json={},
                    headers=self.headers("cancel-after-publish"),
                )
                self.assertTrue(publication_complete.wait(2))
                cancelled = client.post(
                    f"/api/v1/jobs/{started.json()['id']}/cancel",
                    headers={"Origin": "http://testserver"},
                )
                self.assertEqual("cancel_requested", cancelled.json()["status"])
                release_completion.set()
                job = self.wait_job(client, started.json()["id"])
                final = client.get(
                    f"/api/v1/projects/{self.project_id}/final"
                ).json()
        finally:
            release_completion.set()
            repository.mark_job_succeeded = original
        self.assertEqual("succeeded", job["status"])
        self.assertTrue(final["conclusion_artifact_id"])

    def test_conclusion_overview_edit_and_final_build_are_versioned(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            conclusion = client.post(
                f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                json={},
                headers=self.headers("final-conclusion"),
            )
            self.assertEqual("succeeded", self.wait_job(client, conclusion.json()["id"])["status"])
            overview = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={},
                headers=self.headers("final-overview"),
            )
            self.assertEqual("succeeded", self.wait_job(client, overview.json()["id"])["status"])
            current = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            report = client.get(
                f"/api/v1/artifacts/{current['conclusion_report_artifact_id']}/content"
            )
            old_text_id = current["overview_text_artifact_id"]
            edited = client.put(
                f"/api/v1/projects/{self.project_id}/final/overview-text",
                json={
                    "revision": current["revision"],
                    "title": "Edited copper overview",
                    "subtitle": "Verified classes",
                    "labels": ["activation", "coupling"],
                },
                headers=self.headers("final-overview-edit"),
            )
            self.assertEqual(200, edited.status_code, edited.text)
            unchanged = client.put(
                f"/api/v1/projects/{self.project_id}/final/overview-text",
                json={
                    "revision": edited.json()["revision"],
                    "title": "Edited copper overview",
                    "subtitle": "Verified classes",
                    "labels": ["activation", "coupling"],
                },
                headers=self.headers("final-overview-noop"),
            )
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("final-build"),
            )
            old_text = client.get(f"/api/v1/artifacts/{old_text_id}/content")
            payload = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            audit = client.get(
                f"/api/v1/artifacts/{payload['validation_artifact_id']}/content"
            )
            release = client.get(
                f"/api/v1/artifacts/{payload['release_artifact_id']}/content"
            )
        self.assertEqual(200, built.status_code, built.text)
        self.assertEqual(422, unchanged.status_code, unchanged.text)
        self.assertIn("Conclusion", payload["final_draft_md"])
        self.assertIn("/api/v1/artifacts/", payload["final_draft_md"])
        self.assertEqual("Edited copper overview", payload["overview_text"]["title"])
        self.assertEqual(200, report.status_code, report.text)
        self.assertTrue(report.json()["validation"]["passes_validation"])
        self.assertNotIn("Edited copper overview", old_text.text)
        self.assertTrue(payload["validation"]["valid"])
        self.assertTrue(payload["release_current"])
        self.assertEqual("released", release.json()["status"])
        self.assertIn("citation_callouts", audit.json())
        self.assertNotEqual(audit.json(), release.json())

    def test_front_matter_is_user_authored_and_bound_to_final_version(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            initial = client.get(
                f"/api/v1/projects/{self.project_id}/final"
            ).json()
            self.assertEqual(["First"], initial["front_matter"]["authors"])
            self.assertEqual("generated", initial["front_matter"]["field_states"]["authors"])
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/final/front-matter",
                json={
                    "revision": initial["revision"],
                    "title": "Evidence-bound copper catalysis",
                    "authors": ["A. Researcher", "B. Researcher"],
                    "affiliations": ["Institute of Verified Synthesis"],
                    "abstract": "This review synthesizes the confirmed corpus.",
                    "keywords": ["copper", "evidence synthesis"],
                },
                headers=self.headers("save-front-matter"),
            )
            self.assertEqual(200, saved.status_code, saved.text)
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("front-matter-build"),
            )
            self.assertEqual(200, built.status_code, built.text)
            current = client.get(
                f"/api/v1/projects/{self.project_id}/final"
            ).json()
            edited = client.put(
                f"/api/v1/projects/{self.project_id}/final/front-matter",
                json={
                    "revision": current["revision"],
                    "title": "Updated evidence-bound copper catalysis",
                    "authors": ["A. Researcher", "B. Researcher"],
                    "affiliations": ["Institute of Verified Synthesis"],
                    "abstract": "This review synthesizes the confirmed corpus.",
                    "keywords": ["copper", "evidence synthesis"],
                },
                headers=self.headers("edit-front-matter"),
            )
            self.assertEqual(200, edited.status_code, edited.text)
            stale = client.get(
                f"/api/v1/projects/{self.project_id}/final"
            ).json()
        self.assertTrue(current["front_matter_current"])
        self.assertTrue(current["final_current"])
        self.assertIn("# Evidence-bound copper catalysis", current["final_draft_md"])
        self.assertIn("**Authors:** A. Researcher, B. Researcher", current["final_draft_md"])
        self.assertIn("## Abstract", current["final_draft_md"])
        self.assertIn("**Keywords:** copper, evidence synthesis", current["final_draft_md"])
        self.assertFalse(stale["final_current"])
        self.assertTrue(stale["freshness"]["stale"])

    def test_final_audit_warns_for_citation_issues_but_blocks_cross_project_artifacts(self) -> None:
        with self.sessions.begin() as session:
            other = Project(
                user_id=uuid.UUID(self.first.user_id),
                slug="same-user-other-project",
                topic="Other project",
            )
            session.add(other)
            session.flush()
            other_project_id = str(other.id)
        repository = self.app.state.workflow_repository
        artifacts = self.app.state.artifact_service
        run = repository.create_stage_run(
            self.first.user_id, other_project_id, "final", status="succeeded"
        )
        staging = artifacts.stage_run_directory(
            self.first.user_id, other_project_id, run.id
        )
        (staging / "foreign.png").write_bytes(b"foreign-project-image")
        foreign = artifacts.publish(
            self.first.user_id,
            other_project_id,
            run.id,
            "foreign.png",
            logical_name="final/foreign.png",
            artifact_type="png",
            producer_stage="final",
            make_current=False,
            metadata={"operation": "test"},
        )
        service = self.app.state.final_service
        missing_sources = service._validate_markdown(
            self.first,
            self.project_id,
            "# Draft without references\n",
            source_paper_ids=["P001"],
        )
        cross_project = service._validate_markdown(
            self.first,
            self.project_id,
            (
                "# Draft [1]\n\n"
                f"![Foreign](/api/v1/artifacts/{foreign.id}/content)\n\n"
                "## References\n[1] P001\n"
            ),
            source_paper_ids=["P001"],
        )
        self.assertTrue(missing_sources["valid"])
        self.assertIn("missing_references_section", missing_sources["warning_issues"])
        self.assertEqual([], missing_sources["blocking_issues"])
        self.assertFalse(cross_project["valid"])
        self.assertEqual([foreign.id], cross_project["cross_project_artifact_ids"])

    def test_final_audit_warns_for_fabricated_deleted_and_artifactless_sources(self) -> None:
        service = self.app.state.final_service
        fabricated = service._validate_markdown(
            self.first,
            self.project_id,
            "# Draft [1] [2]\n\n## References\n[1] P001\n[2] FABRICATED\n",
            source_paper_ids=["P001"],
        )
        bibliographic = service._validate_markdown(
            self.first,
            self.project_id,
            "# Draft [4]\n\n## References\n[4] Author. Article title. Journal. 2024\n",
            source_paper_ids=["P001"],
            source_reference_numbers={"P001": 4},
        )
        with self.sessions.begin() as session:
            paper = session.query(LibraryPaper).filter_by(
                user_id=uuid.UUID(self.first.user_id), paper_id="P001"
            ).one()
            paper.status = "deleted"
        deleted = service._validate_markdown(
            self.first,
            self.project_id,
            "# Draft [1]\n\n## References\n[1] P001\n",
            source_paper_ids=["P001"],
        )
        with self.sessions.begin() as session:
            paper = session.query(LibraryPaper).filter_by(
                user_id=uuid.UUID(self.first.user_id), paper_id="P001"
            ).one()
            paper.status = "active"
            session.query(LibraryArtifact).filter_by(
                user_id=uuid.UUID(self.first.user_id), paper_id="P001"
            ).update({"availability": "trashed"})
        artifactless = service._validate_markdown(
            self.first,
            self.project_id,
            "# Draft [1]\n\n## References\n[1] P001\n",
            source_paper_ids=["P001"],
        )
        self.assertTrue(fabricated["valid"])
        self.assertIn("references_include_unmapped_sources", fabricated["warning_issues"])
        self.assertEqual([2], fabricated["unmapped_reference_numbers"])
        self.assertNotIn(
            "citation_sources_missing_from_references",
            bibliographic["warning_issues"],
        )
        self.assertEqual(["P001"], bibliographic["listed_source_paper_ids"])
        self.assertTrue(deleted["valid"])
        self.assertIn("library_sources_unavailable", deleted["warning_issues"])
        self.assertTrue(artifactless["valid"])
        self.assertIn(
            "library_source_artifacts_missing", artifactless["warning_issues"]
        )

    def test_docx_export_is_registered_and_downloadable(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            overview = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={},
                headers=self.headers("export-overview"),
            )
            self.assertEqual("succeeded", self.wait_job(client, overview.json()["id"])["status"])
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("export-build"),
            )
            self.assertEqual(200, built.status_code, built.text)
            started = client.post(
                f"/api/v1/projects/{self.project_id}/final/export-jobs",
                json={},
                headers=self.headers("export-docx"),
            )
            job = self.wait_job(client, started.json()["id"])
            artifact_id = job["result"]["docx_artifact_id"]
            downloaded = client.get(f"/api/v1/artifacts/{artifact_id}/content")
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(200, downloaded.status_code)
        self.assertTrue(downloaded.content.startswith(b"PK"))

    def test_pdf_export_supports_both_profiles_without_invalidating_docx(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("pdf-build"),
            )
            self.assertEqual(200, built.status_code, built.text)
            word = client.post(
                f"/api/v1/projects/{self.project_id}/final/export-jobs",
                json={},
                headers=self.headers("pdf-word-first"),
            )
            self.assertEqual(
                "succeeded", self.wait_job(client, word.json()["id"])["status"]
            )
            english = client.post(
                f"/api/v1/projects/{self.project_id}/final/pdf-jobs",
                json={"language_profile": "en"},
                headers=self.headers("pdf-en"),
            )
            english_job = self.wait_job(client, english.json()["id"])
            chinese = client.post(
                f"/api/v1/projects/{self.project_id}/final/pdf-jobs",
                json={"language_profile": "zh-CN"},
                headers=self.headers("pdf-zh"),
            )
            chinese_job = self.wait_job(client, chinese.json()["id"])
            current = client.get(
                f"/api/v1/projects/{self.project_id}/final"
            ).json()
            downloaded = client.get(current["pdf_url"])
        self.assertEqual("succeeded", english_job["status"], english_job)
        self.assertEqual("succeeded", chinese_job["status"], chinese_job)
        self.assertEqual("zh-CN", current["pdf_language_profile"])
        self.assertTrue(current["final_pdf_exists"])
        self.assertFalse(current["final_pdf_stale"])
        self.assertTrue(current["final_draft_docx_exists"])
        self.assertEqual("pass", current["pdf_qa"]["status"])
        self.assertEqual(200, downloaded.status_code)
        self.assertTrue(downloaded.content.startswith(b"%PDF"))

    def test_final_freshness_tracks_optional_component_versions(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            conclusion = client.post(
                f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                json={},
                headers=self.headers("fresh-conclusion"),
            )
            self.assertEqual("succeeded", self.wait_job(client, conclusion.json()["id"])["status"])
            overview = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={},
                headers=self.headers("fresh-overview"),
            )
            self.assertEqual("succeeded", self.wait_job(client, overview.json()["id"])["status"])
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("fresh-build"),
            )
            self.assertEqual(200, built.status_code, built.text)
            current = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            edited = client.put(
                f"/api/v1/projects/{self.project_id}/final/overview-text",
                json={
                    "revision": current["revision"],
                    "title": "Changed after build",
                    "subtitle": "",
                    "labels": [],
                },
                headers=self.headers("fresh-overview-edit"),
            )
            self.assertEqual(200, edited.status_code, edited.text)
            stale = client.get(f"/api/v1/projects/{self.project_id}/final").json()
        self.assertTrue(current["conclusion_current"])
        self.assertTrue(current["overview_figure_current"])
        self.assertTrue(current["final_current"])
        self.assertFalse(stale["final_current"])
        self.assertTrue(stale["freshness"]["stale"])

    def test_equal_generated_bytes_create_new_versions_for_new_draft_lineage(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            first_conclusion_job = client.post(
                f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                json={}, headers=self.headers("lineage-conclusion-v1"),
            )
            self.assertEqual(
                "succeeded",
                self.wait_job(client, first_conclusion_job.json()["id"])["status"],
            )
            first_overview_job = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={}, headers=self.headers("lineage-overview-v1"),
            )
            self.assertEqual(
                "succeeded",
                self.wait_job(client, first_overview_job.json()["id"])["status"],
            )
            first = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            self.revise_and_approve_draft(client, "lineage-draft-v2")
            second_conclusion_job = client.post(
                f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                json={}, headers=self.headers("lineage-conclusion-v2"),
            )
            self.assertEqual(
                "succeeded",
                self.wait_job(client, second_conclusion_job.json()["id"])["status"],
            )
            second_overview_job = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={}, headers=self.headers("lineage-overview-v2"),
            )
            self.assertEqual(
                "succeeded",
                self.wait_job(client, second_overview_job.json()["id"])["status"],
            )
            second = client.get(f"/api/v1/projects/{self.project_id}/final").json()
        self.assertNotEqual(
            first["conclusion_artifact_id"], second["conclusion_artifact_id"]
        )
        self.assertNotEqual(first["overview_artifact_id"], second["overview_artifact_id"])
        self.assertTrue(second["conclusion_current"])
        self.assertTrue(second["overview_figure_current"])

    def test_final_build_does_not_reuse_optional_components_from_an_older_draft(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            conclusion = client.post(
                f"/api/v1/projects/{self.project_id}/final/conclusion-jobs",
                json={}, headers=self.headers("stale-build-conclusion"),
            )
            self.assertEqual("succeeded", self.wait_job(client, conclusion.json()["id"])["status"])
            overview = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={}, headers=self.headers("stale-build-overview"),
            )
            self.assertEqual("succeeded", self.wait_job(client, overview.json()["id"])["status"])
            self.revise_and_approve_draft(client, "stale-build")
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("stale-optional-build"),
            )
            final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
        self.assertEqual(200, built.status_code, built.text)
        self.assertNotIn("A bounded conclusion.", final["final_draft_md"])
        self.assertNotIn("## Review Overview", final["final_draft_md"])

    def test_stale_final_cannot_be_exported_or_have_overview_text_edited(self) -> None:
        with TestClient(self.app) as client:
            self.prepare_approved_draft(client)
            overview = client.post(
                f"/api/v1/projects/{self.project_id}/final/overview-jobs",
                json={}, headers=self.headers("stale-export-overview"),
            )
            self.assertEqual("succeeded", self.wait_job(client, overview.json()["id"])["status"])
            built = client.post(
                f"/api/v1/projects/{self.project_id}/final/build",
                headers=self.headers("stale-export-build"),
            )
            self.assertEqual(200, built.status_code, built.text)
            self.revise_and_approve_draft(client, "stale-export")
            final = client.get(f"/api/v1/projects/{self.project_id}/final").json()
            exported = client.post(
                f"/api/v1/projects/{self.project_id}/final/export-jobs",
                json={}, headers=self.headers("stale-final-export"),
            )
            edited = client.put(
                f"/api/v1/projects/{self.project_id}/final/overview-text",
                json={
                    "revision": final["revision"],
                    "title": "Invalid stale edit",
                    "subtitle": "",
                    "labels": [],
                },
                headers=self.headers("stale-overview-text-edit"),
            )
        self.assertFalse(final["final_current"])
        self.assertEqual(409, exported.status_code, exported.text)
        self.assertEqual(409, edited.status_code, edited.text)


if __name__ == "__main__":
    import unittest

    unittest.main()
