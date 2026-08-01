from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import serve_review_dashboard as dashboard


class MatrixOutlineChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project_id = "outline-check"
        self.project = self.root / "review-projects" / self.project_id
        discovery = self.project / "00_discovery"
        discovery.mkdir(parents=True)
        topic = (
            "axially chiral allenes categorized by substrate classes including "
            "propargylic alcohols, propargylic derivatives, terminal alkynes, and conjugated enynes"
        )
        dashboard.write_json(
            discovery / "selected_discovery_results.json",
            {
                "project_id": self.project_id,
                "human_confirmed": True,
                "local_papers": [{"paper_id": f"P{i:03d}"} for i in range(1, 7)],
            },
        )
        dashboard.write_json(discovery / "combined_results_by_keyword.json", {"topic": topic})
        dashboard.write_json(
            discovery / "keyword_set.draft.json",
            {
                "user_topic": topic,
                "user_keywords": [
                    "propargylic alcohols",
                    "propargylic derivatives",
                    "terminal alkynes",
                    "conjugated enynes",
                ],
                "merged_keywords": [],
            },
        )
        titles = {
            "P001": "Catalytic conversion of propargylic alcohols to chiral allenes",
            "P002": "Stereospecific coupling of propargylic carbonates",
            "P003": "Asymmetric synthesis of allenes from terminal alkynes",
            "P004": "Rhodium-catalyzed 1,6-addition to conjugated enynes",
            "P005": "Propargylic acetate substitution to allenes",
            "P006": "A distinct direct allenylation method",
        }
        metadata_dir = self.root / "review-library" / "metadata" / "papers"
        metadata_dir.mkdir(parents=True)
        for paper_id, title in titles.items():
            dashboard.write_json(
                metadata_dir / f"{paper_id}.metadata.json",
                {
                    "title": {"value": title},
                    "abstract": {"value": title},
                    # Deliberately collapsed metadata classification: the
                    # outline must detect this and use evidence-based hints.
                    "structured_tags": {"value": {"substrate": "propargylic alcohols"}},
                },
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_matrix_preserves_topic_and_comparison_axes(self) -> None:
        dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        matrix = dashboard.read_json_if_exists(self.project / "01_matrix_outline" / "literature_matrix.json")
        self.assertIn("axially chiral allenes", matrix["review_topic"])
        self.assertEqual(matrix["project_id"], self.project_id)
        self.assertEqual(
            matrix["comparison_axes"],
            ["propargylic alcohols", "propargylic derivatives", "terminal alkynes", "conjugated enynes"],
        )

    def test_collapsed_metadata_falls_back_to_semantic_outline_groups(self) -> None:
        dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        matrix = dashboard.read_json_if_exists(self.project / "01_matrix_outline" / "literature_matrix.json")
        groups = dashboard.outline_groups(self.root, self.project_id, matrix["rows"], "substrate")
        self.assertGreaterEqual(len(groups), 4)
        self.assertEqual(groups["propargylic derivatives"], ["P002", "P005"])
        self.assertEqual(groups["terminal alkynes"], ["P003"])
        self.assertEqual(groups["conjugated enynes"], ["P004"])
        assigned = [paper_id for paper_ids in groups.values() for paper_id in paper_ids]
        self.assertCountEqual(assigned, [f"P{i:03d}" for i in range(1, 7)])
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_new_blueprint_handoff_does_not_rebaseline_old_section_outputs(self) -> None:
        dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        stage = self.project / "01_matrix_outline"
        selected_at = dashboard.now_utc()
        matrix = dashboard.read_json_if_exists(stage / "literature_matrix.json")
        (stage / "selected_outline.md").write_text(
            dashboard.selected_outline_document(
                self.root,
                self.project_id,
                matrix["rows"],
                "substrate",
                selected_at,
            ),
            encoding="utf-8",
        )
        dashboard.regenerate_section_blueprint(self.root, self.project_id)
        section_stage = self.project / "02_section_drafting"
        dashboard.write_json(section_stage / "section_drafts.json", {"sections": [{"section_id": "old"}]})
        dashboard.regenerate_section_tasks(self.root, self.project_id)

        freshness = dashboard.section_source_freshness(section_stage)

        self.assertTrue(freshness["stale"])
        handoff = dashboard.read_json_if_exists(section_stage / "section_handoff.json")
        self.assertFalse(handoff.get("output_versions"))


if __name__ == "__main__":
    unittest.main()
