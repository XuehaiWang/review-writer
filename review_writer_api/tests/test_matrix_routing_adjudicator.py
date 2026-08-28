from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "review-literature-matrix-outline"
    / "scripts"
    / "enrich_matrix_facts.py"
)
SPEC = importlib.util.spec_from_file_location("matrix_fact_enrichment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MatrixRoutingAdjudicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paper = {
            "paper_id": "P001",
            "title": "Carbohydrate-tethered axially chiral allenes",
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:route",
                    "chunk_id": "chunk-1",
                    "page_start": 2,
                    "page_end": 2,
                    "section_path": ["Results"],
                    "content_type": "body",
                    "content": (
                        "CuBr2 catalyzes the conversion of terminal-alkyne-bearing "
                        "carbohydrates and aldehydes to 1,3-disubstituted allenes."
                    ),
                    "source_lineage_hash": "lineage-1",
                }
            ],
            "partition_evidence_candidates": [],
        }
        self.categories = [
            {
                "label": "aldehyde-based three-component ATA",
                "aliases": ["terminal alkynes and aldehydes"],
            }
        ]

    def test_accepts_supported_allowed_category(self) -> None:
        result = MODULE.normalize_routing_recommendation(
            self.paper,
            {
                "routing_recommendation": {
                    "status": "classified",
                    "label": "aldehyde-based three-component ATA",
                    "confidence": 0.96,
                    "evidence_key": "sha256:route",
                    "support_excerpt": (
                        "terminal-alkyne-bearing carbohydrates and aldehydes "
                        "to 1,3-disubstituted allenes"
                    ),
                    "rationale": "The reported inputs define the allowed category.",
                    "evidence_ceiling": "No detailed mechanism is established.",
                }
            },
            "reaction_type",
            self.categories,
        )

        self.assertEqual("classified", result["status"])
        self.assertEqual("reaction_type", result["axis_id"])
        self.assertEqual("aldehyde-based three-component ATA", result["label"])
        self.assertEqual("sha256:route", result["evidence_refs"][0]["evidence_key"])

    def test_rejects_label_without_exact_source_excerpt(self) -> None:
        result = MODULE.normalize_routing_recommendation(
            self.paper,
            {
                "routing_recommendation": {
                    "status": "classified",
                    "label": "aldehyde-based three-component ATA",
                    "confidence": 0.99,
                    "evidence_key": "sha256:route",
                    "support_excerpt": "A mechanism that is not present in the source.",
                }
            },
            "reaction_type",
            self.categories,
        )

        self.assertEqual("insufficient_evidence", result["status"])
        self.assertEqual([], result["evidence_refs"])


if __name__ == "__main__":
    unittest.main()
