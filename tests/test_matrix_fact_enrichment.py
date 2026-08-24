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


if __name__ == "__main__":
    unittest.main()
