from __future__ import annotations

import runpy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "skills" / "review-export-docx" / "scripts" / "md2docx.py"
SECTION_GENERATOR = (
    ROOT
    / "skills"
    / "review-section-drafting-figure-picking"
    / "scripts"
    / "generate_section_drafts.py"
)
TEMPLATE = ROOT / "skills" / "review-export-docx" / "review_template.docx"


class DocxUnicodeCompatibilityChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.exporter = runpy.run_path(str(EXPORTER))
        cls.section_generator = runpy.run_path(str(SECTION_GENERATOR))

    def test_repairs_known_relay_truncation(self) -> None:
        damaged = "C\x13X; \x03b1/\x03b3; SN2\x02; C\x03C; \x00"
        expected = "C\u2013X; \u03b1/\u03b3; SN2\u2032; C\u2013C; \uFFFD"

        repaired, replaced = self.exporter["make_xml_compatible"](damaged)
        self.assertEqual(repaired, expected)
        self.assertEqual(replaced, 1)
        self.assertEqual(
            self.section_generator["repair_model_unicode"]({"text": damaged}),
            {"text": expected},
        )

    def test_damaged_markdown_exports_as_valid_ooxml(self) -> None:
        damaged = "# Check\n\nC\x13X \x03b1 \x03b3 SN2\x02 C\x03C \x00\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            markdown = temp / "input.md"
            output = temp / "output.docx"
            markdown.write_text(damaged, encoding="utf-8")

            self.exporter["convert"](markdown, output, TEMPLATE)

            self.assertTrue(output.is_file())
            with ZipFile(output) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        ET.fromstring(archive.read(name))


if __name__ == "__main__":
    unittest.main()
