"""Focused checks for the Stage 6 selection to Stage 7 redraw handoff."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from serve_review_dashboard import (
    approve_figure_for_manuscript,
    artifact_freshness,
    project_figures_payload,
    record_stage_outputs,
    refresh_figure_review_handoff,
    redraw_current_figure,
    section_candidate_freshness,
    section_source_freshness,
    sha256_file,
    sync_selected_candidate_for_redraw,
    write_stage_handoff,
)


class FigureReviewHandoffChecks(unittest.TestCase):
    def build_project(self, root: Path) -> tuple[Path, Path, Path, Path]:
        project = root / "review-projects" / "demo"
        section_stage = project / "02_section_drafting"
        redraw_stage = project / "03_figure_redraw"
        section_stage.mkdir(parents=True)
        redraw_stage.mkdir(parents=True)
        first = section_stage / "first.png"
        second = section_stage / "second.png"
        Image.new("RGB", (120, 60), "white").save(first)
        Image.new("RGB", (120, 60), "white").save(second)
        (section_stage / "section_drafts.json").write_text(
            json.dumps(
                {
                    "sections": [
                        {
                            "section_id": "sec1",
                            "paragraphs": [{"paragraph_id": "sec1-p1", "paper_id": "P001"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (section_stage / "figure_candidates.json").write_text(
            json.dumps(
                [
                    {
                        "figure_id": "P001-F01",
                        "paper_id": "P001",
                        "source_image_path": str(first),
                        "target_paragraph_id": "sec1-p1",
                        "manuscript_selected": True,
                    }
                ]
            ),
            encoding="utf-8",
        )
        (redraw_stage / "redrawn_figure_manifest.json").write_text(
            json.dumps(
                {
                    "project_id": "demo",
                    "figures": [
                        {
                            "figure_id": "P001-F01",
                            "paper_id": "P001",
                            "source_image": str(first),
                            "redrawn_image": str(redraw_stage / "old.png"),
                            "status": "redrawn",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return project, section_stage, redraw_stage, second

    def test_changed_selection_is_promoted_and_old_redraw_is_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project, section_stage, redraw_stage, second = self.build_project(Path(temp_dir))

            sync_selected_candidate_for_redraw(
                project,
                "P001",
                {
                    "candidate_index": 1,
                    "paper_id": "P001",
                    "source_image_path": str(second),
                    "target_paragraph_id": "sec1-p1",
                },
            )

            candidate = json.loads(
                (section_stage / "figure_candidates.json").read_text(encoding="utf-8")
            )[0]
            output = json.loads(
                (redraw_stage / "redrawn_figure_manifest.json").read_text(encoding="utf-8")
            )["figures"][0]
            self.assertEqual(candidate["source_image_path"], str(second))
            self.assertEqual(output["status"], "source_changed")
            self.assertNotIn("redrawn_image", output)
            self.assertTrue(output["superseded_output"]["redrawn_image"])

    def test_redraw_timeout_does_not_report_the_old_source_changed_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, redraw_stage, _ = self.build_project(root)
            manifest_path = redraw_stage / "redrawn_figure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["figures"][0].update(
                {
                    "status": "source_changed",
                    "notes": "Stage 6 selected a different source candidate; redraw this figure again.",
                }
            )
            manifest["figures"][0].pop("redrawn_image", None)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with patch(
                "serve_review_dashboard.run_project_script",
                side_effect=RuntimeError("Workflow script timed out: redraw_figures.py"),
            ) as runner:
                with self.assertRaisesRegex(RuntimeError, "Workflow script timed out"):
                    redraw_current_figure(root, "demo", "P001-F01", force_ai_edit=True)

            self.assertEqual(runner.call_args.kwargs["timeout"], 1200)
            failed = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
            self.assertEqual(failed["status"], "failed")
            self.assertIn("current Stage 6 source candidate", failed["notes"])
            self.assertNotIn("selected a different source candidate", failed["notes"])
            self.assertEqual(failed["last_redraw_attempt"]["source_sha256"], failed["source_image_sha256"])

    def test_human_approval_is_bound_to_current_source_and_output_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, _, redraw_stage, _ = self.build_project(root)
            manifest_path = redraw_stage / "redrawn_figure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            output_path = redraw_stage / "preview.png"
            Image.new("RGB", (240, 120), "white").save(output_path)
            manifest["figures"][0].update(
                {
                    "redrawn_image": str(output_path),
                    "chemistry_integrity": {"status": "failed", "failures": ["test gate"]},
                    "output_disposition": "saved_with_integrity_warning",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = approve_figure_for_manuscript(root, "demo", "P001-F01")

            approved = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
            self.assertEqual(result["figure_id"], "P001-F01")
            self.assertEqual(approved["human_approval"]["status"], "approved")
            self.assertEqual(approved["human_approval"]["source_sha256"], approved["source_image_sha256"])
            self.assertEqual(approved["human_approval"]["output_sha256"], approved["output_image_sha256"])
            self.assertEqual(approved["output_disposition"], "human_approved_for_manuscript")

    def test_human_approval_rejects_an_old_candidate_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, section_stage, redraw_stage, second = self.build_project(root)
            output_path = redraw_stage / "preview.png"
            output_path.write_bytes(b"preview")
            manifest_path = redraw_stage / "redrawn_figure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["figures"][0]["redrawn_image"] = str(output_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            candidates = json.loads((section_stage / "figure_candidates.json").read_text(encoding="utf-8"))
            candidates[0]["source_image_path"] = str(second)
            (section_stage / "figure_candidates.json").write_text(json.dumps(candidates), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "older source candidate"):
                approve_figure_for_manuscript(root, "demo", "P001-F01")

    def test_human_approval_rejects_a_stretched_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, redraw_stage, _ = self.build_project(root)
            output_path = redraw_stage / "square-preview.png"
            Image.new("RGB", (1024, 1024), "white").save(output_path)
            manifest_path = redraw_stage / "redrawn_figure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["figures"][0]["redrawn_image"] = str(output_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "aspect ratio does not match"):
                approve_figure_for_manuscript(root, "demo", "P001-F01")

    def test_legacy_section_handoff_requires_explicit_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, section_stage, _, _ = self.build_project(root)
            blueprint = project / "01_matrix_outline" / "section_blueprint.json"
            blueprint.parent.mkdir(parents=True)
            blueprint.write_text('{"sections":["sec1"]}', encoding="utf-8")
            handoff = section_stage / "section_handoff.json"
            handoff.write_text(
                json.dumps({"source_stage": "blueprint", "source_blueprint": str(blueprint)}),
                encoding="utf-8",
            )

            source_state = section_source_freshness(section_stage)
            candidate_state = section_candidate_freshness(section_stage)
            self.assertTrue(source_state["stale"])
            self.assertTrue(source_state["migration_required"])
            self.assertTrue(candidate_state["stale"])
            self.assertTrue(candidate_state["migration_required"])
            unchanged = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertNotIn("schema_version", unchanged)

    def test_prose_edit_does_not_expire_candidates_but_routing_edit_does(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, section_stage, _, _ = self.build_project(root)
            blueprint = project / "01_matrix_outline" / "section_blueprint.json"
            blueprint.parent.mkdir(parents=True)
            blueprint.write_text('{"sections":["sec1"]}', encoding="utf-8")
            handoff = section_stage / "section_handoff.json"
            write_stage_handoff(handoff, "blueprint", [blueprint])
            record_stage_outputs(
                handoff,
                [section_stage / "section_drafts.json"],
                "sections",
            )

            drafts_path = section_stage / "section_drafts.json"
            drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
            drafts["sections"][0]["paragraphs"][0]["text"] = "format-only cleanup"
            drafts_path.write_text(json.dumps(drafts), encoding="utf-8")
            self.assertTrue(section_source_freshness(section_stage)["stale"])
            self.assertFalse(section_candidate_freshness(section_stage)["stale"])

            drafts["sections"][0]["paragraphs"][0]["paper_id"] = "P002"
            drafts_path.write_text(json.dumps(drafts), encoding="utf-8")
            self.assertTrue(section_candidate_freshness(section_stage)["stale"])

    def test_source_image_change_expires_existing_figure_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, section_stage, _, _ = self.build_project(root)
            source = section_stage / "first.png"
            (section_stage / "paper_figure_candidates.json").write_text(
                json.dumps(
                    {
                        "papers": [
                            {
                                "paper_id": "P001",
                                "candidates": [
                                    {
                                        "candidate_index": 1,
                                        "source_image_path": str(source),
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            review = section_stage / "human_figure_review.json"
            review.write_text(
                json.dumps({"papers": {"P001": {"selected_candidate_index": 1}}}),
                encoding="utf-8",
            )
            refresh_figure_review_handoff(section_stage, accept_current=True)
            handoff = project / "03_figure_redraw" / "figure_review_handoff.json"
            self.assertFalse(artifact_freshness(handoff, [review])["stale"])

            source.write_bytes(b"changed source")
            state = artifact_freshness(handoff, [review])
            self.assertTrue(state["stale"])
            self.assertIn(str(source.resolve()), state["outdated_sources"])

    def test_missing_handoff_is_untracked_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output.json"
            output.write_text("{}", encoding="utf-8")
            state = artifact_freshness(Path(temp_dir) / "missing_handoff.json", [output])
            self.assertTrue(state["stale"])
            self.assertTrue(state["untracked"])

    def test_legacy_handoff_is_not_silently_rebased(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, section_stage, redraw_stage, _ = self.build_project(root)
            output = section_stage / "human_figure_review.json"
            output.write_text(json.dumps({"papers": {}}), encoding="utf-8")
            handoff = redraw_stage / "figure_review_handoff.json"
            handoff.write_text(
                json.dumps({"source_stage": "sections", "source_artifacts": []}),
                encoding="utf-8",
            )

            state = artifact_freshness(handoff, [output])

            self.assertTrue(state["stale"])
            self.assertTrue(state["migration_required"])
            self.assertNotIn("schema_version", json.loads(handoff.read_text(encoding="utf-8")))

    def test_manifest_without_a_current_output_is_semantically_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, _, redraw_stage, _ = self.build_project(root)
            (redraw_stage / "figures_handoff.json").write_text(
                json.dumps({"source_stage": "figure-review"}),
                encoding="utf-8",
            )
            freshness = project_figures_payload(root, project.name)["freshness"]
            self.assertTrue(freshness["semantic_redraw_stale"])
            self.assertEqual(freshness["selected_count"], 1)
            self.assertEqual(freshness["usable_count"], 0)

    def test_old_human_approval_does_not_override_current_aspect_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, section_stage, redraw_stage, _ = self.build_project(root)
            source = section_stage / "first.png"
            output = redraw_stage / "approved-square.png"
            Image.new("RGB", (100, 100), "white").save(output)
            manifest_path = redraw_stage / "redrawn_figure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["figures"][0].update(
                {
                    "redrawn_image": str(output),
                    "render_mode": "source-faithful-bw",
                    "source_image_sha256": sha256_file(source),
                    "chemistry_integrity": {"status": "failed", "failures": ["legacy"]},
                    "output_disposition": "human_approved_for_manuscript",
                    "human_approval": {
                        "status": "approved",
                        "source_image": str(source),
                        "source_sha256": sha256_file(source),
                        "output_image": str(output),
                        "output_sha256": sha256_file(output),
                    },
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            handoff = redraw_stage / "figures_handoff.json"
            write_stage_handoff(handoff, "figure-review", [])
            record_stage_outputs(handoff, [manifest_path, output], "figures")

            payload = project_figures_payload(root, project.name)
            row = payload["redrawn_manifest"]["figures"][0]

            self.assertEqual(payload["freshness"]["usable_count"], 0)
            self.assertFalse(row["human_approval"]["current_policy_match"])
            self.assertEqual(row["aspect_ratio_integrity"]["status"], "failed")

    def test_standard_ai_edit_can_use_provider_canvas_after_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, _, redraw_stage, _ = self.build_project(root)
            source = project / "02_section_drafting" / "first.png"
            output = redraw_stage / "provider-square.png"
            Image.new("RGB", (100, 100), "white").save(output)
            manifest_path = redraw_stage / "redrawn_figure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["figures"][0].update(
                {
                    "redrawn_image": str(output),
                    "render_mode": "ai-edit",
                    "edit_profile": "standard",
                    "chemistry_integrity": {"status": "failed", "failures": ["manual review"]},
                    "output_disposition": "saved_with_integrity_warning",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            approve_figure_for_manuscript(root, "demo", "P001-F01")
            handoff = redraw_stage / "figures_handoff.json"
            record_stage_outputs(handoff, [manifest_path, output], "figures")
            payload = project_figures_payload(root, project.name)
            row = payload["redrawn_manifest"]["figures"][0]

            self.assertEqual(row["aspect_ratio_integrity"]["status"], "failed")
            self.assertEqual(row["aspect_ratio_policy"], "provider_canvas_allowed")
            self.assertTrue(row["human_approval"]["current_policy_match"])
            self.assertEqual(payload["freshness"]["usable_count"], 1)

    def test_figures_ui_exposes_explicit_ai_comparison_without_weakening_default_route(self) -> None:
        html = (
            Path(__file__).resolve().parent
            / "assets"
            / "dashboard"
            / "figures.html"
        ).read_text(encoding="utf-8")

        self.assertIn("redrawCurrentAiOverride", html)
        self.assertIn("force_ai=1", html)
        self.assertIn("standardAiProviderCanvas", html)
        self.assertIn("humanApproveFigure", html)


if __name__ == "__main__":
    unittest.main()
