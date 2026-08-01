"""Regression checks for the canonical project-status artifact contract."""

from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path


STATUS_SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "review-writing-orchestrator"
    / "scripts"
    / "project_status.py"
)


class ProjectStatusChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = runpy.run_path(str(STATUS_SCRIPT))

    def test_current_citations_entries_schema_is_valid(self) -> None:
        valid = self.module["valid_citations_data"]
        self.assertTrue(
            valid(
                {
                    "project_id": "demo",
                    "entries": [{"callout": "[1]", "paper_id": "P001"}],
                }
            )
        )

    def test_optional_conclusion_receipt_accepts_current_first_draft(self) -> None:
        validate = self.module["conclusion_receipt_is_valid"]
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            draft = project / "04_first_draft" / "first_draft.md"
            final = project / "05_final_audit"
            draft.parent.mkdir(parents=True)
            final.mkdir(parents=True)
            draft.write_text("# Review\n", encoding="utf-8")
            (final / "conclusion_integration.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "first_draft_without_optional_conclusion",
                        "first_draft_path": str(draft.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(validate(project, "# Review\n"))

    def test_obsolete_files_are_not_required(self) -> None:
        stages = self.module["STAGES"]
        required = {name for stage in stages for name in stage["required"]}
        self.assertFalse(
            required
            & {
                "draft_bundle.json",
                "content_audit_report.md",
                "format_audit_report.md",
                "final_remaining_issues.md",
                "release_report.md",
            }
        )


if __name__ == "__main__":
    unittest.main()
