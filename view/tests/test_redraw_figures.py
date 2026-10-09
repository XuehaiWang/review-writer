import argparse
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from PIL import Image


SCRIPT = Path(__file__).parents[2] / "skills" / "review-figure-style-redraw" / "scripts" / "redraw_figures.py"
SPEC = importlib.util.spec_from_file_location("redraw_figures", SCRIPT)
assert SPEC and SPEC.loader
redraw_figures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redraw_figures)


class MonochromePaletteTests(unittest.TestCase):
    """Every redrawn figure is greyscale by default; the switch restores the old look."""

    def test_monochrome_prompt_drops_colour_instructions_and_adds_the_override(self) -> None:
        figure = {"figure_id": "F1"}
        source_prompt = redraw_figures.build_prompt(
            "style", figure, "", redraw_figures.FIGURE_TYPE_COLORED, "source"
        )
        monochrome_prompt = redraw_figures.build_prompt(
            "style", figure, "", redraw_figures.FIGURE_TYPE_COLORED, "monochrome"
        )
        # The previous behaviour is untouched...
        self.assertIn("in its original colour", source_prompt)
        self.assertIn("Retain colours only", source_prompt)
        self.assertNotIn("PALETTE OVERRIDE", source_prompt)
        # ...and the monochrome prompt neither keeps nor requests colour.
        self.assertNotIn("in its original colour", monochrome_prompt)
        self.assertNotIn("Retain colours only", monochrome_prompt)
        self.assertIn("PALETTE OVERRIDE", monochrome_prompt)
        # The scientific lock is unchanged either way.
        for prompt in (source_prompt, monochrome_prompt):
            self.assertIn("Treat the scientific layer as immutable", prompt)

    def test_source_palette_leaves_the_prompt_exactly_as_before(self) -> None:
        prompt = "a prompt"
        self.assertEqual(prompt, redraw_figures.apply_palette(prompt, "source"))
        self.assertTrue(redraw_figures.apply_palette(prompt, "monochrome").startswith(prompt))

    def test_conversion_removes_every_chromatic_pixel_and_keeps_lightness(self) -> None:
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "output.png"
            image = Image.new("RGB", (40, 30), "white")
            for x in range(10, 30):
                for y in range(10, 20):
                    image.putpixel((x, y), (20, 150, 220))
            image.putpixel((2, 2), (0, 0, 0))
            image.save(image_path)
            before = redraw_figures.chromatic_content_metrics(image_path)

            conversion = redraw_figures.to_monochrome(image_path)
            after = redraw_figures.chromatic_content_metrics(image_path)
            check = redraw_figures.monochrome_palette_check(image_path)

            with Image.open(image_path) as result:
                self.assertEqual("L", result.mode)
                pixel = result.convert("RGB").getpixel((20, 15))
        self.assertGreater(before["chromatic_pixels"], 0)
        self.assertEqual(0, after["chromatic_pixels"])
        self.assertEqual("pass", check["status"], check)
        # Luminance is preserved, so neutral antialiasing survives the conversion.
        self.assertEqual((pixel[0], pixel[0], pixel[0]), pixel)
        self.assertEqual(40, conversion["width"])

    def test_the_palette_flag_defaults_to_monochrome_and_accepts_source(self) -> None:
        environment = {key: value for key, value in os.environ.items()
                       if key != "REVIEW_WRITER_FIGURE_PALETTE"}
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(sys, "argv", ["redraw", "--project-id", "p"]):
                self.assertEqual("monochrome", redraw_figures.parse_args().palette)
        with mock.patch.object(
            sys, "argv", ["redraw", "--project-id", "p", "--palette", "source"]
        ):
            self.assertEqual("source", redraw_figures.parse_args().palette)


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
                self.assertEqual(rendered.mode, "L")
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
    def test_gateway_moderation_detail_triggers_academic_safety_retry(self) -> None:
        calls: list[str] = []
        failure = redraw_figures.GatewayRequestError(
            "图像服务请求失败，请稍后重试或联系管理员。",
            status_code=422,
            details={
                "provider_message": "内容被安全审核拦截（疑似成人内容）"
            },
        )

        def requester(prompt: str):
            calls.append(prompt)
            if len(calls) == 1:
                raise failure
            return {"data": [{"b64_json": "aW1hZ2U="}]}

        audit: list[dict[str, object]] = []
        result = redraw_figures.call_with_academic_safety_retry(
            requester,
            "primary prompt",
            "academic chemistry safety prompt",
            audit,
        )

        self.assertEqual("aW1hZ2U=", result["data"][0]["b64_json"])
        self.assertEqual(
            ["primary prompt", "academic chemistry safety prompt"], calls
        )
        self.assertEqual("succeeded", audit[0]["status"])

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
                    output_format="png", render_mode="ocr-hollow-ai", style_name="test", limit=0, dry_run=False,
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
            self.assertEqual(row["status"], "redrawn")
            self.assertEqual(row["chemistry_integrity"]["status"], "failed")
            self.assertTrue(Path(row["redrawn_image"]).is_file())
            self.assertEqual(row["output_disposition"], "saved_with_integrity_warning")
            self.assertIn("OCR transcription", row["prompt"])
            self.assertEqual(captured_edit_args[-2:], ["image[]", "urllib"])

if __name__ == "__main__":
    unittest.main()
