from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "skills"
    / "review-figure-style-redraw"
    / "scripts"
    / "generate_overview_figure.py"
)
SPEC = importlib.util.spec_from_file_location("review_overview_figure_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
overview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overview)


class OverviewFigureHelperTests(unittest.TestCase):
    def test_panel_refinement_is_bounded_to_requested_error_budget(self) -> None:
        from PIL import Image

        image = Image.new("RGB", (120, 120), "white")
        refined = overview._refine_panel_box(
            image,
            (40, 40, 80, 80),
            max_dx=12,
            max_dy=12,
            whiteness_threshold=0.85,
        )
        self.assertEqual((28, 28, 92, 92), refined)

    def test_skeleton_bbox_discards_a_tiny_distant_raster_speck(self) -> None:
        from PIL import Image, ImageDraw

        mask = Image.new("L", (100, 100), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((35, 35, 65, 65), fill=255)
        draw.rectangle((2, 2, 4, 4), fill=255)

        self.assertEqual((35, 35, 66, 66), overview._skeleton_content_bbox(mask))

    def test_symbol_allowlist_is_derived_from_project_categories(self) -> None:
        symbols = overview._approved_figure_symbols(
            {
                "taxonomy_profile": "chemistry_general",
                "metal_categories": ["Cu", "Organocatalysis", "Ni catalyst", "Fe"],
            }
        )

        self.assertEqual(["Cu", "Fe", "ee", "R1", "R2", "R3", "R4"], symbols)
        self.assertNotIn("Pd", symbols)
        self.assertNotIn("Ni", symbols)

    def test_existing_integrity_fallbacks_and_blueprint_contract_remain(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('skeleton_source = "programmatic_fallback"', source)
        self.assertIn('return True, "", "appended-dock"', source)
        self.assertIn('"overview_axis_contract": {}', source)
        self.assertIn("visible = len(atoms)", source)
        self.assertIn("output_path.stat().st_size", source)


if __name__ == "__main__":
    unittest.main()
