from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "review-figure-style-redraw" / "scripts" / "redraw_figures.py"
SPEC = importlib.util.spec_from_file_location("redraw_figures_background_cleanup", SCRIPT)
assert SPEC and SPEC.loader
REDRAW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REDRAW)


class FigureRedrawBackgroundCleanupTests(unittest.TestCase):
    def test_chemistry_cleanup_removes_gray_halo_and_preserves_scientific_ink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mechanism.png"
            image = Image.new("RGB", (100, 100), "white")
            image.putpixel((20, 20), (0, 0, 0))
            image.putpixel((21, 20), (210, 210, 210))
            image.putpixel((40, 40), (180, 30, 30))
            image.putpixel((41, 40), (235, 205, 205))
            image.save(path)

            report = REDRAW.clean_ai_chemistry_background(
                path,
                REDRAW.FIGURE_TYPE_MECHANISM,
            )

            with Image.open(path) as cleaned:
                rgb = cleaned.convert("RGB")
                self.assertEqual((100, 100), rgb.size)
                self.assertEqual((0, 0, 0), rgb.getpixel((20, 20)))
                self.assertEqual((255, 255, 255), rgb.getpixel((21, 20)))
                self.assertEqual((180, 30, 30), rgb.getpixel((40, 40)))
                self.assertEqual((235, 205, 205), rgb.getpixel((41, 40)))
            self.assertEqual("cleaned", report["status"])
            self.assertEqual(1, report["removed_pixels"])

    def test_plot_cleanup_is_skipped_to_preserve_semantic_gray(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plot.png"
            image = Image.new("RGB", (20, 20), "white")
            image.putpixel((5, 5), (210, 210, 210))
            image.save(path)

            report = REDRAW.clean_ai_chemistry_background(
                path,
                REDRAW.FIGURE_TYPE_PLOT,
            )

            with Image.open(path) as unchanged:
                self.assertEqual(
                    (210, 210, 210), unchanged.convert("RGB").getpixel((5, 5))
                )
            self.assertEqual("skipped", report["status"])

    def test_large_gray_region_fails_safe_without_modifying_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheme.png"
            image = Image.new("RGB", (20, 20), (210, 210, 210))
            image.save(path)

            report = REDRAW.clean_ai_chemistry_background(
                path,
                REDRAW.FIGURE_TYPE_SIMPLE,
            )

            with Image.open(path) as unchanged:
                self.assertEqual(
                    (210, 210, 210), unchanged.convert("RGB").getpixel((5, 5))
                )
            self.assertEqual("skipped", report["status"])
            self.assertEqual("local_halo_region_too_large", report["reason"])

    def test_off_white_provider_canvas_is_normalized_even_when_it_covers_the_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-canvas.png"
            image = Image.new("RGB", (20, 20), (250, 250, 250))
            image.putpixel((10, 10), (0, 0, 0))
            image.save(path)

            report = REDRAW.clean_ai_chemistry_background(
                path,
                REDRAW.FIGURE_TYPE_COLORED,
            )

            with Image.open(path) as cleaned:
                rgb = cleaned.convert("RGB")
                self.assertEqual((255, 255, 255), rgb.getpixel((0, 0)))
                self.assertEqual((0, 0, 0), rgb.getpixel((10, 10)))
            self.assertEqual("cleaned", report["status"])
            self.assertGreater(report["background_pixels_normalized"], 300)

    def test_enclosed_light_gray_scientific_region_is_not_treated_as_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "enclosed-region.png"
            image = Image.new("RGB", (30, 30), "white")
            for x in range(9, 21):
                image.putpixel((x, 9), (0, 0, 0))
                image.putpixel((x, 20), (0, 0, 0))
            for y in range(9, 21):
                image.putpixel((9, y), (0, 0, 0))
                image.putpixel((20, y), (0, 0, 0))
            for y in range(10, 20):
                for x in range(10, 20):
                    image.putpixel((x, y), (240, 240, 240))
            image.save(path)

            REDRAW.clean_ai_chemistry_background(
                path,
                REDRAW.FIGURE_TYPE_SIMPLE,
            )

            with Image.open(path) as cleaned:
                self.assertEqual(
                    (240, 240, 240), cleaned.convert("RGB").getpixel((15, 15))
                )


if __name__ == "__main__":
    unittest.main()
