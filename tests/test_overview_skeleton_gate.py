from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
