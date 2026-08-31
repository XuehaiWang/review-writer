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
        self.assertEqual("scientific-fact/2", fact["fact_schema_version"])
        self.assertEqual("reported_result", fact["fact_type"])
        self.assertEqual("sha256:abc", fact["source_span"]["evidence_key"])
        self.assertEqual(
            "baseline_plus_targeted_recheck",
            result["fact_extraction_profile"]["mode"],
        )

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

    def test_partition_candidate_can_supply_a_required_fact_role(self) -> None:
        paper = {
            **self.paper,
            "required_fact_roles": ["object_input", "method_conditions"],
            "partition_evidence_candidates": [
                {
                    "evidence_key": "sha256:partition-role",
                    "chunk_id": "chunk-role",
                    "page_start": 5,
                    "page_end": 5,
                    "section_path": ["Results"],
                    "content_type": "text",
                    "content": (
                        "Terminal alkynes and aldehydes were reacted with "
                        "0.4 mol catalyst at room temperature."
                    ),
                    "source_lineage_hash": "lineage",
                }
            ],
        }
        result = PIPELINE.normalize_result(
            paper,
            {
                "facts": [
                    {
                        "field_id": "object_input",
                        "value": "The reported inputs were terminal alkynes and aldehydes.",
                        "support_excerpt": "Terminal alkynes and aldehydes were reacted",
                        "evidence_key": "sha256:partition-role",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.94,
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual(1, len(result["facts"]))
        self.assertEqual("object_input", result["facts"][0]["field_id"])

    def test_chemical_locants_do_not_fail_the_numeric_guard(self) -> None:
        paper = {
            **self.paper,
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:locants",
                    "chunk_id": "chunk-locants",
                    "page_start": 6,
                    "page_end": 6,
                    "section_path": ["Scope"],
                    "content_type": "text",
                    "content": "The product was isolated in 45% yield.",
                    "source_lineage_hash": "lineage",
                    "question_ids": ["quantitative_results"],
                }
            ],
        }
        result = PIPELINE.normalize_result(
            paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "Buta-2,3-dien-1-ol was isolated in 45% yield.",
                        "support_excerpt": "The product was isolated in 45% yield.",
                        "evidence_key": "sha256:locants",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.92,
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual(1, len(result["facts"]))

    def test_tex_spacing_variants_still_match_the_exact_source(self) -> None:
        paper = {
            **self.paper,
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:tex",
                    "chunk_id": "chunk-tex",
                    "page_start": 7,
                    "page_end": 7,
                    "section_path": ["Conditions"],
                    "content_type": "text",
                    "content": r"The reaction was maintained at $25\,\mathrm { C }$ for 2 h.",
                    "source_lineage_hash": "lineage",
                    "question_ids": ["method_conditions"],
                }
            ],
        }
        result = PIPELINE.normalize_result(
            paper,
            {
                "facts": [
                    {
                        "field_id": "method_conditions",
                        "value": r"The reported conditions were $25\,\mathrm { C}$ for 2 h.",
                        "support_excerpt": r"maintained at $25\,\mathrm { C}$ for 2 h",
                        "evidence_key": "sha256:tex",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.93,
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual(1, len(result["facts"]))

    def test_failed_field_objects_are_normalized_without_stringifying(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [
                    {
                        "field_id": "scope",
                        "reason": "The bounded candidates contain no scope passage.",
                    }
                ],
            },
        )

        self.assertEqual(["scope"], result["failed_fields"])
        self.assertEqual("scope", result["failed_field_details"][0]["field_id"])
        self.assertIn("no scope passage", result["failed_field_details"][0]["reason"])

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

    def test_same_user_cache_is_revalidated_and_survives_provider_failure(self) -> None:
        cached_fact = {
            "fact_id": "MF-CACHED",
            "fact_schema_version": "scientific-fact/2",
            "field_id": "quantitative_results",
            "fact_type": "reported_result",
            "value": "The reported result was 91% yield and 96% ee.",
            "evidence_refs": [{"evidence_key": "sha256:abc"}],
        }
        paper = {
            **self.paper,
            "reused_fact_cache": {
                "facts": [cached_fact],
                "source_project_id": "project-old",
            },
        }

        merged = PIPELINE.merge_reused_fact_cache(
            {
                "paper_id": "P001",
                "status": "failed",
                "facts": [],
                "failed_fields": ["all"],
                "error": "provider unavailable",
            },
            paper,
            provider_failed=True,
        )

        self.assertEqual("partial", merged["status"])
        self.assertEqual("MF-CACHED", merged["facts"][0]["fact_id"])
        self.assertTrue(merged["fact_extraction_profile"]["cache_reused"])
        self.assertTrue(
            merged["fact_extraction_profile"]["provider_supplement_failed"]
        )

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
