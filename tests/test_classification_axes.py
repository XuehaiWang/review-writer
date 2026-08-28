from __future__ import annotations

import unittest

from review_writer_core.classification_axes import (
    CLASSIFICATION_CONTRACT_VERSION,
    axis_is_stereochemical_regime,
    canonical_classification_contract,
    normalize_classification_axis_semantics,
)


class ClassificationAxisTests(unittest.TestCase):
    def test_racemic_and_enantioselective_partitions_are_not_reaction_type(self) -> None:
        repaired = normalize_classification_axis_semantics(
            {
                "axis_id": "reaction_type",
                "label": "Reaction type",
                "axis_role": "required_independent_discussion",
                "partitions": [
                    {"partition_id": "racemic", "label": "Racemic ATA"},
                    {
                        "partition_id": "eata",
                        "label": "Enantioselective ATA (EATA)",
                    },
                ],
            }
        )

        self.assertEqual("stereochemical_regime", repaired["axis_id"])
        self.assertEqual(
            "required_independent_discussion", repaired["axis_role"]
        )
        self.assertIn(
            "missing ee or er",
            repaired["partitions"][0]["negative_or_ambiguous_signals"],
        )

    def test_single_chiral_partition_does_not_relabel_a_reaction_axis(self) -> None:
        axis = {
            "axis_id": "reaction_type",
            "label": "Reaction type",
            "partitions": [
                {"partition_id": "coupling", "label": "Chiral cross-coupling"},
                {"partition_id": "addition", "label": "Hydrofunctionalization"},
            ],
        }

        self.assertFalse(axis_is_stereochemical_regime(axis))
        self.assertEqual(
            "reaction_type", normalize_classification_axis_semantics(axis)["axis_id"]
        )

    def test_canonical_contract_splits_repaired_cross_cutting_axis(self) -> None:
        contract = canonical_classification_contract(
            [
                {
                    "axis_id": "reaction_type",
                    "label": "Reaction type",
                    "source_type": "explicit_topic",
                    "axis_role": "primary_organization",
                    "source_surface": "organize by reaction type and compare racemic and asymmetric routes",
                    "partitions": [
                        {"partition_id": "racemic", "label": "Racemic routes"},
                        {
                            "partition_id": "asymmetric",
                            "label": "Enantioselective routes",
                        },
                    ],
                }
            ],
            primary_axis_hint="reaction_type",
            source="test",
        )

        self.assertEqual(CLASSIFICATION_CONTRACT_VERSION, contract["contract_version"])
        self.assertEqual("reaction_type", contract["primary_axis_id"])
        self.assertEqual(
            ["reaction_type", "stereochemical_regime"],
            contract["required_route_axis_ids"],
        )
        self.assertEqual(
            "required_independent_discussion", contract["axes"][1]["axis_role"]
        )
        self.assertEqual(64, len(contract["fingerprint"]))

    def test_runtime_coverage_does_not_change_contract_fingerprint(self) -> None:
        axis = {
            "axis_id": "method",
            "label": "Method",
            "axis_role": "primary_organization",
            "partitions": [{"partition_id": "a", "label": "A"}],
        }
        first = canonical_classification_contract([axis])
        second = canonical_classification_contract(
            [{**axis, "evidence_coverage": {"paper_count": 5}, "role_status": "evidence_confirmed"}]
        )

        self.assertEqual(first["fingerprint"], second["fingerprint"])


if __name__ == "__main__":
    unittest.main()
