"""Behavior checks for Final-stage actions without summary-chart output."""

import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "view" / "serve_review_dashboard.py"
SPEC = importlib.util.spec_from_file_location("serve_review_dashboard_final_stage", DASHBOARD_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dashboard)
FINAL_PAGE_PATH = Path(__file__).resolve().parent / "assets" / "dashboard" / "final.html"
REVIEW_UI_PATH = Path(__file__).resolve().parent / "assets" / "dashboard" / "review-ui.js"


class FinalStageActionTests(unittest.TestCase):
    def test_overview_generation_does_not_rebase_a_stale_final_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            discovery = project / "00_discovery"
            outline = project / "01_matrix_outline"
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            for directory in (discovery, outline, draft_stage, final_stage):
                directory.mkdir(parents=True)
            (discovery / "query_plan.draft.json").write_text("{}", encoding="utf-8")
            (discovery / "selected_discovery_results.json").write_text("{}", encoding="utf-8")
            (outline / "selected_outline.md").write_text("# Outline\n", encoding="utf-8")
            first_draft = draft_stage / "first_draft.md"
            first_draft.write_text("# Review\n\nOld Stage 8 text.\n", encoding="utf-8")
            final_draft = final_stage / "final_draft.md"
            final_draft.write_text("# Review\n\nOld Stage 8 text.\n", encoding="utf-8")
            final_handoff = final_stage / "final_handoff.json"
            dashboard.write_stage_handoff(final_handoff, "draft", [first_draft])
            dashboard.record_stage_outputs(final_handoff, [final_draft], "final")
            first_draft.write_text("# Review\n\nCurrent custom Stage 8 text.\n", encoding="utf-8")

            def fake_run(
                script: Path,
                review_root: Path,
                project_id: str,
                **kwargs: object,
            ) -> str:
                if script.name == "generate_overview_figure.py":
                    output_index = list(kwargs["extra"]).index("--output") + 1
                    Path(list(kwargs["extra"])[output_index]).write_bytes(b"overview")
                return "ok"

            with patch.object(dashboard, "run_project_script", side_effect=fake_run):
                result = dashboard.generate_final_overview_figure(root, "demo")

            self.assertFalse(result["included_in_final_draft"])
            self.assertTrue(result["final_draft_requires_regeneration"])
            self.assertEqual(final_draft.read_text(encoding="utf-8"), "# Review\n\nOld Stage 8 text.\n")
            self.assertTrue(dashboard.artifact_freshness(final_handoff, [final_draft])["stale"])

    def test_overview_uses_the_settings_backed_image_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            for directory in (
                project / "00_discovery",
                project / "01_matrix_outline",
                project / "04_first_draft",
                project / "05_final_audit",
            ):
                directory.mkdir(parents=True)
            (project / "00_discovery" / "query_plan.draft.json").write_text("{}", encoding="utf-8")
            (project / "00_discovery" / "selected_discovery_results.json").write_text("{}", encoding="utf-8")
            (project / "01_matrix_outline" / "selected_outline.md").write_text("# Outline\n", encoding="utf-8")
            (project / "04_first_draft" / "first_draft.md").write_text("# Draft\n", encoding="utf-8")
            captured = {}

            def fake_run(script: Path, review_root: Path, project_id: str, **kwargs: object) -> str:
                captured["extra"] = list(kwargs["extra"])
                output_index = captured["extra"].index("--output") + 1
                Path(captured["extra"][output_index]).write_bytes(b"overview")
                return "ok"

            settings_environment = {
                "IMAGE_OPENAI_BASE_URL": "https://image.example/v1",
                "IMAGE_OPENAI_MODEL": "gpt-image-2",
                "IMAGE_OPENAI_WIRE_API": "chat-completions",
                "IMAGE_OPENAI_API_KEY": "secret-not-for-command-line",
            }
            with (
                patch.object(dashboard, "provider_subprocess_environment", return_value=settings_environment),
                patch.object(dashboard, "run_project_script", side_effect=fake_run),
            ):
                dashboard.generate_final_overview_figure(root, "demo")

            extra = captured["extra"]
            self.assertEqual(extra[extra.index("--base-url") + 1], "https://image.example/v1")
            self.assertEqual(extra[extra.index("--model") + 1], "gpt-image-2")
            self.assertEqual(extra[extra.index("--wire-api") + 1], "chat-completions")
            self.assertNotIn("secret-not-for-command-line", extra)

    def test_final_integrity_gate_detects_lost_stage8_custom_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage = root / "review-projects" / "demo" / "04_first_draft"
            final_stage = root / "review-projects" / "demo" / "05_final_audit"
            stage.mkdir(parents=True)
            final_stage.mkdir(parents=True)
            (stage / "first_draft.md").write_text(
                "# Demo\n\n## Section\n\n1Custom text [1].\n\n"
                "<!-- paragraph_id: sec1-p1 -->\n\n## References\n\n[1] Reference.\n",
                encoding="utf-8",
            )
            final_path = final_stage / "final_draft.md"
            final_path.write_text(
                "# Demo\n\n## Section\n\nCustom text [1].\n\n## References\n\n[1] Reference.\n",
                encoding="utf-8",
            )

            self.assertEqual(
                dashboard.missing_final_draft_paragraphs(root, "demo", final_path),
                ["sec1-p1"],
            )
            final_path.write_text(
                "# Demo\n\n## Section\n\n1Custom text [1].\n\n## References\n\n[1] Reference.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                dashboard.missing_final_draft_paragraphs(root, "demo", final_path),
                [],
            )

    def test_final_audit_report_falls_back_to_generated_format_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stage = Path(temp_dir)
            (stage / "format_scan.md").write_text("# Final Audit\n\nPassed.\n", encoding="utf-8")

            self.assertEqual(
                dashboard.final_audit_report_text(stage),
                "# Final Audit\n\nPassed.\n",
            )

    def test_final_audit_recovers_empty_legacy_citations_from_current_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            matrix = project / "01_matrix_outline"
            sections = project / "02_section_drafting"
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            matrix.mkdir(parents=True)
            sections.mkdir(parents=True)
            draft_stage.mkdir(parents=True)
            final_stage.mkdir(parents=True)
            (sections / "section_drafts.json").write_text(
                json.dumps(
                    {
                        "sections": [
                            {
                                "paragraphs": [
                                    {"paper_id": "P001", "cited_paper_ids": ["P001"]},
                                    {"paper_id": "P002", "cited_paper_ids": ["P002"]},
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (matrix / "literature_matrix.json").write_text(
                json.dumps(
                    {
                        "rows": [
                            {"paper_id": "P001", "authors": ["A. Author"], "title": "First paper", "journal": "J1", "year": 2024},
                            {"paper_id": "P002", "authors": ["B. Author"], "title": "Second paper", "journal": "J2", "year": 2025},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (draft_stage / "first_draft.md").write_text(
                "# Review\n\nBody [1]. More [2].\n\n## References\n",
                encoding="utf-8",
            )
            (draft_stage / "citations.json").write_text("[]\n", encoding="utf-8")
            (final_stage / "overview_figure.png").write_bytes(b"current-overview")
            overview_handoff = final_stage / "overview_figure_handoff.json"
            dashboard.write_stage_handoff(
                overview_handoff,
                "blueprint",
                [draft_stage / "first_draft.md"],
            )
            dashboard.record_stage_outputs(
                overview_handoff,
                [final_stage / "overview_figure.png"],
                "final-overview-figure",
            )

            with patch.object(dashboard, "run_project_script", return_value="ok"):
                dashboard.regenerate_final_audit(root, "demo")

            citations = json.loads((draft_stage / "citations.json").read_text(encoding="utf-8"))
            final_text = (final_stage / "final_draft.md").read_text(encoding="utf-8")
            self.assertEqual([entry["paper_id"] for entry in citations["entries"]], ["P001", "P002"])
            self.assertIn("[1] A. Author. First paper. J1 (2024).", final_text)
            self.assertIn("[2] B. Author. Second paper. J2 (2025).", final_text)
            self.assertIn("OVERVIEW-F01", final_text)
            self.assertTrue(dashboard.overview_figure_is_current(project))

    def test_final_page_has_only_current_actions_in_order(self) -> None:
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")
        labels = ["Generate Conclusion", "Generate Overview Figure", "Generate Final Draft"]
        positions = [final_page.index(label) for label in labels]

        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="final-conclusion"', final_page)
        self.assertIn('id="final-overview-figure"', final_page)
        self.assertNotIn("Outline Images", final_page)
        self.assertNotIn("final-outline-chart", final_page)

    def test_final_actions_switch_the_middle_window_to_the_generated_output(self) -> None:
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")
        self.assertIn("selectedProject='',doc='final'", final_page)
        self.assertIn("'final-conclusion':'conclusion'", final_page)
        self.assertIn("'final-overview-figure':'overview-figure'", final_page)
        self.assertIn("final:'final'", final_page)
        selection = final_page.index("doc=targetDocs[stageId]||doc")
        self.assertLess(selection, final_page.index("await loadProject(selectedProject)", selection))

    def test_unused_stale_overview_does_not_hide_a_current_final_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            outline_stage = project / "01_matrix_outline"
            for directory in (draft_stage, final_stage, outline_stage):
                directory.mkdir(parents=True)

            first_draft = draft_stage / "first_draft.md"
            first_draft.write_text("# Current draft\n", encoding="utf-8")
            final_draft = final_stage / "final_draft.md"
            final_draft.write_text("# Current final\n", encoding="utf-8")
            final_handoff = final_stage / "final_handoff.json"
            dashboard.write_stage_handoff(
                final_handoff,
                "draft",
                [first_draft],
                metadata={"includes_current_overview": False},
            )
            dashboard.record_stage_outputs(final_handoff, [final_draft], "final")

            outline = outline_stage / "selected_outline.md"
            outline.write_text("# Old outline\n", encoding="utf-8")
            overview = final_stage / "overview_figure.png"
            overview.write_bytes(b"old-overview")
            overview_handoff = final_stage / "overview_figure_handoff.json"
            dashboard.write_stage_handoff(overview_handoff, "blueprint", [outline])
            dashboard.record_stage_outputs(
                overview_handoff,
                [overview],
                "final-overview-figure",
            )
            outline.write_text("# Changed outline\n", encoding="utf-8")

            with (
                patch.object(dashboard, "section_source_freshness", return_value={"stale": False}),
                patch.object(dashboard, "project_draft_payload", return_value={"freshness": {"stale": False}}),
            ):
                payload = dashboard.project_final_payload(root, "demo")

            self.assertTrue(payload["freshness"]["overview_stale"])
            self.assertFalse(payload["freshness"]["overview_dependency_stale"])
            self.assertFalse(payload["freshness"]["final_stale"])
            self.assertFalse(payload["freshness"]["stale"])
            self.assertEqual(payload["final_draft_md"], "# Current final\n")

    def test_final_page_hides_metadata_but_keeps_only_the_overall_chart(self) -> None:
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")

        self.assertIn("replace(/<!--[\\s\\S]*?-->/g,'')", final_page)
        self.assertIn("release_chart_full_png", final_page)
        self.assertIn("content=insertChartAfterIntroduction(content,chart)", final_page)
        self.assertNotIn("outline_chart_preview", final_page)

    def test_generic_stage_action_skips_the_final_page(self) -> None:
        review_ui = REVIEW_UI_PATH.read_text(encoding="utf-8")
        self.assertIn('if (current.id === "final") return;', review_ui)

    def test_final_bundle_runs_audit_then_one_overall_chart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_stage = root / "review-projects" / "demo" / "05_final_audit"
            final_stage.mkdir(parents=True)
            calls: list[str] = []

            def fake_audit(review_root: Path, project_id: str) -> dict[str, str]:
                calls.append("audit")
                output = final_stage / "final_draft.md"
                output.write_text("# Final\n", encoding="utf-8")
                return {"final_draft": str(output)}

            def fake_chart(review_root: Path, project_id: str, draft_path: Path) -> str:
                calls.append("overall-chart")
                (final_stage / "review_summary_chart.png").write_bytes(b"png")
                return "generated"

            with patch.object(dashboard, "regenerate_final_audit", side_effect=fake_audit), patch.object(
                dashboard, "refresh_final_overview_chart", side_effect=fake_chart
            ):
                result = dashboard.regenerate_final_draft_bundle(root, "demo")

            self.assertEqual(calls, ["audit", "overall-chart"])
            self.assertEqual(result["final_draft"], str(final_stage / "final_draft.md"))
            self.assertEqual(result["final_full_png"], str(final_stage / "review_summary_chart.png"))

    def test_final_audit_allows_first_draft_and_available_overview_without_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            (draft_stage / "figures").mkdir(parents=True)
            final_stage.mkdir(parents=True)
            (draft_stage / "first_draft.md").write_text(
                "# Review\n\n## Introduction\n\nCurrent draft body.\n", encoding="utf-8"
            )
            (draft_stage / "figures" / "figure_01.png").write_bytes(b"draft-image")
            (final_stage / "overview_figure.png").write_bytes(b"overview-image")
            overview_handoff = final_stage / "overview_figure_handoff.json"
            dashboard.write_stage_handoff(overview_handoff, "blueprint", [])
            dashboard.record_stage_outputs(
                overview_handoff,
                [final_stage / "overview_figure.png"],
                "final-overview-figure",
            )
            calls: list[str] = []

            def fake_run(script: Path, review_root: Path, project_id: str, **kwargs: object) -> str:
                calls.append(script.name)
                return "ok"

            with patch.object(dashboard, "run_project_script", side_effect=fake_run):
                result = dashboard.regenerate_final_audit(root, "demo")

            final_text = (final_stage / "final_draft.md").read_text(encoding="utf-8")
            self.assertEqual(result["conclusion_mode"], "first_draft_without_optional_conclusion")
            self.assertIn("OVERVIEW-F01", final_text)
            self.assertIn("figures/overview_figure.png", final_text)
            self.assertTrue((final_stage / "figures" / "overview_figure.png").is_file())
            self.assertNotIn("integrate_generated_conclusion.py", calls)

    def test_final_audit_does_not_reuse_an_unversioned_overview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            draft_stage.mkdir(parents=True)
            final_stage.mkdir(parents=True)
            (draft_stage / "first_draft.md").write_text(
                "# Review\n\n## Introduction\n\nCurrent draft body.\n",
                encoding="utf-8",
            )
            (final_stage / "overview_figure.png").write_bytes(b"legacy-overview")

            with patch.object(dashboard, "run_project_script", return_value="ok"):
                result = dashboard.regenerate_final_audit(root, "demo")

            final_text = (final_stage / "final_draft.md").read_text(encoding="utf-8")
            self.assertFalse(result["overview"]["included"])
            self.assertNotIn("OVERVIEW-F01", final_text)

    def test_dashboard_instance_lock_rejects_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = dashboard.acquire_dashboard_instance_lock(root, "127.0.0.1", 8765)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(dashboard.acquire_dashboard_instance_lock(root, "127.0.0.1", 8765))
            finally:
                dashboard.release_dashboard_instance_lock(first)

    def test_current_conclusion_requires_a_matching_final_integration_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            draft_stage.mkdir(parents=True)
            final_stage.mkdir(parents=True)
            draft = draft_stage / "first_draft.md"
            conclusion = draft_stage / "conclusion_generated.md"
            final = final_stage / "final_draft.md"
            draft.write_text("# Review\n", encoding="utf-8")
            conclusion.write_text("## Conclusion\n\nCurrent conclusion.\n", encoding="utf-8")
            final.write_text("# Review\n", encoding="utf-8")
            (final_stage / "conclusion_integration.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "first_draft_without_optional_conclusion",
                        "first_draft_path": str(draft),
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(dashboard.conclusion_integration_is_current(project, True))

            receipt = {
                "schema_version": 1,
                "first_draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
                "generated_conclusion_sha256": hashlib.sha256(conclusion.read_bytes()).hexdigest(),
                "inserted_conclusion_heading": "## Conclusion",
            }
            (final_stage / "conclusion_integration.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            final.write_text("# Review\n\n## Conclusion\n\nCurrent conclusion.\n", encoding="utf-8")

            self.assertTrue(dashboard.conclusion_integration_is_current(project, True))

    def test_final_audit_removes_superscript_artifacts_only_from_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            draft_stage = project / "04_first_draft"
            final_stage = project / "05_final_audit"
            draft_stage.mkdir(parents=True)
            final_stage.mkdir(parents=True)
            (draft_stage / "first_draft.md").write_text(
                "# Review\n\nBody keeps H<sup>2</sup>.\n\n## References\n\n"
                "[1] Tian Liang, <sup></sup> Meiwen Liu. Example.\n"
                "[2] Qi Zhang<sup>+</sup>, <sup>[]</sup> Zhongtian Wu. Example.\n",
                encoding="utf-8",
            )

            with patch.object(dashboard, "run_project_script", return_value="ok"):
                dashboard.regenerate_final_audit(root, "demo")

            final_text = (final_stage / "final_draft.md").read_text(encoding="utf-8")
            self.assertIn("Body keeps H<sup>2</sup>.", final_text)
            self.assertIn("[1] Tian Liang, Meiwen Liu. Example.", final_text)
            self.assertIn("[2] Qi Zhang, Zhongtian Wu. Example.", final_text)
            self.assertNotIn("<sup", final_text.split("## References", 1)[1])

    def test_reference_author_cleanup_removes_affiliation_fragments(self) -> None:
        authors = ["Bingyu Shen", "<sup>[", "</sup> <sup>]</sup> Aimei Yang"]
        self.assertEqual(dashboard.clean_reference_author_text(authors), "Bingyu Shen, Aimei Yang")

    def test_reference_render_with_missing_authors_has_no_orphan_period(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            matrix = project / "01_matrix_outline"
            matrix.mkdir(parents=True)
            (matrix / "literature_matrix.json").write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "paper_id": "P004",
                                "authors": [],
                                "title": "Reference without imported authors",
                                "journal": "Example Journal",
                                "year": 2024,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            rendered = dashboard.render_reference_section(
                project,
                [{"callout": 4, "paper_id": "P004"}],
            )

            self.assertIn("[4] Reference without imported authors.", rendered)
            self.assertNotIn("[4].", rendered)

    def test_reference_render_repairs_xml_incompatible_extractor_separator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            matrix = project / "01_matrix_outline"
            matrix.mkdir(parents=True)
            (matrix / "literature_matrix.json").write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "paper_id": "P095",
                                "authors": ["Example Author"],
                                "title": "Ruthenium-Catalyzed C\x00 H Activation",
                                "journal": "Example Journal",
                                "year": 2023,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            rendered = dashboard.render_reference_section(
                project,
                [{"callout": 1, "paper_id": "P095"}],
            )

            self.assertNotIn("\x00", rendered)
            self.assertIn("C–H Activation", rendered)

            repaired_body = dashboard.replace_reference_section(
                "# Review\n\nRuthenium-Catalyzed C\uFFFD H Activation.\n",
                rendered,
            )
            self.assertNotIn("\uFFFD", repaired_body)
            self.assertIn("C–H Activation", repaired_body)

    def test_reference_cleanup_never_crosses_into_the_next_entry(self) -> None:
        markdown = (
            "# Review\n\n## References\n\n"
            "[1] Bowen Wang, Longwu Sun<sup>. First title. (2024).\n"
            "[2] Shengfu Kang, <sup>+</sup> Xiaohong Liu. Second title. (2024).\n"
        )

        cleaned = dashboard.sanitize_reference_section_markup(markdown)

        self.assertIn("[1] Bowen Wang, Longwu Sun. First title. (2024).", cleaned)
        self.assertIn("[2] Shengfu Kang, Xiaohong Liu. Second title. (2024).", cleaned)
        self.assertEqual(cleaned.count("[1]"), 1)
        self.assertEqual(cleaned.count("[2]"), 1)


if __name__ == "__main__":
    unittest.main()
