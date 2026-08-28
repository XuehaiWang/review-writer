from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-literature-matrix-outline"
    / "scripts"
    / "enrich_matrix_facts.py"
)
SPEC = importlib.util.spec_from_file_location("matrix_fact_enrichment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


class MatrixFactEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paper = {
            "paper_id": "P001",
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:abc",
                    "chunk_id": "chunk-1",
                    "page_start": 4,
                    "page_end": 4,
                    "section_path": ["Results"],
                    "content_type": "text",
                    "content": "The optimized protocol afforded the product in 91% yield and 96% ee.",
                    "source_lineage_hash": "lineage",
                    "question_ids": ["quantitative_results"],
                }
            ],
            "partition_evidence_candidates": [
                {
                    "evidence_key": "sha256:partition",
                    "chunk_id": "chunk-2",
                    "page_start": 2,
                    "page_end": 2,
                    "section_path": ["Abstract"],
                    "content_type": "text",
                    "content": "An enantioselective protocol furnished the allene in 96% ee.",
                    "source_lineage_hash": "lineage",
                }
            ],
        }

    def test_fact_requires_exact_source_excerpt_and_keeps_page_reference(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The reported optimized result was 91% yield and 96% ee.",
                        "support_excerpt": "afforded the product in 91% yield and 96% ee",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.93,
                        "evidence_ceiling": "Only the optimized experiment is supported.",
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(result["facts"]))
        fact = result["facts"][0]
        self.assertEqual(4, fact["evidence_refs"][0]["page_start"])
        self.assertEqual("sha256:abc", fact["evidence_refs"][0]["evidence_key"])
        self.assertTrue(fact["evidence_ceiling"])
        self.assertEqual("body", fact["source_channel"])
        self.assertEqual("direct", fact["support_level"])
        self.assertEqual("not_required", result["review_status"])

    def test_hallucinated_support_excerpt_is_rejected(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The result was quantitative.",
                        "support_excerpt": "The reaction gave a quantitative yield.",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 1,
                        "evidence_ceiling": "",
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual([], result["facts"])

    def test_topic_partition_classification_requires_bounded_source_evidence(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_partition_classification": {
                    "partition": "enantioselective evidence (ESE)",
                    "confidence": 0.94,
                    "evidence_key": "sha256:partition",
                    "support_excerpt": "enantioselective protocol furnished the allene in 96% ee",
                    "rationale": "The source explicitly reports an enantioselective outcome.",
                    "evidence_ceiling": "Only the reported experiment is classified.",
                },
            },
            ["racemic evidence", "enantioselective evidence (ESE)"],
        )

        classification = result["topic_partition_classification"]
        self.assertEqual("classified", classification["status"])
        self.assertEqual(
            "enantioselective evidence (ESE)", classification["partition"]
        )
        self.assertEqual(2, classification["evidence_refs"][0]["page_start"])

    def test_topic_partition_classification_does_not_infer_from_absence(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_partition_classification": {
                    "partition": "racemic evidence",
                    "confidence": 0.91,
                    "evidence_key": "sha256:partition",
                    "support_excerpt": "The paper does not mention an enantioselective protocol.",
                    "rationale": "No ee was found.",
                },
            },
            ["racemic evidence", "enantioselective evidence (ESE)"],
        )

        classification = result["topic_partition_classification"]
        self.assertEqual("insufficient_evidence", classification["status"])
        self.assertEqual("", classification["partition"])
        self.assertEqual([], classification["evidence_refs"])

    def test_numeric_fact_cannot_introduce_value_absent_from_quote(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The product was obtained in 99% yield.",
                        "support_excerpt": "afforded the product in 91% yield and 96% ee",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.95,
                    }
                ],
                "failed_fields": [],
            },
        )
        self.assertEqual([], result["facts"])

    def test_formal_tag_is_bound_to_classification_fact_and_evidence(self) -> None:
        axes = [
            {
                "axis_id": "method",
                "label": "Method family",
                "axis_role": "primary_organization",
                "partitions": [
                    {"partition_id": "enantio", "label": "Enantioselective method"}
                ],
            }
        ]
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_classification_assignments": [
                    {
                        "axis_id": "method",
                        "partition_id": "enantio",
                        "relation_to_paper": "primary_contribution",
                        "confidence": 0.94,
                        "evidence_key": "sha256:partition",
                        "support_excerpt": "An enantioselective protocol furnished the allene in 96% ee.",
                    }
                ],
                "classification_outcomes": [],
            },
            classification_axes=axes,
        )
        tag = result["evidence_backed_tags"]["method"][0]
        self.assertTrue(tag["fact_ids"])
        self.assertEqual("sha256:partition", tag["evidence_refs"][0]["evidence_key"])
        facts = {fact["fact_id"]: fact for fact in result["facts"]}
        self.assertEqual("topic_partition", facts[tag["fact_ids"][0]]["field_id"])

    def test_medium_confidence_fact_is_context_only_without_user_warning(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The optimized result was 91% yield and 96% ee.",
                        "support_excerpt": "afforded the product in 91% yield and 96% ee",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.68,
                        "evidence_ceiling": "Context only.",
                    }
                ],
                "failed_fields": ["optional_scope_detail"],
            },
        )

        self.assertEqual("context_only", result["facts"][0]["support_level"])
        self.assertEqual("auto_resolved", result["review_status"])
        self.assertFalse(result["automatic_resolution"]["user_action_required"])

    def test_formal_axis_tag_can_supply_duplicate_topic_partition_route(self) -> None:
        axes = [
            {
                "axis_id": "stereochemical_regime",
                "label": "Stereochemical regime",
                "axis_role": "required_independent_discussion",
                "partitions": [
                    {
                        "partition_id": "eata",
                        "label": "Enantioselective ATA",
                    }
                ],
            }
        ]
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_classification_assignments": [
                    {
                        "axis_id": "stereochemical_regime",
                        "partition_id": "eata",
                        "relation_to_paper": "primary_contribution",
                        "confidence": 0.94,
                        "evidence_key": "sha256:partition",
                        "support_excerpt": "An enantioselective protocol furnished the allene in 96% ee.",
                    }
                ],
            },
            ["racemic ATA", "enantioselective ATA (EATA)"],
            axes,
        )
        derived = PIPELINE.derive_topic_partition_from_formal_tags(
            result, ["racemic ATA", "enantioselective ATA (EATA)"]
        )

        self.assertEqual(
            "classified", derived["topic_partition_classification"]["status"]
        )
        self.assertEqual(
            "enantioselective ATA (EATA)",
            derived["topic_partition_classification"]["partition"],
        )


if __name__ == "__main__":
    unittest.main()
