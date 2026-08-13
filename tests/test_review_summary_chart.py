"""Behavior tests for selecting a review-summary-chart manuscript."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-outline-summary-chart"
    / "scripts"
    / "generate_review_summary_chart.py"
)
SPEC = importlib.util.spec_from_file_location("review_summary_chart", SCRIPT_PATH)
chart = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chart
SPEC.loader.exec_module(chart)


class ResolveDraftTests(unittest.TestCase):
    def test_full_mermaid_can_stop_at_primary_review_sections(self) -> None:
        """Catches the full chart expanding every descendant into an unreadable banner."""
        child = chart.ReviewSection("1.1 Detailed mechanism", 3, 2)
        parent = chart.ReviewSection("1 Introduction", 2, 1, children=[child])

        mermaid = chart.generate_full_mermaid(
            [parent], "Review article", include_descendants=False
        )

        self.assertTrue(mermaid.startswith("graph LR"))
        self.assertIn("1 Introduction", mermaid)
        self.assertNotIn("Detailed mechanism", mermaid)

    def test_full_chart_export_uses_the_browser_free_renderer(self) -> None:
        """Keeps chart generation independent of Edge, a CDN, and machine state."""
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "full-review.png"
            section = chart.ReviewSection("1 Introduction", 2, 1)

            entry = chart.render_full_chart_png(
                'graph LR\n    R["Review"] --> N1["Introduction"]',
                image_path,
                sections=[section],
                review_title="Review",
            )

            self.assertEqual("pillow-static", entry["renderer"])
            self.assertTrue(image_path.is_file())

    def test_full_chart_png_is_written_with_a_content_hash(self) -> None:
        """Catches a manifest that does not identify the emitted immutable image."""
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "full-review.png"

            entry = chart.render_full_chart_png("graph TD\n    A --> B", image_path)

            self.assertEqual(entry["path"], "full-review.png")
            self.assertTrue(image_path.exists())
            self.assertGreater(image_path.stat().st_size, 0)
            self.assertEqual(chart.sha256_file(image_path), entry["sha256"])

    def test_full_chart_export_has_room_for_tall_primary_section_layout(self) -> None:
        """Catches an LR outline being clipped at the fixed browser viewport height."""
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "tall-review.png"
            sections = [
                chart.ReviewSection(f"Section {index}", 2, index)
                for index in range(1, 13)
            ]
            children = "\n".join(
                f'    R --> N{index}["Section {index}"]' for index in range(1, 13)
            )

            chart.render_full_chart_png(
                f"graph LR\n    R[\"Review\"]\n{children}",
                image_path,
                sections=sections,
            )

            with Image.open(image_path) as rendered:
                self.assertGreater(rendered.height, 900)

    def test_plain_text_converts_common_latex_for_chart_labels(self) -> None:
        """Catches raw TeX commands leaking into the raster chart labels."""
        value = r"{\mathrm {CO}}_{2} and S_{N^{2}}\prime with \alpha-allenes"

        self.assertEqual(chart.plain_chart_text(value), "CO2 and SN2' with alpha-allenes")

    def test_plain_text_converts_spaced_mineru_latex(self) -> None:
        """Catches MinerU's spaces around subscript and superscript delimiters."""
        value = r"{\mathrm {CO}} _ {2} and S _ {N ^ {2}} \prime"

        self.assertEqual(chart.plain_chart_text(value), "CO2 and SN2'")

    def test_plain_text_compacts_spaces_inside_mineru_math_groups(self) -> None:
        """Catches chart labels such as C O 2 instead of the readable CO2."""
        value = r"{\mathrm { C O }} _ { 2 } and S _ { N ^ { 2 } }"

        self.assertEqual(chart.plain_chart_text(value), "CO2 and SN2")

    def test_explicit_project_preview_wins_over_final_draft(self) -> None:
        """Catches fallback selection overriding a requested preview manuscript."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            preview = project / "previews" / "conclusion.md"
            final_draft = project / "05_final_audit" / "final_draft.md"
            preview.parent.mkdir(parents=True)
            final_draft.parent.mkdir(parents=True)
            preview.write_bytes(b"# Preview conclusion\n")
            final_draft.write_bytes(b"# Existing final draft\n")

            path, payload = chart.resolve_draft(project, str(preview))

            self.assertEqual(path, preview.resolve())
            self.assertEqual(payload, b"# Preview conclusion\n")

    def test_explicit_manuscript_outside_project_is_rejected(self) -> None:
        """Catches accepting a preview path that escapes the selected project."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            outside = root / "outside.md"
            project.mkdir()
            outside.write_bytes(b"# Not this project\n")

            with self.assertRaisesRegex(
                ValueError, "^input markdown must be inside the selected project$"
            ):
                chart.resolve_draft(project, str(outside))


if __name__ == "__main__":
    unittest.main()
