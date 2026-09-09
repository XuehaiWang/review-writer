from __future__ import annotations

import unittest

from review_writer_core.academic_contracts import taxonomy_diagnostics
from review_writer_core.classification_axes import canonical_classification_contract
from review_writer_core.section_narrative_contracts import (
    apply_single_paper_policy,
    canonical_argument_role,
    derive_narrative_diagnostics,
    derive_scientific_thesis,
    derive_section_depth_contract,
)


class SectionNarrativeContractTests(unittest.TestCase):
    def test_single_paper_policy_limits_writing_without_inventing_evidence_or_merging(self) -> None:
        section = {
            "section_id": "S02", "title": "Independent category", "section_role": "body",
            "primary_papers": ["P001"], "scientific_thesis": {"text": "Provisional question", "status": "provisional"},
            "evidence_readiness": {"status": "insufficient"}, "targeted_fact_gaps": {"P001": ["scope"]},
            "writing_requirements": [{"type": "cross_study_synthesis", "source": "native_blueprint", "instruction": "Compare studies"}],
        }
        bounded = apply_single_paper_policy(section)
        self.assertEqual(section["primary_papers"], bounded["primary_papers"])
        self.assertEqual(section["scientific_thesis"], bounded["scientific_thesis"])
        self.assertEqual(section["evidence_readiness"], bounded["evidence_readiness"])
        self.assertEqual(section["targeted_fact_gaps"], bounded["targeted_fact_gaps"])
        self.assertEqual(1, len(bounded["writing_requirements"]))
        self.assertEqual("evidence_boundary", bounded["writing_requirements"][0]["type"])
        self.assertIn("field-wide consensus", bounded["writing_requirements"][0]["instruction"])
        self.assertEqual(bounded["writing_requirements"][0]["requirement_id"], bounded["single_paper_policy"]["requirement_id"])
        self.assertNotIn("single_paper_policy", section)
        self.assertEqual(bounded, apply_single_paper_policy(bounded))
        report = taxonomy_diagnostics([bounded], ["P001"], classification_contract={"minimum_body_papers": 2})
        self.assertEqual([], report["unjustified_single_paper_section_ids"])
        self.assertEqual([], report["single_paper_merge_suggestions"])
        invalid = {**bounded, "title": "Other or unspecified"}
        self.assertFalse(taxonomy_diagnostics([invalid], ["P001"])["can_confirm"])
        for role, papers in [("body", ["P001", "P002"]), ("introduction", ["P001"]), ("conclusion", ["P001"])]:
            other = {**section, "section_role": role, "primary_papers": papers}
            self.assertEqual(other, apply_single_paper_policy(other))

    def test_single_paper_policy_cleans_legacy_copies_and_preserves_custom_objectives(self) -> None:
        from review_writer_core.academic_contracts import section_academic_contract

        section = {"section_id": "S02", "primary_papers": ["P001"]}
        canonical = apply_single_paper_policy(section)
        instruction = canonical["writing_requirements"][0]["instruction"]
        for objective in [instruction, "Explain the observed selectivity pattern."]:
            legacy = {
                **section,
                "single_paper_policy": {"instruction": instruction},
                "avoid_patterns": ["Do not mix distinct substrate classes.", instruction],
                "academic_contract": {"expected_synthesis": objective, "semantic_scope": "Selected scope"},
                "writing_requirements": [
                    {"type": "source_bounded_case_analysis", "source": "native_blueprint", "instruction": instruction},
                    {"source": "single_paper_policy", "instruction": instruction},
                    {"source": "user", "instruction": "Discuss the reported mechanism."},
                ],
            }
            result = apply_single_paper_policy(legacy)
            self.assertEqual(2, len(result["writing_requirements"]))
            self.assertEqual("user", result["writing_requirements"][0]["source"])
            self.assertNotIn("instruction", result["single_paper_policy"])
            self.assertEqual(["Do not mix distinct substrate classes."], result["avoid_patterns"])
            self.assertEqual("Selected scope", result["academic_contract"]["semantic_scope"])
            expected = section_academic_contract(result)["expected_synthesis"] if objective == instruction else objective
            self.assertEqual(expected, result["academic_contract"]["expected_synthesis"])
            self.assertEqual(result, apply_single_paper_policy(result))

    def test_scientific_thesis_is_derived_from_source_backed_matrix_facts(self) -> None:
        section = {
            "section_id": "S02",
            "title": "Representative methods",
            "section_role": "body",
            "primary_papers": ["P001", "P002"],
        }
        rows = {
            "P001": {
                "scientific_facts": [
                    {
                        "field_id": "object_input",
                        "value": "input class A",
                        "evidence_refs": [{"evidence_key": "sha256:a"}],
                    },
                    {
                        "field_id": "method_conditions",
                        "value": "method family X",
                        "evidence_refs": [{"evidence_key": "sha256:b"}],
                    },
                    {
                        "field_id": "quantitative_results",
                        "value": "reported outcome range A",
                        "evidence_refs": [{"evidence_key": "sha256:c"}],
                    },
                ]
            },
            "P002": {
                "scientific_facts": [
                    {
                        "field_id": "object_input",
                        "value": "input class B",
                        "evidence_refs": [{"evidence_key": "sha256:d"}],
                    },
                    {
                        "field_id": "method_conditions",
                        "value": "method family Y",
                        "evidence_refs": [{"evidence_key": "sha256:e"}],
                    },
                    {
                        "field_id": "scope",
                        "value": "reported scope B",
                        "evidence_refs": [{"evidence_key": "sha256:f"}],
                    },
                ]
            },
        }
        contract = canonical_classification_contract(
            [
                {"axis_id": "research_object", "axis_role": "primary_organization"},
                {"axis_id": "method", "axis_role": "comparison_dimension"},
            ]
        )

        thesis = derive_scientific_thesis(section, rows, contract)

        self.assertEqual("evidence_grounded", thesis["status"])
        self.assertIn("input class A", thesis["text"])
        self.assertIn("method family X", thesis["text"])
        self.assertIn("reported outcome range A", thesis["text"])
        self.assertEqual(["P001", "P002"], thesis["components"]["source_backed_paper_ids"])

    def test_missing_outcomes_produce_provisional_thesis_not_fake_absence(self) -> None:
        thesis = derive_scientific_thesis(
            {
                "section_id": "S02",
                "title": "A bounded section",
                "section_role": "body",
                "primary_papers": ["P001"],
            },
            {
                "P001": {
                    "scientific_facts": [
                        {
                            "field_id": "object_input",
                            "value": "input A",
                            "evidence_refs": [{"evidence_key": "sha256:a"}],
                        }
                    ]
                }
            },
            {"primary_axis_id": "research_object"},
        )

        self.assertEqual("provisional", thesis["status"])
        self.assertIn("supported_shared_understanding", thesis["missing_components"])
        self.assertIn("until the missing outcome evidence is retrieved", thesis["text"])

    def test_depth_and_role_diagnostics_are_derived(self) -> None:
        depth = derive_section_depth_contract(
            {
                "section_role": "body",
                "primary_papers": ["P001", "P002", "P003"],
                "target_words": 1200,
            }
        )
        writing = {
            "section_role": "body",
            "paragraphs": [
                {"argument_role": "section_frame"},
                {"argument_role": "reported_evidence"},
                {"argument_role": "comparison"},
                {"argument_role": "comparison"},
                {"argument_role": "section_synthesis_exit"},
            ],
        }

        diagnostics = derive_narrative_diagnostics(writing, depth)

        self.assertEqual("complete", diagnostics["status"])
        self.assertEqual(2, diagnostics["comparison_paragraph_count"])
        self.assertEqual("anchor_case", canonical_argument_role("reported_evidence"))
        self.assertEqual(960, depth["target_word_min"])
        self.assertEqual(1500, depth["target_word_max"])

    def test_unjustified_single_paper_section_is_warning_with_repair_suggestion(self) -> None:
        diagnostics = taxonomy_diagnostics(
            [
                {"section_id": "S01", "title": "Introduction", "section_role": "introduction"},
                {
                    "section_id": "S02",
                    "title": "Method family A",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                },
                {
                    "section_id": "S03",
                    "title": "Method family B",
                    "section_role": "body",
                    "primary_papers": ["P002", "P003"],
                },
                {"section_id": "S04", "title": "Conclusion", "section_role": "conclusion"},
            ],
            ["P001", "P002", "P003"],
            classification_contract={
                "primary_axis_id": "method",
                "minimum_body_papers": 2,
                "single_paper_section_policy": "merge_unless_scientifically_justified",
            },
        )

        self.assertTrue(diagnostics["can_confirm"])
        self.assertEqual(["S02"], diagnostics["unjustified_single_paper_section_ids"])
        self.assertEqual("S03", diagnostics["single_paper_merge_suggestions"][0]["target_section_id"])
        self.assertIn(
            "taxonomy.single_paper_section_unjustified",
            {issue["rule_id"] for issue in diagnostics["issues"]},
        )

    def test_multiple_declared_primary_axes_block_confirmation(self) -> None:
        diagnostics = taxonomy_diagnostics(
            [
                {"section_id": "S01", "title": "Introduction", "section_role": "introduction"},
                {
                    "section_id": "S02",
                    "title": "Body",
                    "section_role": "body",
                    "primary_papers": ["P001", "P002"],
                },
                {"section_id": "S03", "title": "Conclusion", "section_role": "conclusion"},
            ],
            ["P001", "P002"],
            classification_contract={
                "section_partition_policy": "single_primary_axis",
                "classification_axes": [
                    {"axis_id": "object", "axis_role": "primary_organization"},
                    {"axis_id": "method", "axis_role": "primary_organization"},
                ]
            },
        )

        self.assertFalse(diagnostics["can_confirm"])
        self.assertIn(
            "taxonomy.primary_axis_contract_invalid",
            {issue["rule_id"] for issue in diagnostics["issues"]},
        )


if __name__ == "__main__":
    unittest.main()
