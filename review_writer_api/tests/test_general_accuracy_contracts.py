from __future__ import annotations

import unittest

from sqlalchemy import String, column, func
from sqlalchemy.dialects import postgresql

from review_writer_api.domain_services.discovery import (
    discovery_coverage_diagnostics,
    discovery_search_record,
)
from review_writer_api.domain_services.final import FinalService
from review_writer_api.domain_services.library_index import (
    postgres_term_group_constraint,
)
from review_writer_api.domain_services.sections import strongest_support_level
from review_writer_core.academic_contracts import (
    mechanism_evidence_types,
    taxonomy_diagnostics,
)
from review_writer_core.evidence_queries import COMPARISON_FIELD_IDS
from review_writer_core.evidence_integrity import (
    technical_entity_anchors,
    unsupported_realization_anchors,
)


class GeneralAccuracyContractTests(unittest.TestCase):
    def test_postgres_term_groups_preserve_or_within_and_between_contract(self) -> None:
        expression = postgres_term_group_constraint(
            func.to_tsvector("simple", column("content", String)),
            [["allene", "cumulene"], ["mechanism", "intermediate"]],
        )

        compiled = expression.compile(dialect=postgresql.dialect())
        sql = str(compiled)

        self.assertEqual(4, sql.count("plainto_tsquery("))
        self.assertEqual(4, sql.count(" @@ "))
        self.assertIn(" OR ", sql)
        self.assertIn(" AND ", sql)
        self.assertTrue(
            {"allene", "cumulene", "mechanism", "intermediate"}.issubset(
                set(compiled.params.values())
            )
        )

    def test_realized_numbers_and_entities_must_exist_in_claim_evidence(self) -> None:
        self.assertEqual(
            {"quantitative": [], "technical_entities": []},
            unsupported_realization_anchors(
                "CuBr2 gave 95% ee in 2021.",
                ["CuBr2 gave 95% ee in 2021."],
            ),
        )
        unsupported = unsupported_realization_anchors(
            "ZnI2 gave 97% ee in 2021.",
            ["CuBr2 gave 95% ee in 2021."],
        )
        self.assertEqual({"97%", "97% ee"}, set(unsupported["quantitative"]))
        self.assertEqual(["ZnI2"], unsupported["technical_entities"])

        wrong_metric = unsupported_realization_anchors(
            "CuBr2 gave 95% ee.",
            ["CuBr2 gave 95% yield."],
        )
        self.assertIn("95% ee", wrong_metric["quantitative"])

    def test_topic_acronym_is_not_misread_as_chemical_formula(self) -> None:
        self.assertEqual([], technical_entity_anchors("ATA and EATA were compared."))

    def test_fact_card_support_cannot_downgrade_direct_chunk_support(self) -> None:
        self.assertEqual(
            "direct",
            strongest_support_level("direct", "abstract_limited"),
        )
        self.assertEqual(
            "direct",
            strongest_support_level("coverage_only", "direct"),
        )

    def test_search_record_uses_actual_source_status(self) -> None:
        review = {
            "results": [
                {
                    "keyword": "topic",
                    "local_results": [{"paper_id": "P001", "year": None}],
                    "web_results": [],
                }
            ],
            "external_search": {
                "requested_sources": ["crossref", "openalex"],
                "source_statuses": {
                    "crossref": {"status": "failed", "completed_queries": 1},
                    "openalex": {"status": "disabled", "completed_queries": 0},
                },
            },
        }

        record = discovery_search_record(review)

        self.assertEqual(["crossref"], record["executed_sources"])
        self.assertEqual(["crossref"], record["failed_sources"])
        self.assertNotIn("openalex", record["executed_sources"])

    def test_unknown_years_trigger_non_blocking_coverage_advice(self) -> None:
        review = {
            "results": [
                {
                    "keyword": "topic",
                    "local_results": [
                        {"paper_id": "P001"},
                        {"paper_id": "P002"},
                        {"paper_id": "P003"},
                    ],
                }
            ],
            "external_search": {"completion_state": "disabled"},
        }

        report = discovery_coverage_diagnostics(review)

        self.assertIn("coverage.publication_years_unknown", report["reason_codes"])
        self.assertTrue(report["online_search_suggested"])

    def test_existing_review_methods_are_removed_from_publication_manuscript(self) -> None:
        source = """# Review

## 1 Review Method

The project Library and internal query logs were used.

## Introduction

Body.
"""

        rebuilt = FinalService._remove_review_methods(source)

        self.assertNotIn("Review Method", rebuilt)
        self.assertNotIn("internal query logs", rebuilt)
        self.assertIn("## Introduction", rebuilt)
        self.assertIn("Body.", rebuilt)

    def test_internal_boundary_headings_are_sanitized_for_final_output(self) -> None:
        source = """# Review

## Topic-partition boundary cases — Ketone-based coupling

Body one.

## Enantioselective evidence — Cross-category evidence and boundary cases

Body two.
"""

        rebuilt = FinalService._sanitize_internal_section_headings(source)

        self.assertIn("## Ketone-based coupling", rebuilt)
        self.assertIn("## Enantioselective evidence", rebuilt)
        self.assertNotIn("Topic-partition boundary cases", rebuilt)
        self.assertNotIn("Cross-category evidence and boundary cases", rebuilt)

    def test_final_numbers_follow_first_citation_without_losing_paper_identity(self) -> None:
        rendered, ledger = FinalService._render_final_citation_numbers(
            "Later source [8] is discussed before the earlier-numbered source [3].",
            {
                "complete": True,
                "entries": [
                    {"callout": 8, "paper_id": "P008"},
                    {"callout": 3, "paper_id": "P003"},
                ],
            },
        )

        self.assertEqual(
            "Later source [1] is discussed before the earlier-numbered source [2].",
            rendered,
        )
        self.assertEqual(
            [(1, "P008"), (2, "P003")],
            [(row["callout"], row["paper_id"]) for row in ledger["entries"]],
        )

    def test_overview_prompt_residue_is_not_release_ready_content(self) -> None:
        report = FinalService._overview_semantic_report(
            {
                "title": "Please write a review on the topic of catalytic methods",
                "labels": ["module-cards-crosscut-sidebar"],
            },
            {"topic": "Please write a review on catalytic methods"},
        )

        self.assertEqual("invalid", report["status"])
        self.assertIn("overview_unsupported_labels", report["issues"])

    def test_overview_requires_labels_when_an_image_exists(self) -> None:
        report = FinalService._overview_semantic_report(
            {"title": "Catalytic allene synthesis", "labels": []},
            {"topic": "Catalytic allene synthesis"},
            {"sections": []},
            overview_present=True,
        )
        self.assertIn("overview_labels_missing", report["issues"])

    def test_overview_labels_trace_to_current_body_axis(self) -> None:
        report = FinalService._overview_semantic_report(
            {
                "title": "Catalytic allene synthesis",
                "labels": ["Propargylic alcohol substrates"],
            },
            {"topic": "Catalytic syntheses of allenes by substrate"},
            {
                "classification_basis": {"primary_axis": "substrate"},
                "sections": [
                    {
                        "section_role": "body",
                        "title": "Propargylic alcohol substrates",
                    }
                ],
            },
        )
        self.assertEqual("aligned", report["status"])

    def test_comparison_schema_includes_role_and_safety_without_guessing(self) -> None:
        self.assertIn("intervention_role", COMPARISON_FIELD_IDS)
        self.assertIn("safety_cost_sustainability", COMPARISON_FIELD_IDS)

    def test_taxonomy_contract_detects_missing_required_partition(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Introduction",
                    "section_role": "introduction",
                    "primary_papers": [],
                },
                {
                    "section_id": "S02",
                    "title": "Method A",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                },
            ],
            ["P001"],
            classification_contract={
                "topic_partitions": ["Method A", "Method B"],
                "catch_all_sections_allowed": False,
            },
        )

        self.assertEqual("drift", report["classification_contract_status"])
        self.assertIn("Method B", report["missing_topic_partitions"])

    def test_taxonomy_contract_accepts_source_supported_partition_boundary(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Method A",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                    "topic_partition": "Method A",
                }
            ],
            ["P001"],
            classification_contract={
                "topic_partitions": ["Method A", "Method B"],
                "topic_partition_coverage_boundaries": {
                    "Method B": {
                        "reason": "No selected source supports this partition.",
                        "source": "matrix_evidence_partition_classifier",
                    }
                },
                "catch_all_sections_allowed": False,
            },
        )

        self.assertEqual(
            "aligned_with_boundaries", report["classification_contract_status"]
        )
        self.assertEqual([], report["missing_topic_partitions"])
        self.assertEqual(["Method B"], report["bounded_topic_partitions"])
        self.assertEqual(["Method B"], report["topic_partition_route_gaps"])

    def test_taxonomy_contract_traces_partition_from_section_contract_and_alias(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Introduction",
                    "section_role": "introduction",
                },
                {
                    "section_id": "S02",
                    "title": "Carbonyl-based transformations",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                    "purpose": (
                        "Within this reaction class, separately compare racemic ATA "
                        "and enantioselective ATA evidence."
                    ),
                },
                {
                    "section_id": "S03",
                    "title": "EATA scope and stereocontrol",
                    "section_role": "body",
                    "primary_papers": ["P002"],
                },
            ],
            ["P001", "P002"],
            classification_contract={
                "required_outline_partitions": [
                    "racemic ATA",
                    "enantioselective ATA (EATA)",
                ],
                "topic_comparison_dimensions": ["Cu", "Zn", "Cd", "Ti"],
                "topic_outcome_dimensions": [
                    "monosubstituted allenes",
                    "trisubstituted allenes",
                ],
                "catch_all_sections_allowed": False,
            },
        )

        self.assertEqual("aligned", report["classification_contract_status"])
        self.assertEqual([], report["missing_topic_partitions"])
        self.assertIn("S03", report["topic_partition_trace"]["enantioselective ATA (EATA)"])
        self.assertEqual(["Cu", "Zn", "Cd", "Ti"], report["topic_comparison_dimensions"])
        self.assertNotIn(
            "taxonomy.required_topic_partitions_missing",
            {item["rule_id"] for item in report["issues"]},
        )

    def test_taxonomy_contract_uses_declared_partition_aliases(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S02",
                    "title": "Controlled comparisons",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                    "secondary_axis_routes": {
                        "randomized trials": ["P001"],
                    },
                }
            ],
            ["P001"],
            classification_contract={
                "required_outline_partitions": ["randomized evidence"],
                "classification_axes": [
                    {
                        "axis_id": "study_design",
                        "axis_role": "required_independent_discussion",
                        "partitions": [
                            {
                                "label": "randomized evidence",
                                "aliases": ["randomized trials"],
                            }
                        ],
                    }
                ],
            },
        )

        self.assertEqual("aligned", report["classification_contract_status"])
        self.assertEqual([], report["missing_topic_partitions"])
        self.assertEqual(
            ["S02"], report["topic_partition_trace"]["randomized evidence"]
        )

    def test_boundary_section_with_explicit_rationale_is_not_contract_drift(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Defined primary category",
                    "section_role": "body",
                    "primary_papers": ["P001", "P002"],
                },
                {
                    "section_id": "S02",
                    "title": "Cross-category evidence and boundary cases",
                    "section_role": "body",
                    "primary_papers": ["P003"],
                    "boundary_rationale": (
                        "The source evidence spans both declared categories and is used "
                        "only for their comparison."
                    ),
                },
            ],
            ["P001", "P002", "P003"],
            classification_contract={"catch_all_sections_allowed": False},
        )

        self.assertEqual("aligned", report["classification_contract_status"])
        self.assertEqual(["S02"], report["boundary_rationale_section_ids"])
        self.assertEqual([], report["unresolved_boundary_section_ids"])
        self.assertNotIn(
            "taxonomy.boundary_section_outside_contract",
            {item["rule_id"] for item in report["issues"]},
        )

    def test_taxonomy_contract_accepts_explicit_paper_exclusion_reason(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Introduction",
                    "section_role": "introduction",
                    "excluded_papers": [
                        {
                            "paper_id": "P002",
                            "reason": "The source-confirmed transformation is outside the declared review scope.",
                        }
                    ],
                },
                {
                    "section_id": "S02",
                    "title": "Defined primary category",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                },
                {
                    "section_id": "S03",
                    "title": "Conclusion",
                    "section_role": "conclusion",
                },
            ],
            ["P001", "P002"],
        )

        self.assertTrue(report["can_confirm"])
        self.assertEqual([], report["orphan_paper_ids"])
        self.assertEqual(["P002"], report["excluded_paper_ids"])
        self.assertEqual(1, report["excluded_paper_count"])

    def test_taxonomy_contract_rejects_paper_exclusion_without_reason(self) -> None:
        report = taxonomy_diagnostics(
            [
                {
                    "section_id": "S01",
                    "title": "Defined primary category",
                    "section_role": "body",
                    "primary_papers": ["P001"],
                    "excluded_papers": [{"paper_id": "P002", "reason": ""}],
                }
            ],
            ["P001", "P002"],
        )

        self.assertFalse(report["can_confirm"])
        self.assertIn("P002", report["orphan_paper_ids"])
        self.assertIn(
            "taxonomy.paper_exclusion_reason_missing",
            {item["rule_id"] for item in report["issues"]},
        )

    def test_mechanism_evidence_signature_distinguishes_support_types(self) -> None:
        signature = mechanism_evidence_types(
            "A kinetic isotope effect (KIE) and DFT calculations were reported; "
            "a plausible mechanism was proposed by the authors."
        )

        self.assertIn("isotope_label_or_kie", signature)
        self.assertIn("computational_chemistry", signature)
        self.assertIn("author_proposed_mechanism_only", signature)


if __name__ == "__main__":
    unittest.main()
