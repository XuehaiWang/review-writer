from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "review-export-docx" / "scripts" / "md2docx.py"
TEMPLATE = ROOT / "skills" / "review-export-docx" / "review_template.docx"


def load_export_module():
    spec = importlib.util.spec_from_file_location("review_export_md2docx", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load md2docx module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DocxExportLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.export = load_export_module()

    def test_full_review_chart_anchor_is_before_introduction(self) -> None:
        self.assertTrue(
            self.export.should_insert_full_chart_before_heading("1. Introduction")
        )
        self.assertTrue(
            self.export.should_insert_full_chart_before_heading("引言")
        )
        self.assertFalse(
            self.export.should_insert_full_chart_before_heading("Results and discussion")
        )

    def test_docx_export_does_not_insert_a_table_of_contents(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            source = directory / "review.md"
            output = directory / "review.docx"
            source.write_text(
                "# Review title\n\n## Abstract\n\nA bounded abstract.\n\n"
                "## 1. Introduction\n\nOpening text.\n\n"
                "## 2. Methods\n\nMethod text.\n",
                encoding="utf-8",
            )

            self.export.convert(source, output, TEMPLATE)

            document = self.export.Document(str(output))
            visible = "\n".join(paragraph.text for paragraph in document.paragraphs)
            self.assertNotIn("Table of Contents", visible)
            self.assertEqual([], document.tables)


if __name__ == "__main__":
    unittest.main()
