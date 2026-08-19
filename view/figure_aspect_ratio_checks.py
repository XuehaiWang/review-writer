from __future__ import annotations

import base64
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "review-figure-style-redraw" / "scripts" / "redraw_figures.py"
SPEC = importlib.util.spec_from_file_location("review_writer_redraw_figures", MODULE_PATH)
assert SPEC and SPEC.loader
REDRAW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REDRAW)
OVERVIEW_MODULE_PATH = ROOT / "skills" / "review-figure-style-redraw" / "scripts" / "generate_overview_figure.py"
OVERVIEW_SPEC = importlib.util.spec_from_file_location("review_writer_generate_overview", OVERVIEW_MODULE_PATH)
assert OVERVIEW_SPEC and OVERVIEW_SPEC.loader
OVERVIEW = importlib.util.module_from_spec(OVERVIEW_SPEC)
OVERVIEW_SPEC.loader.exec_module(OVERVIEW)


class FigureAspectRatioChecks(unittest.TestCase):
    def test_overview_catalog_and_images_are_owned_by_the_redraw_skill(self) -> None:
        catalog = OVERVIEW.overview_template_catalog_path()
        self.assertEqual(catalog.parent.name, "overview-templates")
        self.assertIn("review-figure-style-redraw", catalog.parts)
        templates = OVERVIEW.read_json(catalog)
        self.assertEqual(len(templates), 10)
        for template in templates:
            image = OVERVIEW.resolve_overview_template_image(catalog, template)
            self.assertTrue(image.is_file(), image)
            self.assertEqual(image.parent, catalog.parent)

    def test_overview_template_path_cannot_escape_skill_assets(self) -> None:
        catalog = OVERVIEW.overview_template_catalog_path()
        with self.assertRaisesRegex(ValueError, "escapes"):
            OVERVIEW.resolve_overview_template_image(catalog, {"reference_image": "../outside.png"})

    def test_overview_reads_chat_completions_from_image_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"IMAGE_OPENAI_WIRE_API": "chat-completions"},
            clear=True,
        ):
            self.assertEqual(OVERVIEW.normalize_image_wire_api(), "chat-completions")

    def test_overview_uses_chat_completions_without_probing_images_routes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            reference = Path(raw) / "template.png"
            Image.new("RGB", (40, 24), "white").save(reference)
            expected = reference.read_bytes()
            metadata = {}
            with (
                mock.patch.object(OVERVIEW, "_try_chat_completions_image_edit", return_value=expected) as chat,
                mock.patch.object(OVERVIEW, "_try_images_edits", side_effect=AssertionError("wrong route")),
                mock.patch.object(
                    OVERVIEW,
                    "_try_images_generations_text_only",
                    side_effect=AssertionError("wrong route"),
                ),
            ):
                result = OVERVIEW.call_image_edit_api(
                    "image-key",
                    "https://www.micuapi.ai/v1",
                    reference,
                    "create overview",
                    "gpt-image-2",
                    wire_api="chat-completions",
                    request_metadata=metadata,
                )
            self.assertEqual(result, expected)
            chat.assert_called_once()
            self.assertEqual(metadata["endpoint"], "/chat/completions")
            self.assertEqual(metadata["wire_api"], "chat-completions")

    def test_overview_uses_internal_image_gateway_without_direct_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            reference = Path(raw) / "template.png"
            extra = Path(raw) / "skeleton.png"
            Image.new("RGB", (40, 24), "white").save(reference)
            Image.new("RGB", (20, 20), "black").save(extra)
            expected = reference.read_bytes()
            metadata = {}
            with (
                mock.patch.object(OVERVIEW, "image_gateway_configured", return_value=True),
                mock.patch.object(
                    OVERVIEW,
                    "call_gateway_image",
                    return_value=(expected, {"request_id": "gateway-request-1"}),
                ) as gateway,
                mock.patch.object(
                    OVERVIEW,
                    "_try_images_edits",
                    side_effect=AssertionError("direct provider route must not be used"),
                ),
            ):
                result = OVERVIEW.call_image_edit_api(
                    "",
                    "https://api.openai.com/v1",
                    reference,
                    "create overview",
                    "gpt-image-2",
                    request_metadata=metadata,
                    extra_images=[extra],
                )
            self.assertEqual(result, expected)
            gateway.assert_called_once()
            self.assertEqual(len(gateway.call_args.kwargs["images"]), 2)
            self.assertEqual(metadata["endpoint"], "internal-image-gateway")
            self.assertEqual(metadata["gateway_request_id"], "gateway-request-1")

    def test_overview_chat_request_embeds_the_selected_template(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            reference = Path(raw) / "template.png"
            Image.new("RGB", (40, 24), "white").save(reference)
            expected = reference.read_bytes()
            captured = {}

            def fake_open(request, timeout=600):
                captured["url"] = request.full_url
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                encoded = base64.b64encode(expected).decode("ascii")
                return {"choices": [{"message": {"content": f"data:image/png;base64,{encoded}"}}]}

            with mock.patch.object(OVERVIEW, "_open_chat_completion_request", side_effect=fake_open):
                result = OVERVIEW._try_chat_completions_image_edit(
                    "https://www.micuapi.ai/v1",
                    "image-key",
                    reference,
                    "create overview",
                    "gpt-image-2",
                )
            self.assertEqual(result, expected)
            self.assertEqual(captured["url"], "https://www.micuapi.ai/v1/chat/completions")
            self.assertTrue(captured["payload"]["stream"])
            image_url = captured["payload"]["messages"][0]["content"][1]["image_url"]["url"]
            self.assertEqual(base64.b64decode(image_url.split(",", 1)[1]), expected)

    def test_micu_primary_route_uses_the_dedicated_image_key_and_wire_api(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "IMAGE_OPENAI_API_KEY": "image-group-key",
                "OPENAI_API_KEY": "text-group-key",
                "IMAGE_OPENAI_WIRE_API": "chat-completions",
            },
            clear=True,
        ):
            key = REDRAW.resolve_api_key("", "https://www.micuapi.ai/v1")
            wire_api = REDRAW.default_wire_api()
        self.assertEqual(key, "image-group-key")
        self.assertEqual(wire_api, "chat-completions")

    def test_micu_fallback_config_reuses_the_existing_openai_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "IMAGE_FALLBACK_BASE_URL": "https://www.micuapi.ai/v1",
                "IMAGE_FALLBACK_WIRE_API": "chat-completions",
                "IMAGE_FALLBACK_MODEL": "gpt-image-2",
                "OPENAI_API_KEY": "test-only-key",
            },
            clear=True,
        ):
            config = REDRAW.image_fallback_config()
        self.assertEqual(config["base_url"], "https://www.micuapi.ai/v1")
        self.assertEqual(config["wire_api"], "chat-completions")
        self.assertEqual(config["model"], "gpt-image-2")
        self.assertEqual(config["api_key"], "test-only-key")

    def test_chat_completions_embeds_the_current_source_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_path = Path(raw) / "current-stage-6.png"
            Image.new("RGB", (23, 17), "white").save(source_path)
            captured = {}

            def fake_open(request, label, timeout=600):
                captured["url"] = request.full_url
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
                return {
                    "choices": [
                        {"message": {"content": f"data:image/png;base64,{encoded}"}}
                    ]
                }

            with mock.patch.object(REDRAW, "open_chat_completion_request", side_effect=fake_open):
                REDRAW.call_chat_completions_image_edit(
                    "test-only-key",
                    "https://www.micuapi.ai/v1",
                    source_path,
                    "preserve the chemistry",
                    "gpt-image-2",
                )

            self.assertEqual(captured["url"], "https://www.micuapi.ai/v1/chat/completions")
            payload = captured["payload"]
            self.assertEqual(payload["model"], "gpt-image-2")
            self.assertTrue(payload["stream"])
            content = payload["messages"][0]["content"]
            self.assertEqual(content[0]["text"], "preserve the chemistry")
            data_uri = content[1]["image_url"]["url"]
            self.assertEqual(base64.b64decode(data_uri.split(",", 1)[1]), source_path.read_bytes())

    def test_chat_completions_retries_successful_responses_without_an_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_path = Path(raw) / "source.png"
            Image.new("RGB", (17, 13), "white").save(source_path)
            encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
            responses = [
                {"choices": [{"message": {"content": "still processing"}, "finish_reason": "stop"}]},
                {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
                {"choices": [{"message": {"content": f"data:image/png;base64,{encoded}"}}]},
            ]

            with mock.patch.object(REDRAW, "open_chat_completion_request", side_effect=responses) as opened:
                with mock.patch.object(REDRAW.time, "sleep"):
                    result = REDRAW.call_chat_completions_image_edit(
                        "test-only-key",
                        "https://www.micuapi.ai/v1",
                        source_path,
                        "preserve the chemistry",
                        "gpt-image-2",
                    )

            self.assertEqual(opened.call_count, 3)
            request_modes = [
                json.loads(call.args[0].data.decode("utf-8"))["stream"]
                for call in opened.call_args_list
            ]
            self.assertEqual(request_modes, [True, True, False])
            self.assertIsNotNone(REDRAW.extract_chat_completion_image_reference(result))

    def test_chat_completions_no_image_error_includes_provider_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source_path = Path(raw) / "source.png"
            Image.new("RGB", (17, 13), "white").save(source_path)
            response = {
                "choices": [
                    {
                        "message": {"content": "The upstream channel returned text only."},
                        "finish_reason": "stop",
                    }
                ]
            }

            with mock.patch.object(REDRAW, "open_chat_completion_request", return_value=response):
                with mock.patch.object(REDRAW.time, "sleep"):
                    with self.assertRaisesRegex(RuntimeError, "provider_text=The upstream channel returned text only"):
                        REDRAW.call_chat_completions_image_edit(
                            "test-only-key",
                            "https://www.micuapi.ai/v1",
                            source_path,
                            "preserve the chemistry",
                            "gpt-image-2",
                        )

    def test_chat_completions_data_uri_is_saved_as_a_valid_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "generated-source.png"
            out_path = root / "redrawn.png"
            Image.new("RGB", (31, 19), "white").save(source_path)
            encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
            response = {
                "choices": [
                    {
                        "message": {
                            "content": f"![redrawn](data:image/png;base64,{encoded})",
                        }
                    }
                ]
            }
            REDRAW.save_chat_completion_redrawn_image(response, out_path)
            with Image.open(out_path) as saved:
                self.assertEqual(saved.size, (31, 19))

    def test_chat_completions_message_images_url_is_recognized(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "images": [{"image_url": {"url": "https://example.test/result.png"}}],
                    }
                }
            ]
        }
        self.assertEqual(
            REDRAW.extract_chat_completion_image_reference(response),
            ("url", "https://example.test/result.png"),
        )

    def test_all_channels_failed_uses_a_recovery_backoff(self) -> None:
        self.assertEqual(REDRAW.transient_retry_delay("HTTP 503: ALL_CHANNELS_FAILED", 0), 15)
        self.assertEqual(REDRAW.transient_retry_delay("HTTP 503: ALL_CHANNELS_FAILED", 1), 45)
        self.assertEqual(REDRAW.transient_retry_delay("HTTP 503: Service Unavailable", 0), 1)

    def test_provider_failover_only_handles_availability_errors(self) -> None:
        self.assertTrue(REDRAW.should_fail_over_image_provider(RuntimeError("HTTP 503 unavailable")))
        self.assertTrue(REDRAW.should_fail_over_image_provider(RuntimeError("ALL_CHANNELS_FAILED")))
        self.assertFalse(REDRAW.should_fail_over_image_provider(RuntimeError("HTTP 401 unauthorized")))
        self.assertFalse(REDRAW.should_fail_over_image_provider(RuntimeError("HTTP 400 invalid image")))

    def test_scientific_moderation_false_positive_is_recognized_without_matching_generic_400(self) -> None:
        self.assertTrue(
            REDRAW.is_safety_moderation_rejection(
                RuntimeError("HTTP 400: 内容被安全审核拦截 (疑似成人内容)")
            )
        )
        self.assertTrue(REDRAW.is_safety_moderation_rejection(RuntimeError("finish_reason=content_filter")))
        self.assertFalse(REDRAW.is_safety_moderation_rejection(RuntimeError("HTTP 400 invalid image size")))
        self.assertFalse(REDRAW.is_safety_moderation_rejection(RuntimeError("HTTP 401 unauthorized")))

    def test_moderation_false_positive_retries_once_with_concise_academic_prompt(self) -> None:
        prompts = []
        audit = []

        def requester(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                raise RuntimeError("HTTP 400: 内容被安全审核拦截 (疑似成人内容)")
            return {"data": [{"b64_json": "image"}]}

        safety_prompt = REDRAW.build_academic_chemistry_safety_retry_prompt(
            REDRAW.FIGURE_TYPE_MECHANISM,
            REDRAW.MECHANISM_ARROW_STRAIGHTEN_PROFILE,
        )
        result = REDRAW.call_with_academic_safety_retry(
            requester,
            "long original mechanism prompt",
            safety_prompt,
            audit,
        )

        self.assertEqual(result["data"][0]["b64_json"], "image")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[1], safety_prompt)
        self.assertIn("Academic chemistry diagram editing task", safety_prompt)
        self.assertIn("contains no people", safety_prompt)
        self.assertIn("Change only the shape of reaction-flow arrows", safety_prompt)
        self.assertLess(len(safety_prompt), 1500)
        self.assertEqual(audit[0]["status"], "succeeded")
        self.assertEqual(audit[0]["prompt_variant"], "academic-chemistry-safety-context")

    def test_moderation_retry_stops_after_the_single_safe_prompt_attempt(self) -> None:
        attempts = []
        audit = []

        def requester(prompt):
            attempts.append(prompt)
            raise RuntimeError("HTTP 400: safety review rejected")

        with self.assertRaisesRegex(RuntimeError, "academic-chemistry prompt retry also failed"):
            REDRAW.call_with_academic_safety_retry(
                requester,
                "original",
                "academic retry",
                audit,
            )

        self.assertEqual(attempts, ["original", "academic retry"])
        self.assertEqual(audit[0]["status"], "failed")

    def test_mechanism_prompt_preserves_chemistry_unicode_without_mojibake(self) -> None:
        prompt = REDRAW.build_mechanism_arrow_straighten_prompt({})

        self.assertIn("h\u03bd", prompt)
        self.assertIn("SN2\u2032 oxidative addition", prompt)
        for damaged in ("h\u8c13", "\u9225?", "\u951f", "\ufffd"):
            self.assertNotIn(damaged, prompt)
        self.assertEqual(prompt.encode("utf-8").decode("utf-8"), prompt)

        damaged_prompt = (
            prompt.replace("h\u03bd", "h\u8c13")
            .replace("SN2\u2032 oxidative addition", "SN2\u9225? oxidative addition")
            + " \u951f\ufffd"
        )
        repaired_prompt = REDRAW.repair_mechanism_prompt_text(damaged_prompt)
        self.assertIn("h\u03bd", repaired_prompt)
        self.assertIn("SN2\u2032 oxidative addition", repaired_prompt)
        for damaged in ("h\u8c13", "\u9225?", "\u951f", "\ufffd"):
            self.assertNotIn(damaged, repaired_prompt)

    def test_mechanism_prompt_repairs_missing_required_symbols_without_stopping(self) -> None:
        repaired = REDRAW.repair_mechanism_prompt_text("Preserve this technical mechanism diagram.")

        self.assertIn("h\u03bd", repaired)
        self.assertIn("SN2\u2032 oxidative addition", repaired)

    def test_later_mechanism_retry_error_keeps_last_generated_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_path = Path(raw) / "mechanism.png"
            Image.new("RGB", (40, 30), "white").save(out_path)
            fidelity = {
                "status": "failed",
                "failures": ["too much new or displaced ink appeared"],
            }
            row = {
                "figure_id": "P195-F01",
                "render_mode": "ai-edit",
                "edit_profile": REDRAW.MECHANISM_ARROW_STRAIGHTEN_PROFILE,
            }

            retained = REDRAW.retain_successful_mechanism_attempt_after_retry_error(
                row,
                out_path,
                [{"attempt": 1, "status": "failed", "failures": fidelity["failures"], "source_fidelity": fidelity}],
                RuntimeError("HTTP 400 reference image rejected"),
            )

            self.assertTrue(retained)
            self.assertEqual(row["status"], "redrawn")
            self.assertEqual(row["redrawn_image"], str(out_path))
            self.assertEqual(row["output_disposition"], "saved_with_integrity_warning")
            self.assertEqual(row["chemistry_integrity"]["status"], "failed")
            self.assertIn("Later retry error", row["notes"])

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

    def test_generated_content_in_padding_expands_crop_without_stretching(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.png"
            framed_path = root / "framed.png"
            output_path = root / "output.png"
            Image.new("RGB", (200, 80), "white").save(source_path)
            framing = REDRAW.prepare_aspect_preserving_edit_input(source_path, framed_path)

            generated = Image.new("RGB", (200, 200), "white")
            draw = ImageDraw.Draw(generated)
            draw.rectangle((35, 75, 165, 125), outline="black", width=4)
            # Simulate a provider moving a lower label into the technical
            # bottom padding, well beyond the antialiasing guard band.
            draw.rectangle((70, 170, 130, 185), fill="black")
            generated.save(output_path)

            result = REDRAW.normalize_generated_aspect(output_path, source_path, framing)

            self.assertTrue(result["padding_content"]["detected"])
            self.assertTrue(result["provider_canvas_allowed"])
            self.assertEqual(result["crop_mode"], "expanded_for_padding_content")
            self.assertGreater(result["normalized_size"][1], 80)
            with Image.open(output_path) as normalized:
                self.assertEqual(list(normalized.size), result["normalized_size"])
                self.assertLess(min(normalized.convert("L").crop((0, normalized.height - 30, normalized.width, normalized.height)).getextrema()), 64)

    def test_content_touching_padding_boundary_preserves_provider_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.png"
            framed_path = root / "framed.png"
            output_path = root / "output.png"
            Image.new("RGB", (200, 80), "white").save(source_path)
            framing = REDRAW.prepare_aspect_preserving_edit_input(source_path, framed_path)

            generated = Image.new("RGB", (200, 200), "white")
            draw = ImageDraw.Draw(generated)
            draw.rectangle((40, 65, 160, 135), outline="black", width=4)
            draw.rectangle((80, 0, 120, 12), fill="black")
            draw.rectangle((80, 187, 120, 199), fill="black")
            generated.save(output_path)

            result = REDRAW.normalize_generated_aspect(output_path, source_path, framing)

            self.assertEqual(result["crop_mode"], "provider_canvas_preserved_for_padding_content")
            self.assertEqual(result["crop_box"], [0, 0, 200, 200])
            self.assertEqual(result["normalized_size"], [200, 200])

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


class AiSkeletonRedrawGateChecks(unittest.TestCase):
    """The ai3d sanity gate must survive glossy highlight fragmentation."""

    BLUE = (0, 0, 220)

    def _blue_mask(self, image: Image.Image) -> Image.Image:
        small = image.resize(OVERVIEW._GATE_SMALL_SIZE)
        r_ch, g_ch, b_ch = small.split()
        rp, gp, bp = r_ch.load(), g_ch.load(), b_ch.load()
        blue = Image.new("L", small.size, 0)
        blp = blue.load()
        for y in range(small.size[1]):
            for x in range(small.size[0]):
                rv, gv, bv = rp[x, y], gp[x, y], bp[x, y]
                if bv >= rv + 25 and bv >= gv + 15 and bv > 90:
                    blp[x, y] = 255
        return blue

    def _sphere(self, center: tuple[int, int], radius: int = 50) -> Image.Image:
        image = Image.new("RGB", OVERVIEW._GATE_SMALL_SIZE, "white")
        draw = ImageDraw.Draw(image)
        cx, cy = center
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=self.BLUE)
        return image

    def test_highlight_band_fragments_are_clustered_into_one_sphere(self) -> None:
        image = self._sphere((225, 160))
        # A narrow specular band splits the sphere into two disconnected mask
        # blobs whose bounding boxes still interlock, like a real glossy render.
        ImageDraw.Draw(image).rectangle([0, 157, 449, 162], fill="white")
        stats = OVERVIEW._blob_stats(self._blue_mask(image), OVERVIEW._GATE_BLUE_MIN_PX)
        self.assertEqual(len(stats), 2)
        self.assertEqual(OVERVIEW._clustered_blob_count(stats, OVERVIEW._GATE_BLUE_MERGE_PX), 1)
        ok, note = OVERVIEW._ai_redraw_gate(image, 2, 1)
        self.assertTrue(ok, note)
        self.assertEqual(note, "gate_passed")

    def test_distant_extra_spheres_are_still_rejected(self) -> None:
        image = Image.new("RGB", OVERVIEW._GATE_SMALL_SIZE, "white")
        draw = ImageDraw.Draw(image)
        for cx, cy in ((80, 70), (225, 160), (370, 250)):
            draw.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], fill=self.BLUE)
        stats = OVERVIEW._blob_stats(self._blue_mask(image), OVERVIEW._GATE_BLUE_MIN_PX)
        self.assertEqual(OVERVIEW._clustered_blob_count(stats, OVERVIEW._GATE_BLUE_MERGE_PX), 3)
        # Tolerance allows |detected - expected| of 1, so three distant spheres
        # against an expected single R label exceed it and must be rejected.
        ok, note = OVERVIEW._ai_redraw_gate(image, 3, 1)
        self.assertFalse(ok)
        self.assertEqual(note, "r_labels_3_expected_1")

    def test_solid_sphere_count_is_unchanged(self) -> None:
        image = self._sphere((225, 160))
        stats = OVERVIEW._blob_stats(self._blue_mask(image), OVERVIEW._GATE_BLUE_MIN_PX)
        self.assertEqual(len(stats), 1)
        self.assertEqual(OVERVIEW._clustered_blob_count(stats, OVERVIEW._GATE_BLUE_MERGE_PX), 1)
        ok, note = OVERVIEW._ai_redraw_gate(image, 2, 1)
        self.assertTrue(ok, note)


if __name__ == "__main__":
    unittest.main()
