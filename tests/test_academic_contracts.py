from __future__ import annotations

import unittest

from review_writer_core.academic_contracts import (
    classification_basis,
    coverage_diagnostics,
    derive_scope_contract,
    evidence_key,
    is_catch_all_heading,
    section_academic_contract,
    scope_diagnostics,
    synthesis_requirements,
    taxonomy_diagnostics,
)


class AcademicContractTests(unittest.TestCase):
    def test_catch_all_detection_does_not_reject_meaningful_other_heading(self) -> None:
        self.assertTrue(is_catch_all_heading("02 Other or unspecified"))
        self.assertTrue(is_catch_all_heading("其他或未指定"))
        self.assertTrue(is_catch_all_heading("Routing required: P001"))
        self.assertFalse(is_catch_all_heading("Other applications of axially chiral allenes"))

    def test_scope_is_derived_without_overwriting_explicit_values(self) -> None:
        scope = derive_scope_contract(
            "Axially chiral allenes",
            "reaction",
            [{"year": 1955}, {"year": {"value": 2026}}],
            current={"target_question": "Which stereocontrol strategies are transferable?"},
        )
        self.assertEqual(
            "Which stereocontrol strategies are transferable?",
            scope["target_question"],
        )
        self.assertEqual("reaction_strategy", scope["primary_navigation_axis"])
        self.assertEqual({"from": 1955, "to": 2026, "basis": "selected_matrix"}, scope["time_span"])

    def test_taxonomy_blocks_catch_all_and_orphan_papers(self) -> None:
        report = taxonomy_diagnostics(
            [
                {"section_id": "S01", "title": "Introduction", "section_role": "introduction"},
                {
                    "section_id": "S02",
                    "title": "Other or unspecified",
                    "section_role": "body",
                    "paper_ids": ["P001"],
                },
                {"section_id": "S03", "title": "Conclusion", "section_role": "conclusion"},
            ],
            ["P001", "P002"],
        )
        self.assertFalse(report["can_confirm"])
        self.assertEqual(["S02"], report["catch_all_section_ids"])
        self.assertEqual(["P002"], report["orphan_paper_ids"])
        self.assertEqual(
            {"taxonomy.catch_all_body_section", "taxonomy.orphan_papers"},
            {item["rule_id"] for item in report["issues"]},
        )

    def test_taxonomy_allows_unique_primary_routes(self) -> None:
        report = taxonomy_diagnostics(
            [
                {"section_id": "S01", "title": "Introduction", "section_role": "introduction"},
                {"section_id": "S02", "title": "De novo construction", "section_role": "body", "paper_ids": ["P001"]},
                {"section_id": "S03", "title": "Chirality transfer", "section_role": "body", "paper_ids": ["P002"]},
                {"section_id": "S04", "title": "Conclusion", "section_role": "conclusion"},
            ],
            ["P001", "P002"],
        )
        self.assertTrue(report["can_confirm"])
        self.assertEqual([], report["issues"])

    def test_dominant_boundary_section_must_be_rerouted(self) -> None:
        report = taxonomy_diagnostics(
            [
                {"section_id": "S01", "title": "Introduction", "section_role": "introduction"},
                {"section_id": "S02", "title": "Defined precursor class", "section_role": "body", "paper_ids": ["P001", "P002", "P007", "P008"]},
                {
                    "section_id": "S03",
                    "title": "Cross-category evidence and boundary cases",
                    "section_role": "body",
                    "paper_ids": ["P003", "P004", "P005", "P006"],
                },
                {"section_id": "S04", "title": "Conclusion", "section_role": "conclusion"},
            ],
            ["P001", "P002", "P003", "P004", "P005", "P006", "P007", "P008"],
        )

        self.assertFalse(report["can_confirm"])
        self.assertEqual(["S03"], report["dominant_boundary_section_ids"])
        self.assertIn(
            "taxonomy.dominant_boundary_section",
            {item["rule_id"] for item in report["issues"]},
        )

    def test_contextual_review_is_routed_without_becoming_a_body_orphan(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Introduction",
                    "section_role": "introduction",
                    "context_papers": ["P002"],
                },
                {
                    "section_id": "S02",
                    "title": "Alkyne precursors",
                    "section_role": "body",
                    "major_papers": ["P001"],
                },
            ],
            ["P001", "P002"],
        )

        self.assertTrue(report["can_confirm"])
        self.assertEqual([], report["orphan_paper_ids"])
        self.assertEqual(1, report["contextual_paper_count"])

    def test_chemistry_section_selects_only_relevant_synthesis_components(self) -> None:
        section = {
            "title": "Mechanistic comparison of catalytic strategies",
            "section_role": "body",
            "section_thesis": "Compare stereodetermining transition states",
            "primary_papers": ["P001", "P002"],
        }
        requirements = synthesis_requirements(section, taxonomy_profile="chemistry_general")
        by_component = {item["component"]: item for item in requirements}
        self.assertEqual("required", by_component["comparison"]["necessity"])
        self.assertEqual("required", by_component["mechanism"]["necessity"])
        self.assertNotIn("roadmap", by_component)
        contract = section_academic_contract(section)
        self.assertEqual("analytical", contract["node_type"])
        self.assertEqual("evidence_synthesis", contract["academic_role"])

    def test_future_non_chemistry_profile_keeps_generic_requirements(self) -> None:
        requirements = synthesis_requirements(
            {
                "title": "Comparative governance approaches",
                "section_role": "body",
                "primary_papers": ["P001"],
            },
            taxonomy_profile="social_science_general",
        )
        self.assertEqual(["comparison"], [item["component"] for item in requirements])
        self.assertEqual("recommended", requirements[0]["necessity"])

    def test_evidence_key_is_order_stable_and_lineage_sensitive(self) -> None:
        first = evidence_key("P001", "C001", "lineage-a")
        second = evidence_key("P001", "C001", "lineage-a")
        changed = evidence_key("P001", "C001", "lineage-b")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))
        self.assertNotEqual(first, changed)

    def test_classification_basis_disallows_catch_all(self) -> None:
        basis = classification_basis("catalyst")
        self.assertEqual("catalyst_or_method", basis["primary_axis"])
        self.assertFalse(basis["catch_all_sections_allowed"])

    def test_scope_diagnostics_blocks_only_missing_executable_fields(self) -> None:
        valid = derive_scope_contract("Topic", "custom", [])
        self.assertTrue(scope_diagnostics(valid)["can_confirm"])
        invalid = {**valid, "target_question": "", "target_readers": []}
        report = scope_diagnostics(invalid)
        self.assertFalse(report["can_confirm"])
        self.assertEqual(
            {"scope.target_question_missing", "scope.target_readers_missing"},
            {item["rule_id"] for item in report["issues"]},
        )

    def test_coverage_diagnostics_are_bounded_and_explain_missing_fields(self) -> None:
        report = coverage_diagnostics(
            {"search_cutoff_date": "2026-08-20"},
            [
                {"year": 2022, "journal": "Journal A", "tags": {"reaction": ["addition"]}},
                {"year": 2026, "journal": "Journal A", "tags": {"reaction": ["addition"]}},
                {"year": "", "journal": "Journal B"},
            ],
        )
        self.assertEqual("selected_local_corpus_only", report["coverage_claim"])
        self.assertIsNone(report["global_coverage_percentage"])
        self.assertEqual(3, report["selected_paper_count"])
        self.assertEqual({"2022": 1, "2026": 1}, report["year_distribution"])
        self.assertEqual(1, report["year_unknown_count"])
        self.assertEqual("addition", report["topic_clusters"][0]["label"])


if __name__ == "__main__":
    unittest.main()
