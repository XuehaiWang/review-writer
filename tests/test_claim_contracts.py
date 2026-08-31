import unittest

from review_writer_core.claim_contracts import (
    claim_support_coverage,
    derive_section_readiness,
    normalize_section_claim_contract,
)


class ClaimContractTests(unittest.TestCase):
    def test_legacy_authoring_operation_is_not_a_scientific_claim(self) -> None:
        contract = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "primary_papers": ["P001", "P002"],
                "review_claims": [
                    {
                        "claim": "Develop claim-centered synthesis from two primary papers.",
                        "legacy_role": "writing_requirement",
                    }
                ],
            }
        )

        self.assertEqual([], contract["scientific_claims"])
        self.assertEqual(1, len(contract["writing_requirements"]))

    def test_structured_source_testable_legacy_claim_is_preserved(self) -> None:
        contract = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "primary_papers": ["P001"],
                "review_claims": [
                    {
                        "claim_id": "legacy-claim",
                        "proposition": "The reported catalyst changes product selectivity.",
                        "claim_type": "comparison",
                        "supporting_papers": [{"paper_id": "P001"}],
                    }
                ],
            }
        )

        self.assertEqual("legacy-claim", contract["scientific_claims"][0]["claim_id"])
        self.assertEqual(["P001"], contract["scientific_claims"][0]["primary_papers"])

    def test_explicit_fact_and_evidence_binding_survives_normalization(self) -> None:
        claim = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "scientific_claims": [
                    {
                        "claim_id": "S02-SC01",
                        "proposition": "The reported reaction gave 91% yield.",
                        "primary_papers": ["P001"],
                        "fact_ids": ["MF-001"],
                        "evidence_refs": [{"evidence_key": "sha256:abc"}],
                        "allowed_assertion": "91% yield under the reported conditions",
                        "assertion_ceiling": "direct_source_report",
                        "coverage": {"subject": True, "value": True},
                    }
                ],
            }
        )["scientific_claims"][0]

        self.assertEqual(["MF-001"], claim["fact_ids"])
        self.assertEqual("sha256:abc", claim["evidence_refs"][0]["evidence_key"])
        self.assertEqual("direct_source_report", claim["assertion_ceiling"])

    def test_claim_coverage_rejects_wrong_value_and_paper_identity(self) -> None:
        coverage = claim_support_coverage(
            {
                "proposition": "The reaction gave 97% yield.",
                "paper_ids": ["P001"],
                "fact_ids": ["MF-001"],
                "evidence_refs": [{"evidence_key": "sha256:abc"}],
            },
            evidence_texts=["The reaction gave 81% yield."],
            available_fact_ids=["MF-001"],
            evidence_paper_ids=["P002"],
        )

        self.assertEqual("partially_supported", coverage["support_status"])
        self.assertIn("value", coverage["failed_coverage_fields"])
        self.assertIn("paper_identity", coverage["failed_coverage_fields"])

    def test_legacy_imperative_with_paper_refs_remains_authoring_only(self) -> None:
        contract = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "review_claims": [
                    {
                        "claim": "Compare the assigned studies on explicit evidence axes.",
                        "claim_type": "contrast",
                        "supporting_papers": [{"paper_id": "P001"}],
                    }
                ],
            }
        )

        self.assertEqual([], contract["scientific_claims"])
        self.assertEqual(1, len(contract["writing_requirements"]))

    def test_explicit_contract_wins_over_legacy_review_claims(self) -> None:
        contract = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "scientific_claims": [],
                "writing_requirements": [
                    {"instruction": "Compare only source-addressable fields."}
                ],
                "review_claims": [
                    {"proposition": "This legacy proposition must be ignored."}
                ],
            }
        )

        self.assertEqual([], contract["scientific_claims"])
        self.assertEqual(1, len(contract["writing_requirements"]))

    def test_section_readiness_is_derived_with_scientific_precedence(self) -> None:
        report = derive_section_readiness(
            generation_mode="standard",
            required_claim_states=[
                {
                    "claim_id": "S02-SC01",
                    "required_for_section": True,
                    "status": "evidence_missing",
                }
            ],
            structure_gaps=["comparison_missing"],
            depth_sufficient=False,
        )
        self.assertEqual("needs_evidence_repair", report["status"])
        self.assertTrue(report["derived"])

        fallback = derive_section_readiness(
            generation_mode="safe_evidence_fallback",
            required_claim_states=[],
            structure_gaps=[],
            depth_sufficient=True,
        )
        self.assertEqual("provider_fallback", fallback["status"])


if __name__ == "__main__":
    unittest.main()
