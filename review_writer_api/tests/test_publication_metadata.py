from __future__ import annotations

import unittest

from review_writer_core.publication_metadata import (
    extract_front_matter_doi,
    extract_publication_evidence,
    extract_publication_metadata,
    resolve_local_publication_extraction,
    validate_model_publication_extraction,
)


class PublicationMetadataTests(unittest.TestCase):
    def test_labelled_publication_date_beats_download_and_acceptance_dates(self) -> None:
        fields = extract_publication_metadata(
            """
            Received 22 October 2012; accepted 3 January 2013;
            published online 10 February 2013
            Downloaded by Example University on 16 June 2026.
            """,
            "downloaded-2026.pdf",
        )

        self.assertEqual("2013-02", fields["first_publication_date"]["value"])
        self.assertEqual(2013, fields["bibliographic_year"]["value"])
        self.assertEqual("online_first", fields["publication_status"]["value"])
        self.assertEqual(2013, fields["year"]["value"])

    def test_file_creation_or_unlabelled_body_year_is_not_used(self) -> None:
        fields = extract_publication_metadata(
            "Received 1 May 2024. This work cites a foundational study from 1998.",
            "paper.pdf",
        )

        self.assertIsNone(fields["first_publication_date"]["value"])
        self.assertIsNone(fields["bibliographic_year"]["value"])
        self.assertIsNone(fields["year"]["value"])

    def test_journal_volume_issue_header_recovers_older_publication_year(self) -> None:
        fields = extract_publication_metadata(
            "Angew. Chem. Int. Ed.2002, 41, No. 16 2002 WILEY-VCH\n"
            "Enantioselective Synthesis with Allenes"
        )

        self.assertEqual(2002, fields["year"]["value"])
        self.assertEqual(2002, fields["bibliographic_year"]["value"])
        self.assertEqual(
            "local_document:journal_volume_issue_header",
            fields["bibliographic_year"]["source"],
        )
        self.assertGreaterEqual(fields["bibliographic_year"]["confidence"], 0.94)

    def test_doi_after_references_heading_is_not_an_article_doi_candidate(self) -> None:
        candidate = extract_front_matter_doi(
            "Article title\n\n# References\nSmith et al. https://doi.org/10.1000/reference.1"
        )

        self.assertIsNone(candidate["value"])

    def test_explicit_front_matter_doi_is_retained_as_low_confidence_candidate(self) -> None:
        candidate = extract_front_matter_doi(
            "Article title\nDOI: 10.1002/example.123\n\n# Introduction"
        )

        self.assertEqual("10.1002/example.123", candidate["value"])
        self.assertLess(candidate["confidence"], 0.9)

    def test_local_extraction_returns_requested_basic_info_shape(self) -> None:
        extracted = extract_publication_evidence(
            "Published online: 18 June 2024",
            source_location="mineru_markdown_front_matter",
        )

        self.assertEqual(
            {"publication_year": 2024, "publication_date": "2024-06"},
            extracted["basic_info"],
        )
        self.assertEqual("reliable", extracted["status"])
        self.assertFalse(extracted["network_required"])

    def test_model_date_is_trusted_only_when_quote_exists_in_selected_source(self) -> None:
        payload = {
            "basic_info": {
                "publication_year": 2024,
                "publication_date": "2024-06",
            },
            "publication_evidence": {
                "source_text": "Published online 18 June 2024",
                "source_location": "pdf_page_1",
                "date_type": "published_online",
                "confidence": 0.99,
            },
        }
        valid = validate_model_publication_extraction(
            payload,
            sources={"pdf_page_1": "Published online 18 June 2024"},
        )
        invalid = validate_model_publication_extraction(
            payload,
            sources={"pdf_page_1": "Received 18 June 2024"},
        )

        self.assertEqual("reliable", valid["status"])
        self.assertEqual("insufficient", invalid["status"])
        self.assertLessEqual(invalid["publication_evidence"]["confidence"], 0.35)

    def test_conflicting_reliable_pdf_and_markdown_dates_require_network(self) -> None:
        resolved = resolve_local_publication_extraction(
            markdown_text="Published online 18 June 2024",
            pdf_first_page_text="Journal Name 2023, 10, No. 2",
            filename="paper.pdf",
        )

        self.assertEqual("conflict", resolved["status"])
        self.assertTrue(resolved["network_required"])


if __name__ == "__main__":
    unittest.main()
