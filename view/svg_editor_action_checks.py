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
        state = dashboard.public_figure_redraw_states(self.review_root, self.project_id)["P137-F01"]
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["render_mode"], "manual-arrow-edit")
        self.assertTrue(state["preview_only"])
        self.assertEqual(state["error"], "")

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

    def test_verified_svg_content_crop_saves_with_the_cropped_canvas(self) -> None:
        vector = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
        )
        full_svg = Path(vector["full_svg"]).read_text(encoding="utf-8")
        cropped_svg = full_svg.replace(
            'data-original-width="2560" data-original-height="2292"',
            'data-original-width="1200" data-original-height="900" '
            'data-source-width="2560" data-source-height="2292" '
            'data-content-crop="true" data-crop-unit="source-px" '
            'data-crop-x="200" data-crop-y="100" '
            'data-crop-width="1200" data-crop-height="900"',
            1,
        )
        cropped = Image.new("RGBA", (1200, 900), "white")
        cropped_bytes = io.BytesIO()
        cropped.save(cropped_bytes, format="PNG")

        result = dashboard.save_manual_arrow_edit(
            self.review_root,
            self.project_id,
            "P137-F01",
            cropped_bytes.getvalue(),
            [],
            full_vector_svg=cropped_svg,
        )

        with Image.open(result["redrawn_image"]) as saved:
            self.assertEqual(saved.size, (1200, 900))
        audit_path = self.project / "03_figure_redraw" / "manual_arrow_edits" / "P137-F01.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["base_canvas_size"], [2560, 2292])
        self.assertEqual(audit["output_canvas_size"], [1200, 900])
        self.assertEqual(
            audit["canvas_crop"],
            {
                "status": "verified",
                "unit": "source-px",
                "x": 200,
                "y": 100,
                "width": 1200,
                "height": 900,
                "source_width": 2560,
                "source_height": 2292,
            },
        )
        manifest = json.loads(
            (self.project / "03_figure_redraw" / "redrawn_figure_manifest.json").read_text(encoding="utf-8")
        )
        row = manifest["figures"][0]
        self.assertEqual(row["aspect_ratio_policy"], "content_crop_allowed")
        self.assertTrue(dashboard.figure_aspect_policy_matches(row, row["aspect_ratio_integrity"]))
        approved = dashboard.approve_figure_for_manuscript(
            self.review_root,
            self.project_id,
            "P137-F01",
        )
        self.assertEqual(approved["human_approval"]["status"], "approved")
        approved_manifest = json.loads(
            (self.project / "03_figure_redraw" / "redrawn_figure_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            approved_manifest["figures"][0]["aspect_ratio_policy"],
            "content_crop_allowed",
        )

    def test_svg_content_crop_rejects_png_dimension_mismatch(self) -> None:
        vector = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
        )
        full_svg = Path(vector["full_svg"]).read_text(encoding="utf-8")
        invalid_svg = full_svg.replace(
            'data-original-width="2560" data-original-height="2292"',
            'data-original-width="1200" data-original-height="900" '
            'data-source-width="2560" data-source-height="2292" '
            'data-content-crop="true" data-crop-unit="source-px" '
            'data-crop-x="200" data-crop-y="100" '
            'data-crop-width="1200" data-crop-height="900"',
            1,
        )
        wrong_size = Image.new("RGBA", (1199, 900), "white")
        wrong_size_bytes = io.BytesIO()
        wrong_size.save(wrong_size_bytes, format="PNG")

        with self.assertRaisesRegex(ValueError, "does not match submitted PNG"):
            dashboard.save_manual_arrow_edit(
                self.review_root,
                self.project_id,
                "P137-F01",
                wrong_size_bytes.getvalue(),
                [],
                full_vector_svg=invalid_svg,
            )

    def test_reopened_cropped_svg_keeps_its_verified_crop_contract(self) -> None:
        vector = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
        )
        full_svg = Path(vector["full_svg"]).read_text(encoding="utf-8")
        cropped_svg = full_svg.replace(
            'data-original-width="2560" data-original-height="2292"',
            'data-original-width="1200" data-original-height="900" '
            'data-source-width="2560" data-source-height="2292" '
            'data-content-crop="true" data-crop-unit="source-px" '
            'data-crop-x="200" data-crop-y="100" '
            'data-crop-width="1200" data-crop-height="900"',
            1,
        )
        cropped = Image.new("RGBA", (1200, 900), "white")
        cropped_bytes = io.BytesIO()
        cropped.save(cropped_bytes, format="PNG")
        dashboard.save_manual_arrow_edit(
            self.review_root,
            self.project_id,
            "P137-F01",
            cropped_bytes.getvalue(),
            [],
            full_vector_svg=cropped_svg,
        )

        reopened = dashboard.create_full_figure_svg(
            self.review_root,
            self.project_id,
            "P137-F01",
            base_mode="redrawn",
        )
        self.assertEqual(reopened["vectorization"], "saved-manual-svg")
        reopened_svg = Path(reopened["full_svg"]).read_text(encoding="utf-8")
        dashboard.save_manual_arrow_edit(
            self.review_root,
            self.project_id,
            "P137-F01",
            cropped_bytes.getvalue(),
            [{"type": "line", "start": {"x": 10, "y": 10}, "end": {"x": 100, "y": 10}}],
            base_mode="redrawn",
            full_vector_svg=reopened_svg,
        )

        audit = json.loads(
            (self.project / "03_figure_redraw" / "manual_arrow_edits" / "P137-F01.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(audit["canvas_crop"]["source_width"], 2560)
        self.assertEqual(audit["canvas_crop"]["source_height"], 2292)
        self.assertEqual(audit["canvas_crop"]["width"], 1200)
        self.assertEqual(audit["canvas_crop"]["height"], 900)
        manifest = json.loads(
            (self.project / "03_figure_redraw" / "redrawn_figure_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["figures"][0]["aspect_ratio_policy"], "content_crop_allowed")

    def test_saved_manual_svg_ratio_warning_can_be_individually_human_approved(self) -> None:
        stage = self.project / "03_figure_redraw"
        output = stage / "redrawn" / "P137-F01-manual.png"
        svg_path = stage / "manual_arrow_edits" / "P137-F01.svg"
        audit_path = stage / "manual_arrow_edits" / "P137-F01.json"
        output.parent.mkdir(parents=True)
        svg_path.parent.mkdir(parents=True)
        Image.new("RGBA", (751, 200), "white").save(output)
        svg_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="751" height="200" '
            'viewBox="0 0 751 200" data-original-width="751" data-original-height="200">'
            '<g id="full-image-vector-trace"><path d="M 10 10 L 100 10"/></g></svg>',
            encoding="utf-8",
        )
        audit_path.write_text(
            json.dumps(
                {
                    "figure_id": "P137-F01",
                    "source_image": str(self.source_path),
                    "output_image": str(output),
                    "editable_svg": str(svg_path),
                    "output_canvas_size": [751, 200],
                }
            ),
            encoding="utf-8",
        )
        row = {
            "figure_id": "P137-F01",
            "source_image": str(self.source_path),
            "redrawn_image": str(output),
            "render_mode": "manual-arrow-edit",
            "status": "redrawn",
            "chemistry_integrity": {"status": "needs_human_arrow_check"},
            "aspect_ratio_policy": "source_ratio_required",
            "manual_arrow_edit": {
                "status": "saved",
                "audit_path": str(audit_path),
                "editable_svg": str(svg_path),
                "full_image_vector_trace": True,
            },
        }
        manifest_path = stage / "redrawn_figure_manifest.json"
        manifest_path.write_text(
            json.dumps({"project_id": self.project_id, "figures": [row]}),
            encoding="utf-8",
        )
        integrity = dashboard.figure_aspect_ratio_integrity(self.source_path, output)
        self.assertTrue(dashboard.manual_svg_canvas_review_eligible(stage, row, integrity, output))

        approved = dashboard.approve_figure_for_manuscript(
            self.review_root,
            self.project_id,
            "P137-F01",
        )

        self.assertTrue(approved["human_approval"]["manual_canvas_override"])
        approved_row = json.loads(manifest_path.read_text(encoding="utf-8"))["figures"][0]
        self.assertEqual(approved_row["aspect_ratio_policy"], "human_verified_manual_canvas")
        self.assertTrue(dashboard.figure_aspect_policy_matches(approved_row, integrity))

    def test_figures_page_exposes_crop_canvas_without_replacing_existing_tools(self) -> None:
        html = (Path(__file__).parent / "assets" / "dashboard" / "figures.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="svgCropCanvas"', html)
        self.assertIn('id="svgCropPadding"', html)
        self.assertIn("function svgCropCanvasToContent", html)
        self.assertIn("kind:'restore-canvas-crop'", html)
        self.assertIn('data-content-crop="${cropped?', html)
        for existing_control in (
            'id="svgDeleteSelected"',
            'id="svgUndo"',
            'id="svgOpenKetcher"',
            'id="svgDownload"',
            'id="svgSave"',
        ):
            self.assertIn(existing_control, html)

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
        self.assertIn("A current saved artifact supersedes an older AI failure", html)
        self.assertIn("label:'已编辑 · 待审核'", html)

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
        self.assertRegex(
            source,
            r"self\.send_file\(\s*path,\s*ctype,\s*no_store=True,",
        )
        self.assertRegex(
            source,
            r"def send_file\(\s*self,\s*path: Path,\s*content_type: str,\s*\*,\s*no_store: bool = False,",
        )


if __name__ == "__main__":
    unittest.main()
