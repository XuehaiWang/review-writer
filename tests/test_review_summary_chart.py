"""Behavior tests for selecting a review-summary-chart manuscript."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


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
