from __future__ import annotations

import unittest

from review_writer_core.author_metadata import (
    author_quality_issues,
    authors_are_publication_ready,
    clean_author_names,
)
from review_writer_core.draft_bibliography import reference_text


class AuthorMetadataTests(unittest.TestCase):
    def test_cleaner_preserves_names_and_removes_superscript_markers(self) -> None:
        self.assertEqual(
            ["Yuli Wang", "Wanli Zhang", "Shengming Ma"],
            clean_author_names(
                [
                    "Yuli Wang",
                    "<sup></sup> Wanli Zhang",
                    "<sup></sup> and Shengming Ma<sup>",
                    "</sup>",
                ]
            ),
        )

    def test_publisher_chrome_and_affiliation_are_not_authors(self) -> None:
        value = [
            "Vol., No. –",
            "Received 2 May 2024; Accepted 9 May 2024",
            "Department of Chemistry, Example University, China",
        ]
        self.assertEqual([], clean_author_names(value))
        self.assertFalse(authors_are_publication_ready(value))
        self.assertIn("authors_contain_non_author_text", author_quality_issues(value))

    def test_reference_renderer_never_emits_rejected_author_residue(self) -> None:
        rendered = reference_text(
            {
                "authors": ["A. Author", "Cite this article", "Vol., No. –"],
                "title": "A paper",
                "journal": "A Journal",
                "year": 2024,
                "pages": "1-9",
            }
        )
        self.assertIn("A. Author", rendered)
        self.assertNotIn("Cite this", rendered)
        self.assertNotIn("Vol.", rendered)

    def test_structural_markers_and_common_mineru_umlaut_damage_are_normalized(self) -> None:
        raw = ["Ruizhi L€u", "Jinqiang Kuang and Shengming Ma", "Yifan Cui +"]
        self.assertEqual(
            ["Ruizhi Lü", "Jinqiang Kuang", "Shengming Ma", "Yifan Cui"],
            clean_author_names(raw),
        )
        self.assertIn("authors_require_normalization", author_quality_issues(raw))


if __name__ == "__main__":
    unittest.main()
