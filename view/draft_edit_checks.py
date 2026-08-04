"""Regression checks for accepting manual Draft edits into artifact lineage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import serve_review_dashboard as dashboard


class DraftEditChecks(unittest.TestCase):
    def test_paragraph_edit_preserves_canonical_citation_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage = root / "review-projects" / "demo" / "04_first_draft"
            stage.mkdir(parents=True)
            (stage / "first_draft.md").write_text(
                "# Demo\n\n## Section\n\nOriginal [1].\n\n"
                "<!-- paragraph_id: sec1-p1 -->\n\n## References\n\n[1] Example reference.\n",
                encoding="utf-8",
            )
            (stage / "citations.json").write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "entries": [{"callout": 1, "paper_id": "P001"}],
                    }
                ),
                encoding="utf-8",
            )

            result = dashboard.ParagraphEditor(root, "demo").update_paragraph(
                "sec1-p1", "Edited [1].", "citation regression"
            )

            self.assertTrue(result["ok"])
            citations = json.loads((stage / "citations.json").read_text(encoding="utf-8"))
            self.assertEqual(citations["project_id"], "demo")
            self.assertEqual(citations["entries"], [{"callout": 1, "paper_id": "P001"}])
            self.assertIn("[1] Example reference.", (stage / "first_draft.md").read_text(encoding="utf-8"))

    def test_manual_edit_refreshes_draft_output_hash_without_rebasing_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            sections = project / "02_section_drafting"
            figures = project / "03_figure_redraw"
            draft = project / "04_first_draft"
            for directory in (sections, figures, draft):
                directory.mkdir(parents=True, exist_ok=True)

            (sections / "section_drafts.json").write_text('{"sections": []}', encoding="utf-8")
            (sections / "human_figure_review.json").write_text('{"papers": {}}', encoding="utf-8")
            (figures / "redrawn_figure_manifest.json").write_text('{"figures": []}', encoding="utf-8")
            draft_path = draft / "first_draft.md"
            draft_path.write_text(
                "# Demo\n\n## 1 Section\n\nOriginal text.\n\n<!-- paragraph_id: sec1-p1 -->\n",
                encoding="utf-8",
            )

            handoff = draft / "draft_handoff.json"
            dashboard.write_stage_handoff(
                handoff,
                "figures",
                [
                    sections / "section_drafts.json",
                    sections / "human_figure_review.json",
                    figures / "redrawn_figure_manifest.json",
                ],
            )
            dashboard.record_stage_outputs(handoff, [draft_path], "draft")
            original_source_fingerprint = dashboard.read_json_if_exists(handoff)["source_fingerprint"]

            result = dashboard.ParagraphEditor(root, "demo").update_paragraph(
                "sec1-p1", "Edited text.", "regression check"
            )
            self.assertTrue(result["ok"])
            self.assertTrue(dashboard.artifact_freshness(handoff, [draft_path])["stale"])

            refreshed = dashboard.refresh_manual_draft_outputs(root, "demo")

            self.assertFalse(refreshed["stale"])
            self.assertFalse(dashboard.artifact_freshness(handoff, [draft_path])["stale"])
            self.assertEqual(
                dashboard.read_json_if_exists(handoff)["source_fingerprint"],
                original_source_fingerprint,
            )


if __name__ == "__main__":
    unittest.main()
