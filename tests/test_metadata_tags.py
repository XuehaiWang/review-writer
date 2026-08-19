from __future__ import annotations

import unittest

from review_writer_core.metadata_tags import (
    neutral_structured_tag_values,
    structured_tags_are_verified,
    verified_structured_tags,
)


class MetadataTagTrustTests(unittest.TestCase):
    def test_unverified_legacy_tags_are_retained_but_not_returned(self) -> None:
        metadata = {
            "structured_tags": {
                "value": {
                    "catalyst_or_method": "copper catalysis",
                    "reaction_type": "allenation",
                },
                "source": "active_taxonomy_keyword_inference",
                "human_checked": False,
            }
        }

        self.assertFalse(structured_tags_are_verified(metadata))
        self.assertEqual({}, verified_structured_tags(metadata))
        self.assertEqual(
            "copper catalysis",
            metadata["structured_tags"]["value"]["catalyst_or_method"],
        )

    def test_human_verified_tags_are_available_to_retrieval_and_planning(self) -> None:
        metadata = {
            "structured_tags": {
                "value": {
                    "catalyst_or_method": "gold catalysis",
                    "reaction_type": "not specified",
                },
                "human_checked": True,
            }
        }

        self.assertTrue(structured_tags_are_verified(metadata))
        self.assertEqual(
            {"catalyst_or_method": "gold catalysis"},
            verified_structured_tags(metadata),
        )

    def test_project_neutral_shape_contains_no_inferred_values(self) -> None:
        self.assertEqual(
            {"not specified"},
            set(neutral_structured_tag_values().values()),
        )


if __name__ == "__main__":
    unittest.main()
