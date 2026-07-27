"""Behavior checks for conclusion preview and release-chart orchestration."""

import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "view" / "serve_review_dashboard.py"
SPEC = importlib.util.spec_from_file_location("serve_review_dashboard_final_stage", DASHBOARD_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dashboard)
INTEGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-final-audit-release"
    / "scripts"
    / "integrate_generated_conclusion.py"
)
INTEGRATION_SPEC = importlib.util.spec_from_file_location("test_conclusion_integration", INTEGRATION_PATH)
integration = importlib.util.module_from_spec(INTEGRATION_SPEC)
assert INTEGRATION_SPEC.loader is not None
INTEGRATION_SPEC.loader.exec_module(integration)
FINAL_PAGE_PATH = Path(__file__).resolve().parent / "assets" / "dashboard" / "final.html"
REVIEW_UI_PATH = Path(__file__).resolve().parent / "assets" / "dashboard" / "review-ui.js"


class FinalStageActionTests(unittest.TestCase):
    def test_final_page_has_ordered_actions_and_dedicated_chart_targets(self) -> None:
        """Catches final controls being reordered or rendered by the generic stage UI."""
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")
        action_labels = [
            "Generate Conclusion",
            "Generate Outline Images",
            "Generate Final Draft",
        ]
        positions = [final_page.index(label) for label in action_labels]

        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="final-conclusion"', final_page)
        self.assertIn('id="final-outline-chart"', final_page)
        self.assertIn("/run/final", final_page)

    def test_generic_stage_action_skips_the_final_page(self) -> None:
        """Catches a duplicate generic action control on the Final page."""
        review_ui = REVIEW_UI_PATH.read_text(encoding="utf-8")

        self.assertIn('if (current.id === "final") return;', review_ui)

    def test_final_page_uses_chart_payload_keys_and_recognizes_windows_paths(self) -> None:
        """Catches chart previews silently missing when the server returns its documented payload."""
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")

        self.assertIn("outline_chart_preview_full_png_exists", final_page)
        self.assertIn("outline_chart_preview_full_png_path", final_page)
        self.assertIn("release_chart_full_png_exists", final_page)
        self.assertIn("release_chart_full_png_path", final_page)
        self.assertNotIn("outline_chart_preview_png_exists", final_page)
        self.assertNotIn("final_summary_chart_png_exists", final_page)
        self.assertIn(r"/^[A-Za-z]:[\\/]/", final_page)

    def test_final_page_places_the_release_chart_before_final_draft_text(self) -> None:
        """Catches the full-review chart being appended after the manuscript."""
        final_page = FINAL_PAGE_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "content=chartFigure(payload.release_chart_full_png_path,'Final summary chart','Final-release chart.')+content",
            final_page,
        )

    def make_project(self, root: Path, project_id: str = "demo") -> Path:
        project = root / "review-projects" / project_id
        (project / "01_matrix_outline").mkdir(parents=True)
        (project / "04_first_draft").mkdir()
        (project / "05_final_audit").mkdir()
        (project / "01_matrix_outline" / "selected_outline.md").write_text(
            "# Selected outline\n", encoding="utf-8"
        )
        (project / "04_first_draft" / "first_draft.md").write_text(
            "# Review\n\n## Body\n\nDraft text.\n\n## References\n\n[1] Source.\n",
            encoding="utf-8",
        )
        return project

    def test_preview_rejects_missing_or_unvalidated_current_conclusion(self) -> None:
        """Catches previewing an outline without an approved current conclusion."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_project(root)

            with self.assertRaisesRegex(RuntimeError, "current conclusion"):
                dashboard.generate_outline_chart_preview(root, "demo")

    def test_preview_integrates_conclusion_and_selects_preview_markdown(self) -> None:
        """Catches charting first/final fallback instead of the composed preview."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.make_project(root)
            draft_stage = project / "04_first_draft"
            time.sleep(0.01)
            (draft_stage / "conclusion_generated.md").write_text(
                "## Conclusion\n\nValidated conclusion.\n", encoding="utf-8"
            )
            (draft_stage / "conclusion_quality_report.json").write_text(
                json.dumps({"validation": {"passes_validation": True}}), encoding="utf-8"
            )
            calls = []

            def fake_run(script: Path, review_root: Path, project_id: str, **kwargs: object) -> str:
                calls.append((script.name, kwargs.get("extra")))
                (draft_stage / "review_summary_chart.png").write_bytes(b"png")
                return "generated"

            with patch.object(dashboard, "conclusion_integration_module", return_value=integration), patch.object(
                dashboard, "run_project_script", side_effect=fake_run
            ):
                result = dashboard.generate_outline_chart_preview(root, "demo")

            preview = draft_stage / "outline_chart_preview.md"
            self.assertEqual(preview.read_text(encoding="utf-8"), (
                "# Review\n\n## Body\n\nDraft text.\n\n## Conclusion\n\nValidated conclusion.\n\n## References\n\n[1] Source.\n"
            ))
            self.assertEqual(calls, [("generate_review_summary_chart.py", ["--scope", "both", "--input-markdown", str(preview)])])
            self.assertEqual(result["preview_markdown"], str(preview))
            self.assertEqual(result["preview_full_png"], str(draft_stage / "review_summary_chart.png"))

    def test_release_bundle_audits_before_generating_both_scope_chart(self) -> None:
        """Catches release chart generation before final-audit integration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.make_project(root)
            final_stage = project / "05_final_audit"
            calls = []

            def fake_audit(review_root: Path, project_id: str) -> dict[str, str]:
                calls.append("audit")
                (final_stage / "final_draft.md").write_text("# Final\n", encoding="utf-8")
                return {"final_draft": str(final_stage / "final_draft.md")}

            def fake_run(script: Path, review_root: Path, project_id: str, **kwargs: object) -> str:
                calls.append((script.name, kwargs.get("extra")))
                (final_stage / "review_summary_chart.png").write_bytes(b"png")
                return "generated"

            with patch.object(dashboard, "regenerate_final_audit", side_effect=fake_audit), patch.object(
                dashboard, "run_project_script", side_effect=fake_run
            ):
                result = dashboard.regenerate_final_draft_bundle(root, "demo")

            self.assertEqual(calls, ["audit", ("generate_review_summary_chart.py", ["--scope", "both"])])
            self.assertEqual(result["final_draft"], str(final_stage / "final_draft.md"))
            self.assertEqual(result["final_full_png"], str(final_stage / "review_summary_chart.png"))


if __name__ == "__main__":
    unittest.main()
