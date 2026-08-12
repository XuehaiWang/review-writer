import argparse
import base64
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image


SCRIPT = Path(__file__).parents[2] / "skills" / "review-figure-style-redraw" / "scripts" / "redraw_figures.py"
SPEC = importlib.util.spec_from_file_location("redraw_figures", SCRIPT)
assert SPEC and SPEC.loader
redraw_figures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redraw_figures)


class SourceFaithfulRenderTests(unittest.TestCase):
    def test_source_faithful_render_upscales_output_before_binarizing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            output = root / "output.png"
            Image.new("RGB", (11, 7), "white").save(source)

            rendering = redraw_figures.render_source_faithful_bw(source, output)

            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (44, 28))
                self.assertEqual(rendered.mode, "1")
            self.assertEqual(rendering["width"], 44)
            self.assertEqual(rendering["height"], 28)
            self.assertEqual(rendering["scale_factor"], 4)

    def test_source_faithful_render_removes_light_color_fills_without_losing_dark_strokes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            output = root / "output.png"
            image = Image.new("RGB", (8, 2), "white")
            # The first two swatches model the light cyan and magenta fills in P091.
            image.putpixel((1, 0), (127, 255, 254))
            image.putpixel((3, 0), (255, 174, 255))
            # These are black ink and a dark magenta chemical stroke respectively.
            image.putpixel((5, 0), (0, 0, 0))
            image.putpixel((7, 0), (194, 70, 192))
            image.save(source)

            redraw_figures.render_source_faithful_bw(source, output)

            with Image.open(output).convert("L") as rendered:
                self.assertEqual(rendered.getpixel((1 * 4 + 2, 2)), 255)
                self.assertEqual(rendered.getpixel((3 * 4 + 2, 2)), 255)
                self.assertEqual(rendered.getpixel((5 * 4 + 2, 2)), 0)
                self.assertEqual(rendered.getpixel((7 * 4 + 2, 2)), 0)

class OcrValidationTests(unittest.TestCase):
    def test_curl_image_edit_keeps_api_key_out_of_command_arguments(self) -> None:
        captured: dict[str, object] = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs["input"]
            return subprocess.CompletedProcess(command, 0, stdout=b'{"data": []}', stderr=b"")

        redraw_figures.call_images_edit_curl(
            "secret-key",
            "https://example.test",
            Path("figure.png"),
            "preserve labels",
            "gpt-image-2",
            "high",
            "opaque",
            "png",
            "image",
            runner,
        )

        self.assertNotIn("secret-key", " ".join(captured["command"]))
        self.assertIn(b"Authorization: Bearer secret-key", captured["input"])
        self.assertIn("image=@figure.png", captured["command"])

    def test_image_edit_file_field_supports_provider_specific_singular_name(self) -> None:
        source = Path("figure.png")

        fields = redraw_figures.image_edit_file_fields(source, "image")

        self.assertEqual(fields, [("image", source)])

    def test_project_tesseract_runtime_is_discovered_without_system_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / ".tmp" / "tesseract" / "runtime" / "tesseract.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")

            command = redraw_figures.resolve_tesseract_command(root, "")

            self.assertEqual(command, str(executable))

    def test_ai_edit_prompt_excludes_candidate_metadata_that_is_not_in_source_pixels(self) -> None:
        prompt = redraw_figures.build_prompt("test", {
            "source_label": "Scheme 2",
            "source_caption_text": "Scheme 2. A proposed reaction pathway.",
            "what_it_shows": "Catalytic cycle.",
            "fits_paragraph_or_claim": "Compare palladium pathways.",
        })

        self.assertNotIn("Scheme 2", prompt)
        self.assertNotIn("Catalytic cycle", prompt)
        self.assertNotIn("Compare palladium pathways", prompt)

    def test_extract_ocr_text_returns_tesseract_output(self) -> None:
        def runner(command, **kwargs):
            self.assertEqual(command, [
                "tesseract", "figure.png", "stdout", "-l", "eng",
                "--psm", "3", "-c", "user_defined_dpi=300",
            ])
            return subprocess.CompletedProcess(command, 0, stdout="Pd(OAc)2\n85%\n", stderr="")

        result = redraw_figures.extract_ocr_text(Path("figure.png"), "eng", runner)

        self.assertEqual(result, {"status": "ok", "text": "Pd(OAc)2\n85%"})

    def test_extract_ocr_text_marks_missing_tesseract_as_unavailable(self) -> None:
        def runner(command, **kwargs):
            raise FileNotFoundError("tesseract")

        result = redraw_figures.extract_ocr_text(Path("figure.png"), "eng", runner)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["text"], "")

    def test_compare_ocr_text_reports_source_tokens_missing_from_output(self) -> None:
        result = redraw_figures.compare_ocr_text("[Pd] THF 85%", "[Pd] THF")

        self.assertEqual(result["status"], "needs_human_check")
        self.assertEqual(result["missing_tokens"], ["85%"])

    def test_ai_edit_manifest_records_ocr_constraint_and_missing_tokens(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            stage = project / "02_section_drafting"
            stage.mkdir(parents=True)
            source = root / "source.png"
            Image.new("RGB", (20, 10), "white").save(source)
            (stage / "figure_candidates.json").write_text(json.dumps([{
                "figure_id": "F001", "paper_id": "P001", "source_label": "Scheme 1",
                "source_type": "image", "source_image_path": str(source),
            }]), encoding="utf-8")
            image_bytes = io.BytesIO()
            Image.new("RGB", (20, 10), "white").save(image_bytes, format="PNG")
            original_edit = redraw_figures.call_images_edit
            original_ocr = redraw_figures.extract_ocr_text
            captured_edit_args: list[object] = []

            def edit(*args):
                captured_edit_args.extend(args)
                return {"data": [{"b64_json": base64.b64encode(image_bytes.getvalue()).decode()}]}

            redraw_figures.call_images_edit = edit
            redraw_figures.extract_ocr_text = lambda path, language, **kwargs: (
                {"status": "ok", "text": "Pd(OAc)2\n85%"}
                if path.parent.name == "source" else {"status": "ok", "text": "Pd(OAc)2"}
            )
            try:
                redraw_figures.run(argparse.Namespace(
                    review_root=str(root), project_id="demo", paper_id="", figures_file="", base_url="https://example.test",
                    wire_api="images", api_key="test-key", model="gpt-image-2", quality="high", background="opaque",
                    output_format="png", render_mode="ai-edit", style_name="test", limit=0, dry_run=False,
                    require_redrawn=False, ocr_language="eng", tesseract_cmd="",
                    image_field="image[]",
                    images_transport="urllib",
                    edit_profile="standard",
                ))
            finally:
                redraw_figures.call_images_edit = original_edit
                redraw_figures.extract_ocr_text = original_ocr

            manifest = json.loads((project / "03_figure_redraw" / "redrawn_figure_manifest.json").read_text(encoding="utf-8"))
            row = manifest["figures"][0]
            self.assertEqual(row["ocr_source_text"], "Pd(OAc)2\n85%")
            self.assertEqual(row["ocr_output_text"], "Pd(OAc)2")
            self.assertEqual(row["missing_ocr_tokens"], ["85%"])
            self.assertEqual(row["ocr_check_status"], "needs_human_check")
            self.assertEqual(row["status"], "chemistry_integrity_failed")
            self.assertEqual(row["chemistry_integrity"]["status"], "failed")
            self.assertIsNone(row["redrawn_image"])
            self.assertIn("OCR transcription", row["prompt"])
            self.assertEqual(captured_edit_args[-2:], ["image[]", "urllib"])

if __name__ == "__main__":
    unittest.main()
