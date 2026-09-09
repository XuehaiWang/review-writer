from __future__ import annotations

import unittest
from pathlib import Path

from review_writer_core.taxonomy import (
    DEFAULT_TAXONOMY_PROFILE,
    TaxonomyConfigurationError,
    effective_taxonomy_profile,
    load_taxonomy_rules,
    suggest_taxonomy_profile,
    taxonomy_identity,
    taxonomy_profile_catalog,
    validate_selectable_taxonomy_profile,
    validate_taxonomy_profile,
)


ROOT = Path(__file__).resolve().parents[1]


class TaxonomyProfileTests(unittest.TestCase):
    def test_general_academic_is_the_no_domain_rules_default(self) -> None:
        self.assertEqual("general_academic", DEFAULT_TAXONOMY_PROFILE)
        self.assertEqual(
            "general_academic",
            suggest_taxonomy_profile("Graph neural networks for drug discovery"),
        )
        self.assertEqual([], load_taxonomy_rules(ROOT, profile="general_academic"))
        identity = taxonomy_identity(ROOT, profile="general_academic")
        self.assertEqual("general_academic", identity["profile"])
        self.assertIs(False, identity["domain_rules_enabled"])

    def test_public_catalog_hides_topic_specific_profiles(self) -> None:
        profiles = {item["id"]: item for item in taxonomy_profile_catalog()}
        self.assertEqual({"general_academic", "chemistry_general"}, set(profiles))
        self.assertIs(True, profiles["chemistry_general"]["domain_rules_enabled"])

    def test_topic_specific_profile_remains_internal_for_compatibility(self) -> None:
        self.assertEqual("allene", validate_taxonomy_profile("allene"))
        with self.assertRaises(TaxonomyConfigurationError):
            validate_selectable_taxonomy_profile("allene")
        self.assertEqual(
            "allene", suggest_taxonomy_profile("Axially chiral allene synthesis")
        )

    def test_broad_related_terms_do_not_activate_allene_profile(self) -> None:
        for topic in (
            "Propargylic substitution methods",
            "Axial chirality in biaryls",
            "SN2' reactions in organic synthesis",
        ):
            with self.subTest(topic=topic):
                self.assertNotEqual("allene", suggest_taxonomy_profile(topic))

    def test_general_chemistry_activates_internal_topic_rules(self) -> None:
        general = load_taxonomy_rules(ROOT, profile="chemistry_general")
        specialized = load_taxonomy_rules(
            ROOT,
            profile="chemistry_general",
            topic_text="Axially chiral allene synthesis from 3-alkynoates",
        )
        self.assertNotIn("alkynoates", {label for label, _, _ in general})
        self.assertIn("alkynoates", {label for label, _, _ in specialized})
        identity = taxonomy_identity(
            ROOT,
            profile="chemistry_general",
            topic_text="联烯合成方法",
        )
        self.assertEqual(
            ["chemistry_general", "allene"], identity["effective_profiles"]
        )
        self.assertEqual(2, len(identity["rules"]))
        self.assertEqual(
            "allene",
            effective_taxonomy_profile(
                "chemistry_general", "Axially chiral allene synthesis"
            ),
        )

    def test_internal_specialist_profile_keeps_general_chemistry_base(self) -> None:
        labels = {
            label for label, _category, _aliases in load_taxonomy_rules(ROOT, profile="allene")
        }
        self.assertIn("heterocycles", labels)
        self.assertIn("alkynoates", labels)


if __name__ == "__main__":
    unittest.main()
