from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "skills"
    / "mineru-precise-parse-review-writer"
    / "scripts"
    / "parse_review_writer_pdfs.py"
)


def load_mineru_client():
    name = "review_writer_test_mineru_http_errors"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load MinerU client: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ErrorResponse:
    status_code = 422

    def __init__(self, payload: dict):
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        raise AssertionError("The MinerU client must preserve the response before raising.")


class PostSession:
    def __init__(self, response):
        self.response = response

    def post(self, *_args, **_kwargs):
        return self.response


class MinerUHttpErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = load_mineru_client()

    def test_batch_request_preserves_mineru_code_message_and_trace_id(self) -> None:
        response = ErrorResponse(
            {
                "code": -10002,
                "msg": "request parameter error",
                "trace_id": "trace-422",
            }
        )

        with self.assertRaises(RuntimeError) as raised:
            self.client.api_post_json(
                PostSession(response),
                "secret-token",
                "/api/v4/file-urls/batch",
                {"files": [{"name": "paper.pdf"}]},
            )

        message = str(raised.exception)
        self.assertIn("MinerU HTTP 422", message)
        self.assertIn("code=-10002", message)
        self.assertIn("request parameter error", message)
        self.assertIn("trace_id=trace-422", message)
        self.assertNotIn("secret-token", message)

    def test_signed_upload_preserves_storage_error_body(self) -> None:
        response = ErrorResponse(
            {
                "Code": "InvalidArgument",
                "Message": "The request body is invalid.",
                "RequestId": "oss-request-422",
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            pdf = Path(raw) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 600)
            job = SimpleNamespace(pdf_path=pdf, relative_pdf_path="paper.pdf")

            with patch.object(self.client.requests, "put", return_value=response):
                with self.assertRaises(RuntimeError) as raised:
                    self.client.upload_batch_files(
                        [job],
                        ["https://signed-upload.example/path?secret=query"],
                        retries=0,
                        retry_delay=0,
                    )

        message = str(raised.exception)
        self.assertIn("MinerU HTTP 422", message)
        self.assertIn("InvalidArgument", message)
        self.assertIn("The request body is invalid.", message)
        self.assertIn("oss-request-422", message)
        self.assertNotIn("signed-upload.example", message)


if __name__ == "__main__":
    unittest.main()
