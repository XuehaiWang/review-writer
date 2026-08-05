#!/usr/bin/env python3
"""Focused checks for local API provider settings."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_settings as settings


RELEVANT_ENV = {
    "MINERU_API_TOKEN": "",
    "OPENAI_BASE_URL": "",
    "OPENAI_API_KEY": "",
    "REVIEW_WRITING_BASE_URL": "",
    "REVIEW_WRITING_API_KEY": "",
    "REVIEW_WRITING_MODEL": "",
    "REVIEW_WRITING_WIRE_API": "",
    "REVIEW_CONCLUSION_API_KEY": "",
    "REVIEW_CONCLUSION_MODEL": "",
    "REVIEW_CONCLUSION_WIRE_API": "",
    "IMAGE_OPENAI_BASE_URL": "",
    "IMAGE_OPENAI_API_KEY": "",
    "IMAGE_OPENAI_MODEL": "",
    "IMAGE_OPENAI_WIRE_API": "",
}


class ProviderSettingsChecks(unittest.TestCase):
    def test_save_masks_secrets_and_applies_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, RELEVANT_ENV, clear=False):
            root = Path(raw)
            result = settings.save_provider_settings(
                root,
                {
                    "mineru": {"api_key": "mineru-secret-123456"},
                    "text": {
                        "base_url": "https://text.example/v1/",
                        "api_key": "text-secret-123456",
                        "model": "gpt-test-1",
                        "wire_api": "chat-completions",
                    },
                    "image": {
                        "base_url": "https://image.example/v1",
                        "api_key": "image-secret-123456",
                        "model": "gpt-image-test",
                        "wire_api": "images",
                    },
                },
            )
            serialized = json.dumps(result)
            self.assertNotIn("mineru-secret-123456", serialized)
            self.assertNotIn("text-secret-123456", serialized)
            self.assertNotIn("image-secret-123456", serialized)
            self.assertEqual(result["text"]["key_hint"], "••••3456")
            self.assertEqual(os.environ["REVIEW_WRITING_API_KEY"], "text-secret-123456")
            self.assertEqual(os.environ["REVIEW_CONCLUSION_API_KEY"], "text-secret-123456")
            self.assertEqual(os.environ["IMAGE_OPENAI_MODEL"], "gpt-image-test")
            path = settings.settings_path(root)
            self.assertTrue(path.is_file())
            stored = json.loads(path.read_text(encoding="utf-8"))["values"]
            self.assertEqual(stored["MINERU_API_TOKEN"], "mineru-secret-123456")

    def test_blank_keys_preserve_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, RELEVANT_ENV, clear=False):
            root = Path(raw)
            base = {
                "mineru": {"api_key": "mineru-existing"},
                "text": {"base_url": "https://text.example/v1", "api_key": "text-existing", "model": "gpt-a", "wire_api": "responses"},
                "image": {"base_url": "https://image.example/v1", "api_key": "image-existing", "model": "image-a", "wire_api": "chat-completions"},
            }
            settings.save_provider_settings(root, base)
            base["mineru"]["api_key"] = ""
            base["text"]["api_key"] = ""
            base["image"]["api_key"] = ""
            result = settings.save_provider_settings(root, base)
            self.assertTrue(result["mineru"]["key_configured"])
            self.assertEqual(os.environ["OPENAI_API_KEY"], "text-existing")
            self.assertEqual(os.environ["IMAGE_OPENAI_API_KEY"], "image-existing")

    def test_rejects_invalid_provider_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "complete HTTP or HTTPS URL"):
                settings.save_provider_settings(
                    Path(raw),
                    {
                        "mineru": {},
                        "text": {"base_url": "not-a-url", "wire_api": "responses"},
                        "image": {"base_url": "https://image.example/v1", "wire_api": "images"},
                    },
                )

    def test_settings_page_and_routes_are_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "view" / "assets" / "dashboard" / "settings.html").read_text(encoding="utf-8")
        server = (root / "view" / "serve_review_dashboard.py").read_text(encoding="utf-8")
        i18n = (root / "view" / "assets" / "dashboard" / "review-i18n.js").read_text(encoding="utf-8")
        self.assertIn("/api/settings", html)
        self.assertIn('parsed.path == "/api/settings"', server)
        self.assertIn("mountSettingsLink", i18n)
        self.assertIn("rw-settings-shortcut", i18n)
        self.assertIn('id="backToWorkspace"', html)
        self.assertIn("returnTarget()", html)
        self.assertIn("20260805c", html)


if __name__ == "__main__":
    unittest.main()
