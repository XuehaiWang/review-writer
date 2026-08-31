from __future__ import annotations

import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-figure-style-redraw"
    / "scripts"
    / "generate_overview_figure.py"
)
SPEC = importlib.util.spec_from_file_location("overview_skeleton_gate", SCRIPT)
overview = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = overview
SPEC.loader.exec_module(overview)


class OverviewSkeletonGateTests(unittest.TestCase):
    def test_gate_expectation_includes_rendered_hydrogen_spheres(self) -> None:
        # OCC#C* renders five non-H atoms plus three explicit white hydrogens.
        # The previous heavy-atom-only expectation (5) rejected correct AI
        # style transfers that visibly preserved all eight reference atoms.
        self.assertEqual((8, 1), overview.skeleton_atom_counts("OCC#C*"))

    def test_atom_blob_gate_accepts_small_segmentation_variation(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", overview._GATE_SMALL_SIZE, "white")
        draw = ImageDraw.Draw(image)
        for index in range(9):
            x = 28 + (index % 5) * 78
            y = 80 + (index // 5) * 145
            draw.ellipse((x - 13, y - 13, x + 13, y + 13), fill=(25, 25, 25))

        accepted, note = overview._ai_redraw_gate(image, 8, 0)

        self.assertTrue(accepted, note)

    def test_composite_appends_non_destructive_dock_when_no_panel_is_blank(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            figure_path = root / "overview.png"
            skeleton_path = root / "skeleton.png"

            # A deliberately dense figure has no near-white candidate region.
            figure = Image.new("RGB", (320, 240), (31, 62, 101))
            draw = ImageDraw.Draw(figure)
            for x in range(0, 320, 20):
                draw.rectangle((x, 0, min(319, x + 9), 239), fill=(146, 72, 91))
            original_pixels = figure.tobytes()
            figure.save(figure_path)

            skeleton = Image.new("RGB", (120, 180), "white")
            sk_draw = ImageDraw.Draw(skeleton)
            sk_draw.ellipse((35, 10, 85, 60), fill=(20, 20, 20))
            sk_draw.line((60, 60, 60, 150), fill=(20, 20, 20), width=8)
            sk_draw.ellipse((35, 125, 85, 175), fill=(190, 30, 30))
            skeleton.save(skeleton_path)

            ok, reason, panel_source = overview.composite_skeleton_into_figure(
                figure_path, skeleton_path, "uncalibrated-layout"
            )

            self.assertTrue(ok, reason)
            self.assertEqual("appended-dock", panel_source)
            with Image.open(figure_path) as result:
                self.assertEqual((640, 240), result.size)
                self.assertEqual(
                    original_pixels,
                    result.crop((0, 0, 320, 240)).convert("RGB").tobytes(),
                )

    def test_composite_prefers_detected_panel_over_stale_calibration(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            figure_path = root / "overview.png"
            skeleton_path = root / "skeleton.png"

            # Reproduce the drifted vertical layout: its real structure panel is
            # wider and higher than the old calibrated (10, 99)-(126, 493) box.
            figure = Image.new("RGB", (639, 621), (220, 226, 232))
            draw = ImageDraw.Draw(figure)
            real_panel = (27, 57, 202, 376)
            draw.rectangle(real_panel, fill="white", outline=(35, 62, 95), width=2)
            # Other pale cards make whiteness alone insufficient to distinguish
            # a stale calibrated rectangle from the intentionally blank panel.
            for y in (100, 183, 266, 349):
                draw.rectangle((250, y, 610, y + 65), fill=(242, 244, 246),
                               outline=(80, 110, 135), width=2)
                draw.line((270, y + 18, 585, y + 18), fill=(50, 70, 90), width=5)
            figure.save(figure_path)

            skeleton = Image.new("RGB", (160, 260), "white")
            sk_draw = ImageDraw.Draw(skeleton)
            sk_draw.ellipse((55, 15, 105, 65), fill=(30, 30, 30))
            sk_draw.line((80, 65, 80, 205), fill=(30, 30, 30), width=10)
            sk_draw.ellipse((55, 195, 105, 245), fill=(180, 35, 35))
            skeleton.save(skeleton_path)

            ok, reason, panel_source = overview.composite_skeleton_into_figure(
                figure_path, skeleton_path, "module-cards-crosscut-sidebar"
            )

            self.assertTrue(ok, reason)
            self.assertEqual("auto-detected", panel_source)
            with Image.open(figure_path) as result:
                detected = overview.detect_blank_panel(figure)
                self.assertIsNotNone(detected)
                assert detected is not None
                # Exclude the panel border and measure only the pasted molecule;
                # card strokes elsewhere cannot affect this content box.
                inner_panel = (
                    real_panel[0] + 3,
                    real_panel[1] + 3,
                    real_panel[2] - 3,
                    real_panel[3] - 3,
                )
                panel_ink = result.crop(inner_panel).convert("L").point(
                    lambda p: 255 if p < 220 else 0
                )
                content = overview._skeleton_content_bbox(panel_ink)
                self.assertIsNotNone(content)
                assert content is not None
                molecule_center = (
                    inner_panel[0] + (content[0] + content[2]) / 2,
                    inner_panel[1] + (content[1] + content[3]) / 2,
                )
                target_center = (
                    (detected[0] + detected[2]) / 2,
                    (detected[1] + detected[3]) / 2,
                )
                self.assertLess(math.dist(molecule_center, target_center), 12)

    def test_composite_uses_verified_square_layout_panel_and_centers_content(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            figure_path = root / "overview.png"
            skeleton_path = root / "skeleton.png"

            figure = Image.new("RGB", (1024, 1024), (237, 240, 242))
            draw = ImageDraw.Draw(figure)
            draw.rounded_rectangle(
                (20, 118, 399, 476),
                radius=18,
                fill="white",
                outline=(186, 194, 202),
                width=2,
            )
            # Pale cards are intentionally close to white; the calibrated
            # panel must remain the neutral left panel rather than these cards.
            for x, color in ((424, (252, 237, 228)), (595, (250, 244, 224))):
                draw.rounded_rectangle(
                    (x, 170, x + 150, 370), radius=12, fill=color
                )
            figure.save(figure_path)

            skeleton = Image.new("RGB", (300, 120), "white")
            sk_draw = ImageDraw.Draw(skeleton)
            sk_draw.rounded_rectangle((20, 45, 280, 75), radius=14, fill=(210, 20, 20))
            skeleton.save(skeleton_path)

            ok, reason, panel_source = overview.composite_skeleton_into_figure(
                figure_path, skeleton_path, "module-cards-crosscut-sidebar"
            )

            self.assertTrue(ok, reason)
            self.assertEqual("calibrated", panel_source)
            with Image.open(figure_path) as result:
                red_pixels = [
                    (x, y)
                    for y in range(result.height)
                    for x in range(result.width)
                    if (
                        (pixel := result.getpixel((x, y)))[0] > 170
                        and pixel[0] > pixel[1] * 2
                        and pixel[0] > pixel[2] * 2
                    )
                ]
                self.assertTrue(red_pixels)
                red_center_x = sum(x for x, _y in red_pixels) / len(red_pixels)
                expected_panel_center_x = (20 + 399) / 2
                self.assertLess(abs(red_center_x - expected_panel_center_x), 4)
                self.assertGreater(min(y for _x, y in red_pixels), 160)
                self.assertLess(max(y for _x, y in red_pixels), 465)

    def test_composite_uses_compact_left_panel_instead_of_blank_evidence_table(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            figure_path = root / "overview.png"
            skeleton_path = root / "skeleton.png"

            figure = Image.new("RGB", (1024, 1024), "white")
            draw = ImageDraw.Draw(figure)
            # Match the compact provider variant from the reported regression.
            draw.rounded_rectangle(
                (30, 143, 389, 394),
                radius=18,
                fill="white",
                outline=(186, 194, 202),
                width=2,
            )
            # Heading/caption make the older taller calibration fail.
            draw.rectangle((70, 88, 350, 128), fill=(18, 49, 94))
            draw.rectangle((150, 405, 280, 430), fill=(18, 49, 94))
            # A large neutral blank table on the right must never win merely
            # because its whitespace is larger than the structure panel.
            draw.rounded_rectangle(
                (520, 145, 840, 425),
                radius=14,
                fill="white",
                outline=(205, 210, 216),
                width=2,
            )
            figure.save(figure_path)

            skeleton = Image.new("RGB", (300, 120), "white")
            sk_draw = ImageDraw.Draw(skeleton)
            sk_draw.rounded_rectangle((20, 45, 280, 75), radius=14, fill=(210, 20, 20))
            skeleton.save(skeleton_path)

            ok, reason, panel_source = overview.composite_skeleton_into_figure(
                figure_path, skeleton_path, "module-cards-crosscut-sidebar"
            )

            self.assertTrue(ok, reason)
            self.assertEqual("calibrated", panel_source)
            with Image.open(figure_path) as result:
                red_pixels = [
                    (x, y)
                    for y in range(result.height)
                    for x in range(result.width)
                    if (
                        (pixel := result.getpixel((x, y)))[0] > 170
                        and pixel[0] > pixel[1] * 2
                        and pixel[0] > pixel[2] * 2
                    )
                ]
                self.assertTrue(red_pixels)
                red_center_x = sum(x for x, _y in red_pixels) / len(red_pixels)
                red_center_y = sum(y for _x, y in red_pixels) / len(red_pixels)
                self.assertLess(abs(red_center_x - (30 + 389) / 2), 4)
                self.assertGreater(red_center_y, 190)
                self.assertLess(red_center_y, 380)
                self.assertLess(max(x for x, _y in red_pixels), 389)
                # The compositor must not add a second inset frame around the
                # molecule; this point lies on the old synthetic frame path.
                self.assertEqual((255, 255, 255), result.getpixel((37, 150)))

    def test_module_cards_auto_detection_rejects_right_hand_table(self) -> None:
        self.assertFalse(
            overview._panel_matches_layout_zone(
                "module-cards-crosscut-sidebar", (520, 145, 840, 425), 1024, 1024
            )
        )
        self.assertFalse(
            overview._panel_matches_layout_zone(
                "module-cards-crosscut-sidebar", (214, 128, 384, 442), 1024, 1024
            )
        )
        self.assertTrue(
            overview._panel_matches_layout_zone(
                "module-cards-crosscut-sidebar", (30, 143, 389, 394), 1024, 1024
            )
        )

    def test_pastel_card_is_not_a_neutral_structure_panel(self) -> None:
        from PIL import Image

        image = Image.new("RGB", (400, 300), (252, 236, 226))
        self.assertGreater(overview._panel_whiteness(image, (20, 20, 380, 280)), 0.95)
        self.assertLess(overview._panel_neutrality(image, (20, 20, 380, 280)), 0.1)

    def test_programmatic_skeleton_png_has_transparent_background(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "skeleton.png"
            rendered = overview.render_smiles_ball_and_stick(
                "CCO", output_path, img_size=(420, 300)
            )
            self.assertEqual(output_path, rendered)
            with Image.open(output_path) as image:
                self.assertEqual("RGBA", image.mode)
                self.assertEqual((0, 255), image.getchannel("A").getextrema())

    def test_legacy_white_skeleton_background_becomes_transparent(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (120, 80), "white")
        ImageDraw.Draw(image).ellipse((40, 20, 80, 60), fill=(20, 40, 80))
        rgba = overview._skeleton_rgba(image)

        self.assertEqual(0, rgba.getpixel((0, 0))[3])
        self.assertEqual(255, rgba.getpixel((60, 40))[3])

    def test_wide_skeleton_rotates_to_fit_portrait_panel(self) -> None:
        from PIL import Image, ImageDraw

        layer = Image.new("RGBA", (600, 100), (255, 255, 255, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (20, 20, 580, 80), radius=25, fill=(20, 40, 80, 255)
        )
        fitted, rotated = overview._fit_skeleton_layer(layer, 220, 300)

        self.assertTrue(rotated)
        self.assertLessEqual(fitted.width, 220)
        self.assertLessEqual(fitted.height, 300)
        self.assertGreater(fitted.height, fitted.width)


if __name__ == "__main__":
    unittest.main()
