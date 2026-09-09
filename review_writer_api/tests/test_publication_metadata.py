from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_writer_api import scientific_tasks
from review_writer_core.publication_metadata import (
    _layout_page_regions,
    extract_front_matter_doi,
    extract_publication_evidence,
    extract_publication_metadata,
    read_pdf_first_page_text,
    resolve_local_publication_extraction,
    validate_model_publication_extraction,
)


class PublicationMetadataTests(unittest.TestCase):
    def test_layout_regions_follow_whitespace_even_when_header_exceeds_eighteen_percent(self) -> None:
        layout = "\n".join(
            [*[f"Header {index}" for index in range(10)], *([""] * 4)]
            + [f"Body {index}" for index in range(24)]
            + ["", "", "", "Footer 1", "Footer 2"]
        )

        _full, header, body, footer = _layout_page_regions(layout)

        self.assertIn("Header 9", header)
        self.assertNotIn("Body 0", header)
        self.assertIn("Body 0", body)
        self.assertEqual("Footer 1\nFooter 2", footer)

    def test_dense_layout_uses_text_density_window_without_fixed_ratio(self) -> None:
        layout = "\n".join(f"Line {index}" for index in range(100))

        _full, header, body, footer = _layout_page_regions(layout)

        self.assertEqual(10, len(header.splitlines()))
        self.assertEqual(80, len(body.splitlines()))
        self.assertEqual(10, len(footer.splitlines()))

    def test_first_page_text_exposes_coordinate_header_and_footer_regions(self) -> None:
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "coordinate-regions.pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(width=612, height=792)
            font = writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }
                )
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): font}
                    )
                }
            )
            content = DecodedStreamObject()
            content.set_data(
                b"BT /F1 10 Tf 72 760 Td (Journal Header 12 \\(2024\\) 100123) Tj ET\n"
                b"BT /F1 10 Tf 72 400 Td (Body reference Nature 2020, 1, 1-2) Tj ET\n"
                b"BT /F1 10 Tf 72 24 Td (Footer Journal 2024, 12, 100-110) Tj ET"
            )
            page[NameObject("/Contents")] = writer._add_object(content)
            with path.open("wb") as output:
                writer.write(output)

            extracted = read_pdf_first_page_text(path)

        self.assertEqual("coordinate_layout", extracted.extraction_mode)
        self.assertIn("Journal Header 12 (2024) 100123", extracted.header_text)
        self.assertIn("Footer Journal 2024, 12, 100-110", extracted.footer_text)
        self.assertNotIn("Body reference", extracted.header_text)
        self.assertNotIn("Body reference", extracted.footer_text)

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

    def test_reliable_local_evidence_skips_metadata_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "paper.md"
            pdf = root / "paper.pdf"
            output = root / "result.json"
            markdown.write_text(
                "Published online 18 June 2024\n\n# Introduction",
                encoding="utf-8",
            )
            pdf.write_bytes(b"%PDF-test")
            args = SimpleNamespace(
                markdown=markdown,
                pdf=pdf,
                filename="paper.pdf",
                output=output,
            )
            with (
                patch.object(scientific_tasks, "gateway_configured", return_value=True),
                patch.object(
                    scientific_tasks,
                    "read_pdf_first_page_text",
                    return_value="Published online 18 June 2024",
                ),
                patch.object(scientific_tasks, "call_json_model") as model_call,
            ):
                scientific_tasks.publication_date_extract(args)

            result = json.loads(output.read_text(encoding="utf-8"))
            model_call.assert_not_called()
            self.assertFalse(result["model_attempted"])
            self.assertFalse(result["model_needed"])


if __name__ == "__main__":
    unittest.main()
