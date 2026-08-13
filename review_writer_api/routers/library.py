"""Native, versioned Library routes."""

from __future__ import annotations

import asyncio
import mimetypes
import uuid
import anyio
from collections.abc import Callable, Mapping
from contextlib import suppress
from functools import partial
from threading import Event
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from review_writer_api.domain_services.library import (
    LibraryPaperRecord,
    LibraryService,
    MinerUPreciseParseFailed,
)
from review_writer_api.errors import ArtifactRangeNotSatisfiable, WorkflowValidationError
from review_writer_api.job_service import JobService
from review_writer_api.routers.files import _byte_range, _read_range
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    LiteratureDownloadRequest,
    LiteratureSearchRequest,
)


def _paper_payload(record: LibraryPaperRecord) -> dict[str, Any]:
    metadata = record.metadata
    value = lambda key, default=None: (
        metadata.get(key, {}).get("value", default)
        if isinstance(metadata.get(key), dict)
        else metadata.get(key, default)
    )
    return {
        "id": record.id,
        "paper_id": record.paper_id,
        "title": record.title,
        "authors": record.authors,
        "keywords": record.keywords,
        "tags": record.tags,
        "original_filename": record.original_filename,
        "content_sha256": record.content_sha256,
        "artifact_ids": record.artifact_ids,
        "updated_at": record.updated_at,
        "year": value("year"),
        "journal": value("journal", ""),
        "doi": value("doi", ""),
        "structured_tags": value("structured_tags", record.tags),
        "human_review_status": (metadata.get("human_review") or {}).get("status"),
        "needs_human_check": (metadata.get("quality") or {}).get("needs_human_check"),
    }


def build_library_router(
    principal_dependency: Callable[..., Principal],
    library_service: LibraryService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/library", tags=["library"])
    configured = dict(handlers or {})
    search_handler = configured.get("library.search")
    if search_handler is not None:
        job_service.register_handler("library.search", search_handler)
    download_handler = configured.get("library.download")
    if download_handler is not None:

        def reconcile_download(context, payload):
            result = dict(download_handler(context, payload) or {})
            principal = Principal(context.user_id, frozenset({Role.USER}))
            library_service.reconcile_download_result(principal, result)
            return result

        job_service.register_handler("library.download", reconcile_download)

    @router.get("/papers")
    def papers(
        q: str = "",
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        rows = library_service.list(principal, q)
        return {"items": [_paper_payload(row) for row in rows], "count": len(rows), "query": q}

    @router.post("/papers")
    async def upload_pdf(
        request: Request,
        filename: str,
        principal: Principal = Depends(principal_dependency),
    ):
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].casefold()
        if content_type not in {"application/pdf", "application/octet-stream"}:
            raise WorkflowValidationError("Only PDF uploads are accepted.")
        staged, safe_name = library_service.begin_upload(principal, filename)
        size = 0
        cancel_requested = Event()
        disconnect_watcher: asyncio.Task | None = None

        async def watch_disconnect() -> None:
            while not cancel_requested.is_set():
                if await request.is_disconnected():
                    cancel_requested.set()
                    return
                await anyio.sleep(0.25)

        try:
            with staged.open("xb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > library_service.MAX_PDF_BYTES:
                        raise WorkflowValidationError("Each PDF must be 80 MB or smaller.")
                    handle.write(chunk)
            library_service.validate_staged_upload(staged, size)
            disconnect_watcher = asyncio.create_task(watch_disconnect())
            admission = partial(
                library_service.admit_staged,
                principal,
                safe_name,
                staged,
                cancel_requested=cancel_requested.is_set,
            )
            record, outcome = await anyio.to_thread.run_sync(
                admission, abandon_on_cancel=True
            )
            payload = {
                **_paper_payload(record),
                "status": outcome,
                "mineru_ready": True,
                "library_count": library_service.count(principal),
            }
            return JSONResponse(
                status_code=(status.HTTP_200_OK if outcome == "duplicate_file" else status.HTTP_201_CREATED),
                content=payload,
            )
        except MinerUPreciseParseFailed as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"status": "failed", **exc.payload()},
            )
        finally:
            cancel_requested.set()
            if disconnect_watcher is not None:
                disconnect_watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await disconnect_watcher
            staged.unlink(missing_ok=True)

    @router.get("/papers/{paper_id}/metadata")
    def metadata(
        paper_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return library_service.get(principal, paper_id).metadata

    @router.put("/papers/{paper_id}/metadata")
    def save_metadata(
        paper_id: str,
        payload: dict[str, Any],
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return library_service.update_metadata(principal, paper_id, payload).metadata

    @router.get("/papers/{paper_id}/markdown")
    def markdown(
        paper_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> PlainTextResponse:
        path = library_service.file(principal, paper_id, "markdown")
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))

    @router.get("/papers/{paper_id}/pdf")
    def pdf(
        paper_id: str,
        request: Request,
        principal: Principal = Depends(principal_dependency),
    ):
        path = library_service.file(principal, paper_id, "pdf")
        size = path.stat().st_size
        common = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-cache",
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(path.name, safe=""),
        }
        raw_range = request.headers.get("Range")
        if not raw_range:
            return FileResponse(path, media_type="application/pdf", headers=common)
        try:
            start, end = _byte_range(raw_range, size)
        except ArtifactRangeNotSatisfiable as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.payload(),
                headers={**common, "Content-Range": f"bytes */{size}"},
            )
        length = end - start + 1
        return StreamingResponse(
            _read_range(path, start, length),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=mimetypes.guess_type(path.name)[0] or "application/pdf",
            headers={
                **common,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            },
        )

    @router.get("/papers/{paper_id}/asset")
    def mineru_asset(
        paper_id: str,
        path: str,
        principal: Principal = Depends(principal_dependency),
    ) -> FileResponse:
        resolved = library_service.mineru_asset(principal, paper_id, path)
        return FileResponse(
            resolved,
            media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            headers={"Cache-Control": "private, no-cache"},
        )

    @router.delete("/papers/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_paper(
        paper_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> None:
        library_service.delete(principal, paper_id)

    @router.post("/search-jobs", status_code=status.HTTP_202_ACCEPTED)
    def search_job(
        payload: LiteratureSearchRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        payload_data = payload.model_dump()
        key = idempotency_key.strip() or str(uuid.uuid4())
        job = job_service.submit(
            principal,
            scope="library",
            project_id=None,
            job_type="library.search",
            idempotency_key=key,
            payload=payload_data,
        )
        return _job_response(job)

    @router.get("/search-jobs/current")
    def current_search_job(
        principal: Principal = Depends(principal_dependency),
    ):
        job = job_service.repository.get_current_job(
            principal.user_id, scope="library", job_type="library.search"
        )
        return {"job": _job_response(job).model_dump() if job is not None else None}

    @router.post("/download-jobs", status_code=status.HTTP_202_ACCEPTED)
    def download_job(
        payload: LiteratureDownloadRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        payload_data = payload.model_dump()
        job = job_service.submit(
            principal,
            scope="library",
            project_id=None,
            job_type="library.download",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            payload=payload_data,
        )
        return _job_response(job)

    @router.get("/download-jobs/current")
    def current_download_job(
        principal: Principal = Depends(principal_dependency),
    ):
        job = job_service.repository.get_current_job(
            principal.user_id, scope="library", job_type="library.download"
        )
        return {"job": _job_response(job).model_dump() if job is not None else None}

    return router
