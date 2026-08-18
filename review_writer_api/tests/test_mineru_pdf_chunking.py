from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_ingestion_module():
    path = ROOT / "view" / "local_pdf_ingestion.py"
    spec = importlib.util.spec_from_file_location("test_local_pdf_ingestion", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MinerUPdfChunkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ingestion = load_ingestion_module()

    def test_process_error_prioritizes_provider_failure(self) -> None:
        completed = subprocess.CompletedProcess(
            ["mineru"],
            1,
            stdout=json.dumps({"failed_count": 1, "manifest_path": "a/very/long/path"}),
            stderr=(
                "[batch] request-id\n"
                "[failed] paper.pdf: page count exceeds the provider limit\n"
            ),
        )

        detail = self.ingestion._process_error_detail(completed)

        self.assertEqual(
            "[failed] paper.pdf: page count exceeds the provider limit", detail
        )

    def test_split_and_merge_preserves_pages_and_images(self) -> None:
        from pypdf import PdfReader, PdfWriter

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "paper.pdf"
            writer = PdfWriter()
            for _ in range(201):
                writer.add_blank_page(width=612, height=792)
            with source.open("wb") as stream:
                writer.write(stream)

            parts = self.ingestion._split_pdf_for_mineru(
                source, root / "chunks", "p001"
            )
            self.assertEqual(2, len(parts))
            self.assertEqual(200, len(PdfReader(str(parts[0].pdf_path)).pages))
            self.assertEqual(1, len(PdfReader(str(parts[1].pdf_path)).pages))

            output = root / "mineru-outputs"
            for part in parts:
                extracted = output / "extracted" / part.slug
                images = extracted / "images"
                images.mkdir(parents=True)
                (images / "figure.png").write_bytes(b"image")
                (extracted / "full.md").write_text(
                    "![figure](images/figure.png)\n", encoding="utf-8"
                )
                (extracted / f"{part.slug}_content_list.json").write_text(
                    json.dumps(
                        [
                            {
                                "type": "image",
                                "img_path": "images/figure.png",
                                "page_idx": 0,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                markdown = output / "markdown" / f"{part.slug}.md"
                markdown.parent.mkdir(parents=True, exist_ok=True)
                markdown.write_text(
                    f"![figure](../extracted/{part.slug}/images/figure.png)\n",
                    encoding="utf-8",
                )

            self.ingestion._merge_mineru_parts(output, "p001", parts)

            merged_dir = output / "extracted" / "p001"
            blocks = json.loads(
                (merged_dir / "p001_content_list.json").read_text(encoding="utf-8")
            )
            self.assertEqual([0, 200], [block["page_idx"] for block in blocks])
            self.assertEqual(
                [
                    "parts/part-001/images/figure.png",
                    "parts/part-002/images/figure.png",
                ],
                [block["img_path"] for block in blocks],
            )
            for block in blocks:
                self.assertTrue((merged_dir / block["img_path"]).is_file())
            merged_markdown = (output / "markdown" / "p001.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("mineru_source_pages: 1-200", merged_markdown)
            self.assertIn("mineru_source_pages: 201-201", merged_markdown)
            self.assertIn(
                "../extracted/p001/parts/part-002/images/figure.png",
                merged_markdown,
            )


if __name__ == "__main__":
    unittest.main()
