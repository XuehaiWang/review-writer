from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "review-figure-style-redraw" / "scripts" / "redraw_figures.py"
SPEC = importlib.util.spec_from_file_location("review_writer_redraw_figures", MODULE_PATH)
assert SPEC and SPEC.loader
REDRAW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REDRAW)


class FigureAspectRatioChecks(unittest.TestCase):
    def test_wide_source_is_letterboxed_and_cropped_back_without_stretching(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.png"
            framed_path = root / "framed.png"
            output_path = root / "output.png"
            source = Image.new("RGB", (200, 80), "white")
            draw = ImageDraw.Draw(source)
            draw.rectangle((35, 15, 165, 65), outline="black", width=4)
            source.save(source_path)

            framing = REDRAW.prepare_aspect_preserving_edit_input(source_path, framed_path)
            self.assertTrue(framing["applied"])
            self.assertEqual(framing["content_box"], [0, 60, 200, 80])
            with Image.open(framed_path) as framed:
                self.assertEqual(framed.size, (200, 200))
                self.assertEqual(framed.getpixel((100, 20)), (255, 255, 255))
                framed.resize((100, 100), Image.Resampling.LANCZOS).save(output_path)

            result = REDRAW.normalize_generated_aspect(output_path, source_path, framing)
            self.assertEqual(result["crop_mode"], "letterbox_content_box")
            self.assertEqual(result["normalized_size"], [200, 80])
            with Image.open(output_path) as normalized:
                self.assertEqual(normalized.size, (200, 80))
                self.assertLess(sum(normalized.getpixel((35, 40))) / 3, 120)

    def test_tall_source_uses_the_same_coordinate_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.png"
            framed_path = root / "framed.png"
            output_path = root / "output.png"
            Image.new("RGB", (60, 180), "white").save(source_path)
            framing = REDRAW.prepare_aspect_preserving_edit_input(source_path, framed_path)
            self.assertEqual(framing["content_box"], [60, 0, 60, 180])
            Image.open(framed_path).resize((1024, 1024), Image.Resampling.BILINEAR).save(output_path)
            result = REDRAW.normalize_generated_aspect(output_path, source_path, framing)
            self.assertEqual(result["normalized_size"], [60, 180])
            self.assertAlmostEqual(result["normalized_aspect_ratio"], 1 / 3)

    def test_matching_provider_ratio_is_not_cropped_twice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.png"
            framed_path = root / "framed.png"
            output_path = root / "output.png"
            Image.new("RGB", (240, 120), "white").save(source_path)
            framing = REDRAW.prepare_aspect_preserving_edit_input(source_path, framed_path)
            Image.new("RGB", (1200, 600), "white").save(output_path)
            result = REDRAW.normalize_generated_aspect(output_path, source_path, framing)
            self.assertEqual(result["crop_mode"], "provider_already_matches_source")
            with Image.open(output_path) as normalized:
                self.assertEqual(normalized.size, (240, 120))


if __name__ == "__main__":
    unittest.main()
