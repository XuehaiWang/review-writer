from __future__ import annotations

import unittest
from pathlib import Path

from review_writer_core.taxonomy import (
    DEFAULT_TAXONOMY_PROFILE,
    load_taxonomy_rules,
    suggest_taxonomy_profile,
    taxonomy_identity,
    taxonomy_profile_catalog,
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

    def test_explicit_domain_profiles_remain_available(self) -> None:
        profiles = {item["id"]: item for item in taxonomy_profile_catalog()}
        self.assertIs(True, profiles["chemistry_general"]["domain_rules_enabled"])
        self.assertIs(True, profiles["allene"]["domain_rules_enabled"])
        self.assertEqual(
            "allene", suggest_taxonomy_profile("Axially chiral allene synthesis")
        )


if __name__ == "__main__":
    unittest.main()
