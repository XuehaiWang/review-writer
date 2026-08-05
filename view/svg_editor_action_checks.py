from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import serve_review_dashboard as dashboard


class SvgEditorResolutionChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.review_root = Path(self.temp_dir.name)
        self.project_id = "svg-resolution-check"
        self.project = self.review_root / "review-projects" / self.project_id
        candidate_dir = self.project / "02_section_drafting"
        candidate_dir.mkdir(parents=True)
        self.source_path = self.project / "source.png"
        source = Image.new("RGB", (2560, 2292), "white")
        draw = ImageDraw.Draw(source)
        draw.line((100, 100, 2460, 2192), fill="black", width=4)
        source.save(self.source_path)
        (candidate_dir / "figure_candidates.json").write_text(
            json.dumps(
                [
                    {
                        "figure_id": "P137-F01",
                        "source_image_path": str(self.source_path),
                    }
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_full_svg_reports_original_raster_dimensions(self) -> None:
        result = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
        )

        self.assertEqual((result["base_width"], result["base_height"]), (2560, 2292))
        svg = Path(result["full_svg"]).read_text(encoding="utf-8")
        self.assertIn('width="1600" height="1432"', svg)
        self.assertIn('data-original-width="2560" data-original-height="2292"', svg)

    def test_missing_redraw_selection_is_not_silently_replaced_with_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "selected AI redraw is unavailable"):
            dashboard.create_full_figure_svg(
                self.review_root,
                self.project_id,
                "P137-F01",
                base_mode="redrawn",
            )

    def test_orphaned_redraw_file_is_not_recovered_without_manifest_lineage(self) -> None:
        stage = self.project / "03_figure_redraw"
        redraw = stage / "redrawn" / "P137-F01.png"
        redraw.parent.mkdir(parents=True)
        Image.new("RGB", (900, 700), "white").save(redraw)
        (stage / "redrawn_figure_manifest.json").write_text(
            json.dumps({"project_id": self.project_id, "figures": [{"figure_id": "P137-F01", "status": "failed"}]}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "selected AI redraw is unavailable"):
            dashboard.create_full_figure_svg(
                self.review_root,
                self.project_id,
                "P137-F01",
                base_mode="redrawn",
            )

    def test_legacy_redraw_path_field_is_accepted_as_svg_base(self) -> None:
        stage = self.project / "03_figure_redraw"
        redraw = stage / "redrawn" / "legacy-output.png"
        redraw.parent.mkdir(parents=True)
        Image.new("RGB", (800, 600), "white").save(redraw)
        (stage / "redrawn_figure_manifest.json").write_text(
            json.dumps(
                {
                    "project_id": self.project_id,
                    "figures": [
                        {
                            "figure_id": "P137-F01",
                            "output_path": str(redraw),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
            base_mode="redrawn",
        )

        self.assertEqual(result["base_mode"], "redrawn")
        self.assertEqual(Path(result["base_image"]), redraw.resolve())
        self.assertEqual((result["base_width"], result["base_height"]), (800, 600))

    def test_cached_preview_sized_export_is_normalized_to_base_size(self) -> None:
        vector = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
        )
        full_svg = Path(vector["full_svg"]).read_text(encoding="utf-8")
        preview = Image.new("RGBA", (1600, 1432), "white")
        preview_bytes = io.BytesIO()
        preview.save(preview_bytes, format="PNG")

        result = dashboard.save_manual_arrow_edit(
            self.review_root,
            self.project_id,
            "P137-F01",
            preview_bytes.getvalue(),
            [{"type": "erase", "width": 8, "points": [{"x": 20, "y": 20}, {"x": 40, "y": 40}]}],
            full_vector_svg=full_svg,
        )

        with Image.open(result["redrawn_image"]) as saved:
            self.assertEqual(saved.size, (2560, 2292))
        audit_path = self.project / "03_figure_redraw" / "manual_arrow_edits" / "P137-F01.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["submitted_canvas_size"], [1600, 1432])
        self.assertEqual(audit["output_canvas_size"], [2560, 2292])
        self.assertTrue(audit["canvas_normalized_to_base"])

    def test_preview_normalization_is_general_for_portrait_and_wide_images(self) -> None:
        for base_size in ((2560, 3968), (5568, 3012), (5340, 5136)):
            with self.subTest(base_size=base_size):
                Image.new("RGB", base_size, "white").save(self.source_path)
                scale = dashboard.FULL_SVG_MAX_DIMENSION / max(base_size)
                preview_size = tuple(max(1, round(value * scale)) for value in base_size)
                preview = Image.new("RGBA", preview_size, "white")
                preview_bytes = io.BytesIO()
                preview.save(preview_bytes, format="PNG")
                full_svg = (
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{preview_size[0]}" '
                    f'height="{preview_size[1]}" viewBox="0 0 {preview_size[0]} {preview_size[1]}">'
                    '<g id="full-image-vector-trace"></g></svg>'
                )

                result = dashboard.save_manual_arrow_edit(
                    self.review_root,
                    self.project_id,
                    "P137-F01",
                    preview_bytes.getvalue(),
                    [{"type": "erase", "width": 8, "points": [{"x": 20, "y": 20}, {"x": 40, "y": 40}]}],
                    full_vector_svg=full_svg,
                )

                with Image.open(result["redrawn_image"]) as saved:
                    self.assertEqual(saved.size, base_size)

    def test_figures_page_has_no_shadowed_function_declarations(self) -> None:
        html = (Path(__file__).parent / "assets" / "dashboard" / "figures.html").read_text(
            encoding="utf-8"
        )
        import re

        names = re.findall(
            r"^\s*(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
            html,
            re.MULTILINE,
        )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual(duplicates, [])
        self.assertIn("$('projectSelect').addEventListener('change'", html)
        self.assertIn("event.target.closest('#openSvgEditor')", html)
        self.assertIn("loadProjects();", html)
        self.assertIn("assetRevision+=1", html)
        self.assertIn("&v=${assetRevision}", html)
        self.assertIn("{cache:'no-store'}", html)

    def test_orthogonal_arrow_adapts_to_initial_drag_axis(self) -> None:
        html = (Path(__file__).parent / "assets" / "dashboard" / "figures.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("function svgAdaptOrthogonalRoute", html)
        self.assertIn("Math.abs(dy)>Math.abs(dx)?'vertical-first':'horizontal-first'", html)
        self.assertIn(
            "orthogonalRoute==='vertical-first'?{x:start.x,y:end.y}:{x:end.x,y:start.y}",
            html,
        )
        self.assertIn("routePending:style==='orthogonal'", html)

    def test_mutable_project_files_disable_http_caching(self) -> None:
        source = Path(dashboard.__file__).read_text(encoding="utf-8")
        self.assertIn("self.send_file(path, ctype, no_store=True)", source)
        self.assertIn("def send_file(self, path: Path, content_type: str, *, no_store: bool = False)", source)


if __name__ == "__main__":
    unittest.main()
