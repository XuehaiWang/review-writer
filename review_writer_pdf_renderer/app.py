from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "review-final-audit-release"
    / "scripts"
    / "render_modern_survey_pdf.py"
)
OUTPUTS = (
    "manuscript.pdf",
    "manuscript.tex",
    "manuscript_state.json",
    "render_manifest.json",
    "pdf_qa.json",
    "compile.log",
)
MAX_ASSET_BYTES = 40 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 160 * 1024 * 1024


class RenderAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=100)
    filename: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_base64: str = Field(min_length=1)


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_markdown: str = Field(min_length=1, max_length=8_000_000)
    language_profile: Literal["en", "zh-CN"]
    source_final_artifact_id: str = Field(min_length=1, max_length=100)
    source_release_artifact_id: str = Field(min_length=1, max_length=100)
    assets: list[RenderAsset] = Field(default_factory=list, max_length=500)


app = FastAPI(title="Review Writer PDF Renderer", docs_url=None, redoc_url=None)


def require_token(authorization: str) -> None:
    expected = str(os.environ.get("REVIEW_WRITER_PDF_RENDERER_TOKEN") or "")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid renderer token.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/render")
def render(
    payload: RenderRequest,
    authorization: str = Header(default=""),
) -> Response:
    require_token(authorization)
    with tempfile.TemporaryDirectory(prefix="review-writer-pdf-") as temporary:
        root = Path(temporary)
        inputs = root / "inputs"
        output = root / "output"
        inputs.mkdir()
        output.mkdir()
        artifact_paths: dict[str, str] = {}
        seen_artifact_ids: set[str] = set()
        total = 0
        for index, asset in enumerate(payload.assets):
            if asset.artifact_id in seen_artifact_ids:
                raise HTTPException(status_code=422, detail="Duplicate PDF asset id.")
            seen_artifact_ids.add(asset.artifact_id)
            try:
                raw = base64.b64decode(asset.data_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(status_code=422, detail="Invalid asset encoding.") from exc
            total += len(raw)
            if len(raw) > MAX_ASSET_BYTES or total > MAX_TOTAL_ASSET_BYTES:
                raise HTTPException(status_code=413, detail="PDF assets exceed renderer limits.")
            if hashlib.sha256(raw).hexdigest() != asset.sha256:
                raise HTTPException(status_code=422, detail="PDF asset hash mismatch.")
            suffix = Path(asset.filename).suffix.casefold()
            if suffix not in {".png", ".jpg", ".jpeg", ".pdf", ".svg"}:
                raise HTTPException(status_code=422, detail="Unsupported PDF asset type.")
            path = inputs / f"asset-{index:04d}{suffix}"
            path.write_bytes(raw)
            artifact_paths[asset.artifact_id] = str(path)
        input_json = root / "input.json"
        input_json.write_text(
            json.dumps(
                {
                    "final_markdown": payload.final_markdown,
                    "language_profile": payload.language_profile,
                    "source_final_artifact_id": payload.source_final_artifact_id,
                    "source_release_artifact_id": payload.source_release_artifact_id,
                    "artifact_paths": artifact_paths,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input-json",
                    str(input_json),
                    "--output-dir",
                    str(output),
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SOURCE_DATE_EPOCH": "1704067200",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15 * 60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="PDF compilation timed out.") from exc
        if result.returncode != 0:
            raise HTTPException(
                status_code=422,
                detail="PDF compilation failed: " + result.stdout[-6000:],
            )
        if any(not (output / name).is_file() for name in OUTPUTS):
            raise HTTPException(status_code=500, detail="PDF renderer output is incomplete.")
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(
            archive_bytes, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for name in OUTPUTS:
                archive.write(output / name, arcname=name)
        return Response(
            archive_bytes.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=render-bundle.zip"},
        )
