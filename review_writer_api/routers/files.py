"""Authenticated immutable artifact streaming endpoints."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.errors import ArtifactRangeNotSatisfiable
from review_writer_api.security import Principal


CHUNK_SIZE = 64 * 1024


def _byte_range(value: str, size: int) -> tuple[int, int]:
    raw = str(value or "").strip()
    if not raw.startswith("bytes=") or "," in raw:
        raise ArtifactRangeNotSatisfiable("Only one byte range is supported.")
    specification = raw[6:].strip()
    if "-" not in specification:
        raise ArtifactRangeNotSatisfiable("The byte range is invalid.")
    start_text, end_text = specification.split("-", 1)
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start = max(0, size - suffix_length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise ArtifactRangeNotSatisfiable("The byte range is invalid.") from exc
    if size <= 0 or start < 0 or start >= size or end < start:
        raise ArtifactRangeNotSatisfiable("The byte range is outside the artifact.")
    return start, min(end, size - 1)


def _read_range(path: Path, start: int, length: int) -> Iterator[bytes]:
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def build_file_router(
    principal_dependency: Callable[..., Principal], artifact_service: ArtifactService
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

    @router.get("/{artifact_id}/content")
    def artifact_content(
        artifact_id: str,
        request: Request,
        principal: Principal = Depends(principal_dependency),
    ):
        resolved = artifact_service.resolve_owned_artifact(principal.user_id, artifact_id)
        artifact = resolved.artifact
        media_type = mimetypes.guess_type(resolved.path.name)[0] or "application/octet-stream"
        common_headers = {
            "Accept-Ranges": "bytes",
            "ETag": f'"{artifact.content_sha256}"',
            "Cache-Control": "private, no-cache",
        }
        requested_range = request.headers.get("Range")
        if not requested_range:
            return FileResponse(
                resolved.path,
                media_type=media_type,
                filename=resolved.path.name,
                headers=common_headers,
            )

        try:
            start, end = _byte_range(requested_range, artifact.size_bytes)
        except ArtifactRangeNotSatisfiable as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.payload(),
                headers={
                    **common_headers,
                    "Content-Range": f"bytes */{artifact.size_bytes}",
                },
            )
        length = end - start + 1
        headers = {
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{artifact.size_bytes}",
            "Content-Length": str(length),
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(resolved.path.name, safe="")
            ),
        }
        return StreamingResponse(
            _read_range(resolved.path, start, length),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers=headers,
        )

    return router
