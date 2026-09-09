from __future__ import annotations

import unittest

from review_writer_core.stages.figures.overview_structure import (
    contract_smiles,
    derive_overview_structure_contract,
    resolve_contract_chemical_name,
    taxonomy_profile_has_structure_registry,
    taxonomy_requires_overview_structure,
)


ATA_TOPIC = (
    "Please write a review on the topic allenation-of-terminal-alkynes (ATA), "
    "focusing on terminal alkyne allenation with different substrates to access "
    "mono-, 1,3-di-, and trisubstituted allenes."
)


class OverviewStructureContractTests(unittest.TestCase):
    def test_structure_policy_is_loaded_from_taxonomy_resources(self) -> None:
        self.assertTrue(taxonomy_profile_has_structure_registry("allene"))
        self.assertTrue(taxonomy_requires_overview_structure("allene"))
        self.assertTrue(taxonomy_profile_has_structure_registry("chemistry_general"))
        self.assertFalse(taxonomy_requires_overview_structure("chemistry_general"))
        self.assertFalse(taxonomy_profile_has_structure_registry("general_academic"))

    def test_synthesis_topic_prefers_product_over_substrate(self) -> None:
        contract = derive_overview_structure_contract(
            ATA_TOPIC,
            query_plan={
                "keywords": [
                    {"keyword": "terminal alkynes", "category": "substrate"},
                    {"keyword": "substituted allenes", "category": "product"},
                ]
            },
            taxonomy_profile="allene",
        )

        self.assertEqual("resolved", contract["status"])
        self.assertEqual("target_product", contract["role"])
        self.assertEqual("allene", contract["motif"])
        self.assertEqual("*C=C=C*", contract_smiles(contract))
        self.assertNotIn("#", contract_smiles(contract))

    def test_general_chemistry_uses_specialized_structure_only_on_strong_topic_signal(self) -> None:
        allene_contract = derive_overview_structure_contract(
            "Allenation methods for substituted products",
            taxonomy_profile="chemistry_general",
        )
        broad_contract = derive_overview_structure_contract(
            "Axial chirality in biaryl compounds",
            taxonomy_profile="chemistry_general",
        )

        self.assertEqual("allene", allene_contract["effective_taxonomy_profile"])
        self.assertEqual("allene", allene_contract["motif"])
        self.assertEqual("chemistry_general", broad_contract["effective_taxonomy_profile"])
        self.assertNotEqual("allene", broad_contract.get("motif"))

    def test_sparse_ata_topic_recovers_product_from_matrix_facts(self) -> None:
        contract = derive_overview_structure_contract(
            "A review of ATA reactions",
            matrix={
                "rows": [
                    {
                        "structured_tags": {
                            "substrate": {"value": "terminal alkyne"},
                            "product": {"value": "1,3-disubstituted allene"},
                        }
                    }
                ]
            },
            taxonomy_profile="allene",
        )

        self.assertEqual("target_product", contract["role"])
        self.assertEqual("allene", contract["motif"])
        self.assertIn("matrix_product_facts", contract["evidence_sources"])

    def test_synthesis_target_never_falls_back_to_substrate(self) -> None:
        contract = derive_overview_structure_contract(
            "Synthesis from terminal alkynes under catalytic conditions",
            query_plan={
                "keywords": [
                    {"keyword": "terminal alkynes", "category": "substrate"}
                ]
            },
            taxonomy_profile="chemistry_general",
        )

        self.assertEqual("unresolved", contract["status"])
        self.assertEqual("target_product", contract["role"])
        self.assertEqual("", contract_smiles(contract))

    def test_non_synthesis_topic_can_use_primary_subject(self) -> None:
        contract = derive_overview_structure_contract(
            "Reactivity and functionalization of terminal alkynes",
            taxonomy_profile="chemistry_general",
        )

        self.assertEqual("resolved", contract["status"])
        self.assertEqual("primary_subject", contract["role"])
        self.assertEqual("alkyne", contract["motif"])
        self.assertEqual("*C#C*", contract_smiles(contract))

    def test_non_chemical_topic_omits_structure(self) -> None:
        contract = derive_overview_structure_contract(
            "Governance models for public research infrastructure",
            taxonomy_profile="general_academic",
        )

        self.assertEqual("not_applicable", contract["status"])
        self.assertEqual("none", contract["role"])
        self.assertEqual("", contract_smiles(contract))

    def test_explicit_matrix_product_structure_supports_unregistered_chemistry(self) -> None:
        contract = derive_overview_structure_contract(
            "Synthesis of benzoxazoles from aminophenols",
            matrix={
                "rows": [
                    {
                        "structured_tags": {
                            "product": {
                                "value": "benzoxazole",
                                "smiles": "c1ccc2ocnc2c1",
                            }
                        }
                    }
                ]
            },
            taxonomy_profile="chemistry_general",
        )

        self.assertEqual("resolved", contract["status"])
        self.assertEqual("target_product", contract["role"])
        self.assertEqual("explicit_structure", contract["motif"])
        self.assertEqual("c1ccc2ocnc2c1", contract_smiles(contract))
        self.assertIn("matrix_product_structure", contract["evidence_sources"])

    def test_unregistered_product_can_use_bounded_name_resolver(self) -> None:
        contract = derive_overview_structure_contract(
            "Synthesis of benzoxazoles from aminophenols",
            taxonomy_profile="chemistry_general",
        )
        calls: list[str] = []

        def resolver(name: str, **_: object) -> dict[str, str]:
            calls.append(name)
            return {
                "resolver": "test_resolver",
                "resolved_name": "benzoxazole",
                "smiles": "c1ccc2ocnc2c1",
                "source_url": "https://example.invalid/benzoxazole",
            }

        resolved = resolve_contract_chemical_name(contract, resolver=resolver)

        self.assertEqual("unresolved", contract["status"])
        self.assertEqual(["benzoxazoles"], contract["candidate_names"])
        self.assertEqual(["benzoxazoles"], calls)
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual("resolved_chemical_name", resolved["motif"])
        self.assertEqual("c1ccc2ocnc2c1", contract_smiles(resolved))
        self.assertEqual("test_resolver", resolved["resolution"]["resolver"])

    def test_invalid_name_resolution_is_safely_omitted(self) -> None:
        contract = derive_overview_structure_contract(
            "Synthesis of benzoxazoles from aminophenols",
            taxonomy_profile="chemistry_general",
        )
        resolved = resolve_contract_chemical_name(
            contract,
            resolver=lambda *_args, **_kwargs: {
                "resolved_name": "benzoxazole",
                "smiles": "not valid smiles",
            },
        )

        self.assertEqual("unresolved", resolved["status"])
        self.assertEqual("unavailable", resolved["name_resolution_status"])
        self.assertEqual("", contract_smiles(resolved))


if __name__ == "__main__":
    unittest.main()
