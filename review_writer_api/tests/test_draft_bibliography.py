from __future__ import annotations

import unittest

from review_writer_core.draft_bibliography import (
    citation_entries_from_draft,
    repair_numbered_references,
)


class DraftBibliographyTests(unittest.TestCase):
    def test_recovers_identity_and_repairs_gapped_references(self) -> None:
        markdown = """# Review

First supported paragraph [3].

<!-- paragraph_id: S01-p1 -->

Second supported paragraph [8, 10].

<!-- paragraph_id: S02-p1 -->

## References

[3] Old reference.
[8] Old reference.
"""
        sections = {
            "sections": [
                {
                    "section_id": "S01",
                    "paragraphs": [
                        {"paragraph_id": "S01-p1", "cited_paper_ids": ["P001"]}
                    ],
                },
                {
                    "section_id": "S02",
                    "paragraphs": [
                        {
                            "paragraph_id": "S02-p1",
                            "cited_paper_ids": ["P002", "P003"],
                        }
                    ],
                },
            ]
        }
        matrix = {
            "rows": [
                {"paper_id": "P001", "title": "First", "year": 2020},
                {"paper_id": "P002", "title": "Second", "year": 2021},
                {"paper_id": "P003", "title": "Third", "year": 2022},
            ]
        }

        identity = citation_entries_from_draft(markdown, sections)
        repaired, report = repair_numbered_references(markdown, identity, matrix)

        self.assertEqual([], identity["unresolved_callouts"])
        self.assertTrue(report["changed"])
        self.assertIn("First supported paragraph. [1]", repaired)
        self.assertIn("Second supported paragraph. [2, 3]", repaired)
        self.assertIn("[1] First. 2020", repaired)
        self.assertIn("[2] Second. 2021", repaired)
        self.assertIn("[3] Third. 2022", repaired)
        self.assertNotIn("[8] Old reference", repaired)

    def test_unknown_callout_is_not_guessed(self) -> None:
        markdown = """# Review

Paragraph with an unknown source [9].

<!-- paragraph_id: S01-p1 -->

## References

[9] Unknown.
"""
        identity = citation_entries_from_draft(
            markdown,
            {"sections": [{"section_id": "S01", "paragraphs": []}]},
        )
        repaired, report = repair_numbered_references(
            markdown,
            identity,
            {"rows": [{"paper_id": "P001", "title": "Unrelated"}]},
        )

        self.assertEqual(markdown, repaired)
        self.assertEqual("not_applied", report["status"])
        self.assertEqual([9], report["unresolved_callouts"])

    def test_structured_identity_replaces_mixed_legacy_and_canonical_numbers(self) -> None:
        markdown = """# Review

First claim from one paper [16]. Second claim from the same paper [16]. [7]

<!-- paragraph_id: S05-p1 -->

## References

[7] Existing but inconsistently numbered reference.
"""
        sections = {
            "sections": [
                {
                    "section_id": "S05",
                    "paragraphs": [
                        {
                            "paragraph_id": "S05-p1",
                            "cited_paper_ids": ["P817"],
                            "claim_realizations": [
                                {
                                    "claim_id": "S05-p1-C01",
                                    "text": "First claim from one paper.",
                                    "citation_group": ["P817"],
                                },
                                {
                                    "claim_id": "S05-p1-C02",
                                    "text": "Second claim from the same paper.",
                                    "citation_group": ["P817"],
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        matrix = {
            "rows": [
                {
                    "paper_id": "P817",
                    "title": "Preparation of an allene",
                    "year": 2014,
                    "doi": "10.1000/example",
                }
            ]
        }

        identity = citation_entries_from_draft(markdown, sections)
        repaired, report = repair_numbered_references(markdown, identity, matrix)

        self.assertEqual([16], identity["unresolved_callouts"])
        self.assertEqual("applied", report["status"])
        self.assertEqual("structured_paragraph_identity", report["mode"])
        self.assertEqual([16], report["resolved_legacy_callouts"])
        self.assertIn(
            "First claim from one paper. Second claim from the same paper. [1]",
            repaired,
        )
        self.assertNotIn("[16]", repaired)
        self.assertIn("[1] Preparation of an allene. 2014", repaired)


if __name__ == "__main__":
    unittest.main()
