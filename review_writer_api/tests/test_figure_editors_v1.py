from __future__ import annotations

import base64
import io

from fastapi.testclient import TestClient
from PIL import Image

from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase


def png_data_url(size: tuple[int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class FigureEditorsV1Tests(NativeFigureApiTestCase):
    def test_full_svg_is_an_immutable_same_origin_artifact_without_embedded_raster(self) -> None:
        with TestClient(self.app) as client:
            self.confirm_review(client)
            response = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/full-svg",
                json={"base_mode": "source"},
                headers=self.headers(),
            )
            self.assertEqual(200, response.status_code, response.text)
            payload = response.json()
            svg = client.get(payload["full_svg_url"]).text
        self.assertIn("full-image-vector-trace", svg)
        self.assertNotIn("<image", svg.lower())
        self.assertTrue(payload["full_svg_url"].startswith("/api/v1/artifacts/"))

    def test_manual_svg_crop_saves_and_can_receive_explicit_canvas_approval(self) -> None:
        cropped_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            'viewBox="0 0 10 10" data-original-width="10" data-original-height="10" '
            'data-source-width="20" data-source-height="10" data-content-crop="true" '
            'data-crop-unit="source-px" data-crop-x="0" data-crop-y="0" '
            'data-crop-width="10" data-crop-height="10">'
            '<title>edited</title><g id="full-image-vector-trace"><path d="M0 0h1"/></g>'
            '<g data-vector-kind="ketcher-structure" data-ketcher-ket="e30="/></svg>'
        )
        with TestClient(self.app) as client:
            self.confirm_review(client)
            saved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/manual-edit",
                json={
                    "image_png_data_url": png_data_url((10, 10)),
                    "operations": [{"type": "crop"}],
                    "base_mode": "source",
                    "editable_svg": cropped_svg,
                    "full_vector_svg": cropped_svg,
                },
                headers=self.headers(),
            )
            self.assertEqual(200, saved.status_code, saved.text)
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
            row = payload["redrawn_manifest"]["figures"][0]
            svg_content = client.get(row["editable_svg_url"]).text
            approved = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/approve",
                headers=self.headers(),
            )
            after = client.get(
                f"/api/v1/projects/{self.project_id}/figures"
            ).json()
        self.assertEqual("verified", row["manual_edit"]["canvas_crop"]["status"])
        self.assertEqual(0, payload["freshness"]["usable_count"])
        self.assertIn('data-vector-kind="ketcher-structure"', svg_content)
        self.assertTrue(row["editable_svg_url"].startswith("/api/v1/artifacts/"))
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertTrue(
            after["redrawn_manifest"]["figures"][0]["human_approval"][
                "manual_canvas_override"
            ]
        )

    def test_manual_canvas_mismatch_without_verified_crop_is_rejected(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            'viewBox="0 0 10 10"><g id="full-image-vector-trace"/></svg>'
        )
        with TestClient(self.app) as client:
            self.confirm_review(client)
            response = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/manual-edit",
                json={
                    "image_png_data_url": png_data_url((10, 10)),
                    "operations": [],
                    "base_mode": "source",
                    "editable_svg": svg,
                    "full_vector_svg": svg,
                },
                headers=self.headers(),
            )
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("FIGURE_CANVAS_MISMATCH", response.json()["error"]["code"])

    def test_manual_svg_rejects_active_or_external_css(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" '
            'viewBox="0 0 20 10"><style>@import url(https://evil.invalid/x.css)</style>'
            '<g id="full-image-vector-trace"/></svg>'
        )
        with TestClient(self.app) as client:
            self.confirm_review(client)
            response = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/manual-edit",
                json={
                    "image_png_data_url": png_data_url((20, 10)),
                    "operations": [],
                    "base_mode": "source",
                    "editable_svg": svg,
                    "full_vector_svg": svg,
                },
                headers=self.headers(),
            )
        self.assertEqual(422, response.status_code, response.text)
        self.assertIn("unsafe style", response.json()["error"]["message"].lower())

    def test_manual_save_creates_new_artifact_revision_without_overwriting_previous(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" '
            'viewBox="0 0 20 10"><g id="full-image-vector-trace"/></svg>'
        )
        body = {
            "image_png_data_url": png_data_url((20, 10)),
            "operations": [{"type": "arrow", "points": [{"x": 1, "y": 1}, {"x": 2, "y": 2}]}],
            "base_mode": "source",
            "editable_svg": svg,
            "full_vector_svg": svg,
        }
        with TestClient(self.app) as client:
            self.confirm_review(client)
            first = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/manual-edit",
                json=body,
                headers=self.headers(),
            )
            self.assertEqual(200, first.status_code, first.text)
            body["operations"].append({"type": "line"})
            second = client.post(
                f"/api/v1/projects/{self.project_id}/figures/P001-F02/manual-edit",
                json=body,
                headers=self.headers(),
            )
        self.assertEqual(200, second.status_code, second.text)
        self.assertNotEqual(
            first.json()["audit_artifact_id"], second.json()["audit_artifact_id"]
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
