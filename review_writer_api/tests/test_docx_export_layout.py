from __future__ import annotations

import importlib.util
import sys
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

    def test_static_toc_is_grouped_into_styled_section_cards(self) -> None:
        doc = self.export.Document(str(TEMPLATE))
        self.export._clear_body(doc)

        self.export._insert_static_toc(
            doc,
            [
                (2, "1. Introduction"),
                (3, "1.1 Scope and terminology"),
                (4, "1.1.1 Evidence boundary"),
                (2, "2. Catalytic strategies"),
                (3, "2.1 Copper catalysis"),
            ],
        )

        self.assertEqual(1, len(doc.tables))
        table = doc.tables[0]
        self.assertEqual(2, len(table.rows))
        self.assertEqual("01", table.cell(0, 0).text)
        self.assertIn("Introduction", table.cell(0, 1).text)
        self.assertIn("Scope and terminology", table.cell(0, 1).text)
        self.assertEqual("02", table.cell(1, 0).text)
        self.assertIn('w:fill="1F6B54"', table.cell(0, 0)._tc.xml)

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


if __name__ == "__main__":
    unittest.main()
