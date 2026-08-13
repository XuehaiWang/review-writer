from __future__ import annotations

import runpy
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "skills" / "review-export-docx" / "scripts" / "md2docx.py"
EXPORT_WRAPPER = ROOT / "skills" / "review-export-docx" / "scripts" / "run_md2docx.py"
DASHBOARD_SERVER = ROOT / "view" / "serve_review_dashboard.py"
FINAL_DASHBOARD = ROOT / "view" / "assets" / "dashboard" / "final.html"
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
        cls.wrapper = runpy.run_path(str(EXPORT_WRAPPER))
        cls.section_generator = runpy.run_path(str(SECTION_GENERATOR))

    def test_dependency_cache_is_partitioned_by_runtime_and_platform(self) -> None:
        target = self.wrapper["dependency_dir"]()

        self.assertEqual(target.parent.name, ".deps")
        self.assertNotEqual(target, target.parent)
        self.assertIn(sys.implementation.cache_tag, target.name)

    def test_dependency_probe_imports_pillow_native_extension(self) -> None:
        self.assertIn("from PIL import Image, _imaging", self.wrapper["IMPORT_PROBE"])
        ready, detail = self.wrapper["probe_dependencies"]()

        self.assertTrue(ready, detail)

    def test_docker_context_excludes_workstation_dependency_cache(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        self.assertIn("skills/review-export-docx/.deps", dockerignore)

    def test_docx_download_keeps_filename_extension(self) -> None:
        server_source = DASHBOARD_SERVER.read_text(encoding="utf-8")
        dashboard_source = FINAL_DASHBOARD.read_text(encoding="utf-8")

        self.assertIn('"download_name": docx_path.name', server_source)
        self.assertIn('download_name=path.name if path.suffix.lower() == ".docx" else None', server_source)
        self.assertIn('self.send_header(\n                    "Content-Disposition"', server_source)
        self.assertIn("const docxDownloadName=", dashboard_source)
        self.assertIn("download=\"'+esc(docxDownloadName(payload.final_draft_docx_path))+'\"", dashboard_source)
        self.assertIn("link.download=docxDownloadName(result.download_name||result.path)", dashboard_source)

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

    def test_spaced_mineru_mathrm_and_mathsf_export(self) -> None:
        source = (
            "# Check\n\n"
            "Carboxylation with { \\mathrm { C O } } _ { 2 }.\n\n"
            "Product from { \\mathsf { C O } } _ { 2 } { \\mathrm { : } }.\n"
        )
        normalized = self.exporter["normalize_mineru_latex"](source)
        self.assertIn("$\\mathrm{CO}_{2}$", normalized)
        self.assertIn("$\\mathsf{CO}_{2}$", normalized)
        self.assertIn("$\\mathrm{:}$", normalized)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            markdown = temp / "mineru.md"
            output = temp / "mineru.docx"
            markdown.write_text(source, encoding="utf-8")

            self.exporter["convert"](markdown, output, TEMPLATE)

            self.assertTrue(output.is_file())
            with ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml")
                ET.fromstring(document_xml)
                self.assertIn(b"CO", document_xml)

    def test_existing_complex_math_formula_is_not_rewritten(self) -> None:
        existing_formula = (
            "$\\mathrm { B } ( \\mathrm { C } _ { 6 } \\mathrm { F } _ { 5 } ) _ { 3 } "
            "{ \\mathsf { C } } 2 { \\bigl ( } \\mathsf { s p } ^ { 2 } { \\bigr ) } "
            "{ \\mathsf { - C } } 3$"
        )
        source = (
            "# Check\n\n"
            "Raw { \\mathsf { C O } } _ { 2 } must be normalized.\n\n"
            f"Existing formula must remain intact: {existing_formula}\n"
        )

        normalized = self.exporter["normalize_mineru_latex"](source)

        self.assertIn("$\\mathsf{CO}_{2}$", normalized)
        self.assertEqual(normalized.count(existing_formula), 1)
        self.assertNotIn("$$\\mathsf{C}$", normalized)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            markdown = temp / "existing-math.md"
            output = temp / "existing-math.docx"
            markdown.write_text(source, encoding="utf-8")

            self.exporter["convert"](markdown, output, TEMPLATE)

            self.assertTrue(output.is_file())
            with ZipFile(output) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        ET.fromstring(archive.read(name))

    def test_raw_mathsf_prime_and_mathbf_are_normalized_without_blocking(self) -> None:
        source = (
            "# Check\n\n"
            "MinerU raw notation: \\mathsf { C O } _ { 2 }, "
            "\\mathbf { R }, and x\\prime.\n"
        )

        normalized = self.exporter["normalize_mineru_latex"](source)

        self.assertIn("$\\mathsf{CO}_{2}$", normalized)
        self.assertIn("$\\mathbf{R}$", normalized)
        self.assertIn("$\\prime$", normalized)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            markdown = temp / "raw-wrapper.md"
            output = temp / "raw-wrapper.docx"
            markdown.write_text(source, encoding="utf-8")

            self.exporter["convert"](markdown, output, TEMPLATE)

            self.assertTrue(output.is_file())
            with ZipFile(output) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        ET.fromstring(archive.read(name))

    def test_unknown_mineru_latex_is_warning_only_and_docx_is_created(self) -> None:
        source = "# Check\n\nUnsupported but visible: \\unknowncommand { X }.\n"

        normalized = self.exporter["normalize_mineru_latex"](source)
        self.assertIn("\\unknowncommand", normalized)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            markdown = temp / "unknown-command.md"
            output = temp / "unknown-command.docx"
            markdown.write_text(source, encoding="utf-8")

            self.exporter["convert"](markdown, output, TEMPLATE)

            self.assertTrue(output.is_file())
            with ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml")
                ET.fromstring(document_xml)
                self.assertIn(b"unknowncommand", document_xml)


if __name__ == "__main__":
    unittest.main()
