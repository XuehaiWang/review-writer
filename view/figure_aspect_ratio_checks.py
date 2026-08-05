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
                return {"choices": [{"message": {"content": "unused"}}]}

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
