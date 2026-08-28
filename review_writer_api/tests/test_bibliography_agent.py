from __future__ import annotations

import unittest

from review_writer_core.bibliography_agent import (
    bibliography_agent_prompt,
    bounded_bibliography_regions,
    validate_bibliography_agent_result,
)
from review_writer_core.bibliography_audit import (
    apply_bibliography_updates,
    audit_bibliography,
)
from review_writer_core.paper_sources.base import PaperSourceConnector, SourceSearchResult


class _Connector(PaperSourceConnector):
    name = "crossref"

    def search(self, request):
        return SourceSearchResult(
            source=self.name,
            status="completed",
            candidates=[
                {
                    "title": "Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol",
                    "authors": ["Juntao Ye", "Shengming Ma"],
                    "year": 2014,
                    "bibliographic_year": 2014,
                    "journal": "Organic Syntheses",
                    "identifiers": {"doi": "10.15227/orgsyn.091.0233"},
                }
            ],
        )


class BibliographyAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.markdown = (
            "## Working with Hazardous Chemicals\n\n"
            + ("Safety boilerplate. " * 900)
            + "\n\n## Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol\n\n"
            "Juntao Ye and Shengming Ma\\*<sup>1</sup>\n\n"
            "State Key Laboratory of Organometallic Chemistry\n\n"
            "Checked by Mingyao Wu and Dawei Ma\n\n"
            "## Procedure\n\nReaction details.\n"
        )
        self.metadata = {
            "title": {
                "value": "Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol",
                "confidence": 0.86,
            },
            "authors": {"value": [], "confidence": 0.0},
            "journal": {"value": None, "confidence": 0.0},
            "year": {"value": None, "confidence": 0.0},
            "doi": {"value": None, "confidence": 0.0},
        }

    def test_regions_reach_title_after_long_boilerplate_without_unbounded_body(self) -> None:
        regions = bounded_bibliography_regions(
            self.markdown,
            title=self.metadata["title"]["value"],
        )

        combined = "\n".join(item["text"] for item in regions)
        self.assertIn("Juntao Ye and Shengming Ma", combined)
        self.assertLessEqual(sum(len(item["text"]) for item in regions), 24_000)
        self.assertIn("UNTRUSTED", bibliography_agent_prompt(self.metadata, regions))

    def test_validator_accepts_authors_and_rejects_checked_by_as_authors(self) -> None:
        regions = bounded_bibliography_regions(
            self.markdown,
            title=self.metadata["title"]["value"],
        )
        location = next(
            item["source_location"]
            for item in regions
            if "Juntao Ye and Shengming Ma" in item["text"]
        )
        payload = {
            "fields": {
                "authors": {
                    "value": ["Juntao Ye", "Shengming Ma"],
                    "role": "article_authors",
                    "source_excerpt": "Juntao Ye and Shengming Ma\\*<sup>1</sup>",
                    "source_location": location,
                    "confidence": 0.97,
                }
            }
        }
        accepted = validate_bibliography_agent_result(payload, regions)
        self.assertEqual(
            ["Juntao Ye", "Shengming Ma"], accepted["fields"]["authors"]["value"]
        )

        payload["fields"]["authors"] = {
            "value": ["Mingyao Wu", "Dawei Ma"],
            "role": "article_authors",
            "source_excerpt": "Checked by Mingyao Wu and Dawei Ma",
            "source_location": location,
            "confidence": 0.99,
        }
        rejected = validate_bibliography_agent_result(payload, regions)
        self.assertNotIn("authors", rejected["fields"])

    def test_agent_identity_hint_enables_provider_match_and_canonical_updates(self) -> None:
        extraction = {
            "status": "reliable",
            "fields": {
                "authors": {
                    "value": ["Juntao Ye", "Shengming Ma"],
                    "role": "article_authors",
                    "source_excerpt": "Juntao Ye and Shengming Ma",
                    "source_location": "mineru_title_neighborhood",
                    "confidence": 0.97,
                    "verification_status": "verified",
                }
            },
        }

        audit = audit_bibliography(
            self.metadata,
            connectors=[_Connector()],
            document_agent_extraction=extraction,
        )
        updated, changed = apply_bibliography_updates(self.metadata, audit)

        self.assertEqual("verified", audit["status"])
        self.assertIn("authors", changed)
        self.assertEqual(["Juntao Ye", "Shengming Ma"], updated["authors"]["value"])
        self.assertEqual("10.15227/orgsyn.091.0233", updated["doi"]["value"])
        self.assertEqual("Organic Syntheses", updated["journal"]["value"])
        self.assertEqual(2014, updated["year"]["value"])


if __name__ == "__main__":
    unittest.main()
