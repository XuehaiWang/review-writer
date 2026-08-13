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

    def test_human_checks_use_deployment_independent_routes(self) -> None:
        stages = self.module["STAGES"]
        checks = "\n".join(stage["human_check"] for stage in stages)
        self.assertNotIn("127.0.0.1", checks)
        self.assertIn("/discovery", checks)
        self.assertIn("/draft", checks)

    def test_orphan_optional_outputs_are_not_reported_as_completed(self) -> None:
        summarize = self.module["summarize"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            (project / "04_first_draft").mkdir(parents=True)
            (project / "05_final_audit").mkdir(parents=True)
            (project / "04_first_draft" / "conclusion_generated.md").write_text(
                "Old conclusion", encoding="utf-8"
            )
            (project / "05_final_audit" / "review_summary_chart.html").write_text(
                "old", encoding="utf-8"
            )
            (project / "05_final_audit" / "review_summary_chart.json").write_text(
                "{}", encoding="utf-8"
            )
            (project / "05_final_audit" / "review_summary_chart.png").write_bytes(b"old")

            result = summarize(root, "demo")

            self.assertNotIn("conclusion_generation", result["completed_stage_ids"])
            self.assertNotIn("summary_chart", result["completed_stage_ids"])
            self.assertEqual(result["next_stage"]["id"], "discovery")
            final_stage = next(
                stage for stage in result["stages"] if stage["id"] == "final_audit"
            )
            self.assertNotIn(
                "generated_conclusion_missing_from_final_draft",
                final_stage["semantic_issues"],
            )


if __name__ == "__main__":
    unittest.main()
