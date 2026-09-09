import unittest

from review_writer_core.claim_contracts import (
    build_fact_grounded_claims,
    claim_is_executable,
    claim_support_coverage,
    derive_section_readiness,
    normalize_section_claim_contract,
    resolve_claim_with_fact,
    scientific_claim_evidence_state,
)


class ClaimContractTests(unittest.TestCase):
    @staticmethod
    def audited_fact(field_id="quantitative_results", fact_id="MF-001"):
        return {
            "fact_id": fact_id,
            "paper_id": "P001",
            "field_id": field_id,
            "value": "The reported reaction gave 91% yield.",
            "subject": "the reported reaction",
            "predicate": "gave 91% yield",
            "qualifiers": {"conditions": "reported conditions"},
            "evidence_refs": [{"evidence_key": "sha256:abc"}],
            "support_level": "direct",
            "assertion_ceiling": "direct_source_report",
            "evidence_ceiling": "Use only the reported experiment.",
            "epistemic_status": "direct_source_report",
            "confidence": 0.95,
            "validation_contract": "fact-support/3",
            "verification": {
                "contract": "fact-support/3",
                "status": "supported",
                "reason": "The relation matches the source.",
            },
        }

    def test_fact_projection_does_not_invent_missing_slots(self):
        claims = build_fact_grounded_claims(
            section_id="S02",
            section_title="Catalyst comparison",
            primary_papers=["P001"],
            required_fact_roles=["quantitative_results", "mechanism"],
            rows_by_id={
                "P001": {"scientific_facts": [self.audited_fact()]}
            },
        )

        self.assertEqual(["S02-SC-MF-001"], [c["claim_id"] for c in claims])
        self.assertTrue(claim_is_executable(claims[0]))
        self.assertFalse(claims[0]["required_for_section"])

    def test_targeted_fact_repair_keeps_blueprint_claim_identity(self):
        placeholder = {"claim_id": "legacy-mechanism-gap", "primary_papers": ["P001"],
                       "required_fact_roles": ["mechanism"], "required_for_section": True}
        fact = self.audited_fact(field_id="mechanism", fact_id="MF-MECH")

        resolved = resolve_claim_with_fact(placeholder, fact)

        self.assertIsNotNone(resolved)
        self.assertEqual(placeholder["claim_id"], resolved["claim_id"])
        self.assertEqual(["MF-MECH"], resolved["fact_ids"])
        self.assertTrue(claim_is_executable(resolved))

    def test_fact_projection_leaves_cross_study_argument_to_the_planner(self):
        second = {
            **self.audited_fact(fact_id="MF-002"),
            "paper_id": "P002",
            "value": "The second study reported 84% yield.",
            "evidence_refs": [{"evidence_key": "sha256:def"}],
        }
        claims = build_fact_grounded_claims(
            section_id="S02",
            section_title="Catalyst comparison",
            primary_papers=["P001", "P002"],
            required_fact_roles=["quantitative_results"],
            rows_by_id={
                "P001": {"scientific_facts": [self.audited_fact()]},
                "P002": {"scientific_facts": [second]},
            },
        )

        self.assertEqual(2, len(claims))
        self.assertEqual(["MF-001", "MF-002"], [c["fact_ids"][0] for c in claims])
        self.assertTrue(all(c["claim_type"] == "reported_result" for c in claims))
        self.assertTrue(all(claim_is_executable(c) for c in claims))

    def test_claim_evidence_state_requires_exact_registered_fact_binding(self):
        claim = build_fact_grounded_claims(
            section_id="S02",
            section_title="Results",
            primary_papers=["P001"],
            required_fact_roles=["quantitative_results"],
            rows_by_id={
                "P001": {"scientific_facts": [self.audited_fact()]}
            },
        )[0]
        missing = scientific_claim_evidence_state(
            claim,
            [{"paper_id": "P001", "evidence_key": "sha256:abc"}],
        )
        supported = scientific_claim_evidence_state(
            claim,
            [
                {
                    "paper_id": "P001",
                    "evidence_key": "sha256:abc",
                    "fact_bindings": [self.audited_fact()],
                }
            ],
        )

        self.assertEqual("partially_supported", missing["status"])
        self.assertEqual(["MF-001"], missing["missing_fact_ids"])
        self.assertEqual("evidence_supported", supported["status"])

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
                "primary_papers": ["P001", "P002"],
                "scientific_claims": [
                    {
                        "claim_id": "S02-SC01",
                        "proposition": "The reported reaction gave 91% yield.",
                        "primary_papers": ["P001"],
                        "fact_ids": ["MF-001"],
                        "evidence_refs": [{"evidence_key": "sha256:abc"}],
                        "support_status": "supported",
                        "allowed_assertion": "91% yield under the reported conditions",
                        "assertion_ceiling": "direct_source_report",
                        "coverage": {"subject": True, "value": True},
                    }
                ],
            }
        )["scientific_claims"][0]

        self.assertEqual(["MF-001"], claim["fact_ids"])
        self.assertEqual(["P001"], claim["primary_papers"])
        self.assertEqual("sha256:abc", claim["evidence_refs"][0]["evidence_key"])
        self.assertEqual("direct_source_report", claim["assertion_ceiling"])
        state = scientific_claim_evidence_state(
            claim,
            [
                {
                    "paper_id": "P001",
                    "evidence_key": "sha256:abc",
                    "fact_bindings": [{"fact_id": "MF-001"}],
                }
            ],
        )
        self.assertEqual("evidence_supported", state["status"])
        self.assertEqual([], state["missing_paper_ids"])

    def test_claim_without_paper_identity_uses_section_papers_for_legacy_compatibility(self) -> None:
        claim = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "primary_papers": ["P001", "P002"],
                "scientific_claims": [
                    {
                        "claim_id": "legacy-claim",
                        "proposition": "The reported systems differ in yield.",
                    }
                ],
            }
        )["scientific_claims"][0]

        self.assertEqual(["P001", "P002"], claim["primary_papers"])

    def test_explicit_multi_paper_claim_keeps_its_own_scope(self) -> None:
        claim = normalize_section_claim_contract(
            {
                "section_id": "S02",
                "primary_papers": ["P001", "P002", "P003"],
                "scientific_claims": [
                    {
                        "claim_id": "comparison-claim",
                        "proposition": "P001 and P002 report different conditions.",
                        "claim_type": "cross_study_comparison",
                        "primary_papers": ["P001", "P002"],
                    }
                ],
            }
        )["scientific_claims"][0]

        self.assertEqual(["P001", "P002"], claim["primary_papers"])

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
