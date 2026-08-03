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
    def test_final_page_has_only_current_actions_in_order(self) -> None:
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")
        labels = ["Generate Conclusion", "Generate Overview Figure", "Generate Final Draft"]
        positions = [final_page.index(label) for label in labels]

        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="final-conclusion"', final_page)
        self.assertIn('id="final-overview-figure"', final_page)
        self.assertNotIn("Outline Images", final_page)
        self.assertNotIn("final-outline-chart", final_page)

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
