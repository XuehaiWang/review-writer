from __future__ import annotations

import hashlib
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from review_writer_pdf_renderer.app import app


ROOT = Path(__file__).resolve().parents[1]
RENDER_SCRIPT_PATH = (
    ROOT
    / "skills"
    / "review-final-audit-release"
    / "scripts"
    / "render_modern_survey_pdf.py"
)
RENDER_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "review_writer_pdf_qa_script", RENDER_SCRIPT_PATH
)
assert RENDER_SCRIPT_SPEC is not None and RENDER_SCRIPT_SPEC.loader is not None
RENDER_SCRIPT = importlib.util.module_from_spec(RENDER_SCRIPT_SPEC)
RENDER_SCRIPT_SPEC.loader.exec_module(RENDER_SCRIPT)


class PdfRendererContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"REVIEW_WRITER_PDF_RENDERER_TOKEN": "test-renderer-token"},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(app)

    @staticmethod
    def payload() -> dict:
        return {
            "final_markdown": "# Review\n\nEvidence-grounded synthesis.",
            "language_profile": "en",
            "source_final_artifact_id": "final-id",
            "source_release_artifact_id": "release-id",
            "assets": [],
        }

    def test_render_requires_internal_bearer_token(self) -> None:
        response = self.client.post("/render", json=self.payload())
        self.assertEqual(401, response.status_code)

    def test_duplicate_asset_ids_are_rejected_before_compilation(self) -> None:
        payload = self.payload()
        asset = {
            "artifact_id": "figure-id",
            "filename": "figure.png",
            "sha256": hashlib.sha256(b"\x00").hexdigest(),
            "data_base64": "AA==",
        }
        payload["assets"] = [asset, asset]
        response = self.client.post(
            "/render",
            json=payload,
            headers={"Authorization": "Bearer test-renderer-token"},
        )
        self.assertEqual(422, response.status_code)
        self.assertIn("Duplicate PDF asset id", response.text)

    def test_invalid_base64_is_rejected_before_compilation(self) -> None:
        payload = self.payload()
        payload["assets"] = [
            {
                "artifact_id": "figure-id",
                "filename": "figure.png",
                "sha256": "0" * 64,
                "data_base64": "not-base64!",
            }
        ]
        response = self.client.post(
            "/render",
            json=payload,
            headers={"Authorization": "Bearer test-renderer-token"},
        )
        self.assertEqual(422, response.status_code)

    def test_container_installs_ctex_lualatex_runtime(self) -> None:
        dockerfile = (ROOT / "Dockerfile.pdf-renderer").read_text(encoding="utf-8")
        self.assertIn("texlive-lang-chinese", dockerfile)
        self.assertIn("texlive-lang-japanese", dockerfile)

    def test_pdf_qa_blocks_internal_workflow_marker_leaks(self) -> None:
        class MediaBox:
            width = 595.28
            height = 841.89

        class Page:
            mediabox = MediaBox()

            @staticmethod
            def extract_text() -> str:
                return "Visible paragraph_id and inserted_figure metadata."

            @staticmethod
            def get(_key: str, default=None):
                return default

        class Reader:
            pages = [Page()]

        with patch.object(RENDER_SCRIPT, "PdfReader", return_value=Reader()):
            qa = RENDER_SCRIPT.inspect_pdf(
                Path("unused.pdf"),
                "",
                {"validation": {"warning_issues": []}},
            )

        issue = next(
            item
            for item in qa["blocking_issues"]
            if item["type"] == "internal_workflow_marker"
        )
        self.assertEqual(["inserted_figure", "paragraph_id"], issue["markers"])


if __name__ == "__main__":
    unittest.main()
