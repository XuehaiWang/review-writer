from __future__ import annotations

import importlib.util
import io
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "review-metadata-prep" / "scripts" / "prepare_metadata.py"
SPEC = importlib.util.spec_from_file_location("prepare_metadata_provider_checks", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProviderResponseChecks(unittest.TestCase):
    def test_valid_json_object_is_returned(self) -> None:
        self.assertEqual(MODULE.decode_json_object(b'{"ok": true}', "test"), {"ok": True})

    def test_empty_response_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "empty response"):
            MODULE.decode_json_object(b"", "test")

    def test_non_json_response_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-JSON content"):
            MODULE.decode_json_object(b"<html>bad gateway</html>", "test")

    def test_non_transient_http_error_is_not_hidden_as_json_error(self) -> None:
        request = MODULE.urllib.request.Request("https://example.invalid/v1/responses")
        error = urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"invalid key"}'),
        )
        with patch.object(MODULE.urllib.request, "urlopen", side_effect=error) as urlopen:
            with self.assertRaisesRegex(RuntimeError, r"HTTP 401.*invalid key"):
                MODULE.open_json_request(request, timeout=1, context="Metadata model request")
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
