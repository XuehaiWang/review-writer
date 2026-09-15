from io import BytesIO
import unittest
from pypdf import PdfReader, PdfWriter
from view.local_pdf_ingestion import _document_info


class PdfDocumentInfoTests(unittest.TestCase):
    def test_nonstandard_dates_do_not_block_document_info(self):
        for date in ("5th July 2014", "invalid date", "D:20140705120000Z", ""):
            with self.subTest(date=date):
                writer = PdfWriter()
                writer.add_blank_page(width=100, height=100)
                writer.add_metadata({"/Title": "Catalytic study", "/Author": "Author", "/CreationDate": date})
                stream = BytesIO()
                writer.write(stream)
                reader = PdfReader(BytesIO(stream.getvalue()))
                info = _document_info(reader)
                self.assertEqual("Catalytic study", info["title"])
                self.assertEqual(date, info["creation_date"])
                self.assertNotIn("publication_date", info)
