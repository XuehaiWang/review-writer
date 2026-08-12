from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import serve_review_dashboard as dashboard


MATRIX_PAGE = Path(__file__).resolve().parent / "assets" / "dashboard" / "matrix.html"


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

    def test_discovery_selection_is_not_capped_at_thirty_papers(self) -> None:
        groups = [
            {
                "keyword": "allene",
                "category": "reaction",
                "keep": True,
                "local_results": [
                    {
                        "paper_id": f"P{index:03d}",
                        "title": f"Candidate {index}",
                        "score": 100 - index,
                        "keep": True,
                    }
                    | {"role": "excluded" if index == 46 else "uncertain"}
                    for index in range(1, 47)
                ],
                "web_results": [],
            }
        ]

        selected = dashboard.selected_from_combined(groups, self.project_id)

        self.assertEqual(len(selected["local_papers"]), 45)
        self.assertEqual(selected["local_papers"][0]["paper_id"], "P001")
        self.assertEqual(selected["local_papers"][-1]["paper_id"], "P045")
        metadata_dir = self.root / "review-library" / "metadata" / "papers"
        for index in range(7, 46):
            paper_id = f"P{index:03d}"
            dashboard.write_json(
                metadata_dir / f"{paper_id}.metadata.json",
                {"title": {"value": f"Candidate {index}"}, "abstract": {"value": "Candidate abstract"}},
            )
        selected["human_confirmed"] = True
        dashboard.write_json(
            self.project / "00_discovery" / "selected_discovery_results.json",
            selected,
        )

        sync = dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        matrix = dashboard.read_json_if_exists(self.project / "01_matrix_outline" / "literature_matrix.json")

        self.assertEqual(sync["selected_paper_count"], 45)
        self.assertEqual(len(matrix["rows"]), 45)
        self.assertEqual(sync["selected_paper_ids"], [f"P{index:03d}" for index in range(1, 46)])
        self.assertTrue(sync["selection_current"])
        self.assertTrue(dashboard.project_matrix_payload(self.root, self.project_id)["discovery_selection"]["selection_current"])

    def test_reconfirming_discovery_replaces_matrix_membership_exactly(self) -> None:
        discovery_path = self.project / "00_discovery" / "selected_discovery_results.json"
        dashboard.write_json(
            discovery_path,
            {
                "project_id": self.project_id,
                "human_confirmed": True,
                "local_papers": [{"paper_id": "P001"}, {"paper_id": "P002"}],
            },
        )
        first = dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        self.assertEqual(first["selected_paper_ids"], ["P001", "P002"])

        dashboard.write_json(
            discovery_path,
            {
                "project_id": self.project_id,
                "human_confirmed": True,
                "local_papers": [{"paper_id": "P002"}, {"paper_id": "P003"}],
            },
        )
        second = dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        matrix = dashboard.read_json_if_exists(self.project / "01_matrix_outline" / "literature_matrix.json")

        self.assertEqual([row["paper_id"] for row in matrix["rows"]], ["P002", "P003"])
        self.assertEqual(second["added_paper_ids"], ["P003"])
        self.assertEqual(second["removed_paper_ids"], ["P001"])
        self.assertTrue(dashboard.project_matrix_payload(self.root, self.project_id)["discovery_selection"]["selection_current"])

    def test_explicit_candidate_selection_defaults_to_not_in_matrix(self) -> None:
        groups = [
            {
                "keyword": "allene",
                "keep": True,
                "local_results": [
                    {"paper_id": "P001", "keep": True, "selected_for_matrix": False},
                    {"paper_id": "P002", "keep": True, "selected_for_matrix": True},
                    {"paper_id": "P003", "keep": True, "selected_for_matrix": False},
                ],
            }
        ]

        selected = dashboard.selected_from_combined(groups, self.project_id)

        self.assertEqual([row["paper_id"] for row in selected["local_papers"]], ["P002"])

    def test_explicit_selection_overrides_legacy_candidate_keep_flag(self) -> None:
        groups = [
            {
                "keyword": "copper catalysis",
                "keep": True,
                "local_results": [
                    {
                        "paper_id": "P125",
                        "keep": False,
                        "selected_for_matrix": True,
                        "role": "core_candidate",
                    }
                ],
                "web_results": [],
            }
        ]

        selected = dashboard.selected_from_combined(groups, self.project_id)

        self.assertEqual([row["paper_id"] for row in selected["local_papers"]], ["P125"])

    def test_manual_outline_requires_content_and_a_major_section(self) -> None:
        with self.assertRaisesRegex(ValueError, "level-2 section heading"):
            dashboard.validate_selected_outline_markdown("# Title only")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            dashboard.validate_selected_outline_markdown("")
        with self.assertRaisesRegex(ValueError, "Every major section must assign"):
            dashboard.validate_selected_outline_markdown("## Introduction\nPurpose: frame the topic.\n")
        valid = "## Introduction\nAssigned papers: P001, P002.\nPurpose: frame the topic.\n"
        self.assertEqual(dashboard.validate_selected_outline_markdown(valid), valid)

    def test_matrix_page_has_fourth_custom_option_and_editable_selected_outline(self) -> None:
        source = MATRIX_PAGE.read_text(encoding="utf-8")

        self.assertIn("['custom','E','Custom outline'", source)
        self.assertIn('id="selectedOutlineEditor"', source)
        self.assertIn('id="saveOutlineEdits"', source)
        self.assertIn("outline_md:outlineMarkdownDraft", source)
        self.assertIn("const request={outline_style:style}", source)
        self.assertNotIn("request.outline_md=payload.selected_outline_md", source)
        self.assertIn("Reset to blank", source)
        self.assertIn("resettingCustom", source)
        self.assertIn("Visual outline builder", source)
        self.assertIn("data-section-paper", source)
        self.assertIn("recommendedPaperIds", source)
        self.assertIn("dragstart", source)
        self.assertIn("renderOutlineComparison", source)
        self.assertIn("Current saved version", source)
        self.assertIn("System-generated candidates", source)
        self.assertIn("selection.selection_source==='user'", source)

    def test_built_in_outline_assigns_representative_papers_to_intro_and_conclusion(self) -> None:
        dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        matrix = dashboard.read_json_if_exists(self.project / "01_matrix_outline" / "literature_matrix.json")
        document = dashboard.selected_outline_document(
            self.root,
            self.project_id,
            matrix["rows"],
            "substrate",
            dashboard.now_utc(),
        )

        sections = document.split("## ")[1:]
        introduction = next(section for section in sections if section.startswith("Introduction"))
        conclusion = next(section for section in sections if section.startswith("Cross-category comparison"))
        self.assertIn("Assigned papers:", introduction)
        self.assertIn("Assigned papers:", conclusion)

    def test_blueprint_accepts_manual_level_two_section_headings(self) -> None:
        dashboard.sync_matrix_from_discovery(self.root, self.project_id)
        stage = self.project / "01_matrix_outline"
        (stage / "selected_outline.md").write_text(
            "# Selected Outline\n\n"
            "## Introduction and scope\n"
            "Purpose: define the review scope.\n\n"
            "## Evidence synthesis\n"
            "Assigned papers: P001, P002.\n"
            "Purpose: compare the core evidence.\n",
            encoding="utf-8",
        )

        dashboard.regenerate_section_blueprint(self.root, self.project_id)
        blueprint = dashboard.read_json_if_exists(stage / "section_blueprint.json")

        self.assertEqual(
            [section["title"] for section in blueprint["sections"]],
            ["Introduction and scope", "Evidence synthesis"],
        )
        self.assertEqual(blueprint["sections"][1]["major_papers"], ["P001", "P002"])

    def test_legacy_confirmed_set_becomes_explicit_without_selecting_every_candidate(self) -> None:
        data = {
            "results": [
                {
                    "keyword": "allene",
                    "keep": True,
                    "local_results": [
                        {"paper_id": "P001", "keep": True},
                        {"paper_id": "P002", "keep": True},
                        {"paper_id": "P003", "keep": True},
                    ],
                }
            ]
        }
        legacy_selected = {
            "human_confirmed": True,
            "local_papers": [{"paper_id": "P002"}],
        }

        payload = dashboard.discovery_payload_with_explicit_selection(data, legacy_selected)
        rows = payload["results"][0]["local_results"]

        self.assertEqual([row["selected_for_matrix"] for row in rows], [False, True, False])
        self.assertEqual(payload["selection_mode"], "explicit")

    def test_unconfirmed_legacy_candidates_start_unselected(self) -> None:
        data = {
            "results": [
                {
                    "keyword": "allene",
                    "keep": True,
                    "local_results": [{"paper_id": "P001", "keep": True}],
                }
            ]
        }

        payload = dashboard.discovery_payload_with_explicit_selection(
            data,
            {"human_confirmed": False, "local_papers": [{"paper_id": "P001"}]},
        )

        self.assertFalse(payload["results"][0]["local_results"][0]["selected_for_matrix"])

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
