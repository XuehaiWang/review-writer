from __future__ import annotations

import unittest

from review_writer_core.mineru_bibliography import (
    as_document_audit_extraction,
    extract_mineru_bibliography,
)
from review_writer_core.bibliography_audit import audit_bibliography


class MinerUBibliographyTests(unittest.TestCase):
    def test_recovers_clean_citation_fields_and_byline(self) -> None:
        markdown = """pubs.acs.org/joc

# Synthesis of Multisubstituted Allenamides

Zhi Zhang, Miao-Miao Ji, Xiao-Feng Wu, and Jin-Bao Peng\\*<sup>1</sup>

Cite This: J. Org. Chem. 2024, 89, 9001−9010

ABSTRACT: A paper abstract.
"""
        blocks = [
            {
                "type": "text",
                "text": "Synthesis of Multisubstituted Allenamides",
                "page_idx": 0,
                "bbox": [10, 20, 500, 60],
            },
            {
                "type": "text",
                "text": "Zhi Zhang, Miao-Miao Ji, Xiao-Feng Wu, and Jin-Bao Peng*",
                "page_idx": 0,
                "bbox": [10, 70, 500, 90],
            },
        ]

        result = extract_mineru_bibliography(blocks, markdown, filename="paper.pdf")
        fields = result["fields"]

        self.assertEqual(
            ["Zhi Zhang", "Miao-Miao Ji", "Xiao-Feng Wu", "Jin-Bao Peng"],
            fields["authors"]["value"],
        )
        self.assertEqual("J. Org. Chem.", fields["journal"]["value"])
        self.assertEqual(2024, fields["year"]["value"])
        self.assertEqual("89", fields["volume"]["value"])
        self.assertEqual("9001-9010", fields["pages"]["value"])
        self.assertEqual("confirmed", fields["authors"]["verification_status"])
        self.assertEqual("mineru-block-1", fields["authors"]["evidence"]["block_id"])
        audit_extraction = as_document_audit_extraction(result)
        self.assertEqual("verified", audit_extraction["fields"]["authors"]["verification_status"])
        self.assertEqual("mineru-block-1", audit_extraction["fields"]["authors"]["block_id"])

        metadata = {
            key: value
            for key, value in fields.items()
            if key in {
                "title",
                "authors",
                "journal",
                "year",
                "bibliographic_year",
                "volume",
                "pages",
                "doi",
            }
        }
        audit = audit_bibliography(
            metadata,
            connectors=[],
            network_mode="disabled",
            local_extraction={"status": "insufficient"},
        )
        self.assertEqual("verified", audit["status"])
        self.assertEqual("mineru_local_document", audit["verification_method"])
        self.assertFalse(audit["network_lookup"]["used"])

    def test_rejects_publisher_residue_as_authors(self) -> None:
        markdown = """# A Reliable Article Title

Vol., No. –

Received 1 May 2024; Accepted 2 June 2024

## Abstract
"""

        result = extract_mineru_bibliography([], markdown, filename="paper.pdf")

        self.assertIsNone(result["fields"]["authors"]["value"])
        self.assertEqual("missing", result["fields"]["authors"]["verification_status"])
        self.assertIn("authors", result["needs_agent_fields"])

    def test_unlabelled_journal_mention_does_not_become_canonical_journal(self) -> None:
        markdown = """# A Reliable Article Title

Alice Author and Bob Chemist

This paper compares work previously published in Green Chemistry 2017.
"""

        result = extract_mineru_bibliography([], markdown, filename="paper.pdf")

        self.assertNotIn("journal", result["fields"])
        self.assertIn("journal", result["needs_agent_fields"])

    def test_pdf_first_page_recovers_byline_missing_from_mineru_markdown(self) -> None:
        title = (
            "Dimethylprolinol Versus Diphenylprolinol in CuBr2-Catalyzed "
            "Enantioselective Allenylation of Terminal Alkynols"
        )
        result = extract_mineru_bibliography(
            [],
            f"# {title}\n\nLaboratory of Molecular Recognition and Synthesis\n",
            filename="paper.pdf",
            pdf_first_page_text=(
                f"{title}\nDengke Ma\nXinyu Duan\nChunling Fu\nXin Huang*\n"
                "Shengming Ma* 0000-0002-2866-2431\n"
                "Abstract The reaction gives useful products.\n"
            ),
        )
        self.assertEqual(
            ["Dengke Ma", "Xinyu Duan", "Chunling Fu", "Xin Huang", "Shengming Ma"],
            result["fields"]["authors"]["value"],
        )
        self.assertEqual(
            "pdf_first_page_byline", result["fields"]["authors"]["source"]
        )


if __name__ == "__main__":
    unittest.main()
