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
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.errors import (
    ArtifactRangeNotSatisfiable,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.job_service import JobService
from review_writer_api.routers.files import _byte_range, _read_range
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    BibliographyResolutionRequest,
    LiteratureDownloadRequest,
    LiteratureSearchRequest,
)
from review_writer_core.metadata_tags import structured_tags_are_verified
from review_writer_core.bibliography_audit import bibliography_candidates


def _paper_payload(
    record: LibraryPaperRecord,
    index_status: dict[str, Any] | None = None,
    search_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = record.metadata
    resolved_index_status = index_status or {
        "mineru": "ready" if record.artifact_ids.get("mineru") else "unavailable",
        "fulltext": "not_indexed",
        "semantic": "disabled",
        "chunk_count": 0,
    }
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
        "structured_tags": record.tags,
        "structured_tags_verified": structured_tags_are_verified(metadata),
        "human_review_status": (metadata.get("human_review") or {}).get("status"),
        "needs_human_check": (metadata.get("quality") or {}).get("needs_human_check"),
        "mineru_parse_status": resolved_index_status["mineru"],
        "document_index_status": resolved_index_status["fulltext"],
        "embedding_status": resolved_index_status["semantic"],
        "index_status": resolved_index_status,
        "search_match": search_match,
        "bibliography_audit": record.bibliography_audit,
    }


def build_library_router(
    principal_dependency: Callable[..., Principal],
    library_service: LibraryService,
    library_index_service: LibraryIndexService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/library", tags=["library"])

    def acquisition_operation_key(principal: Principal, project_id: str) -> str:
        normalized = str(project_id or "").strip()
        if not normalized:
            return ""
        if job_service.repository.get_owned_project(principal.user_id, normalized) is None:
            raise WorkflowNotFound("Project not found.")
        return f"project:{normalized}"
    configured = dict(handlers or {})

    def submit_discovery_refresh(
        principal: Principal,
        refresh: dict[str, Any],
        paper_id: str,
    ):
        project_id = str(refresh.get("project_id") or "").strip()
        candidate_id = str(refresh.get("candidate_id") or "").strip()
        if not project_id or not candidate_id or not paper_id:
            return None
        try:
            return job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="discovery.candidate-refresh",
                idempotency_key=(
                    f"candidate:{candidate_id}:paper:{paper_id}:"
                    f"revision:{refresh.get('source_revision')}"
                ),
                payload={
                    "project_id": project_id,
                    "candidate_id": candidate_id,
                    "paper_id": paper_id,
                    "source_revision": refresh.get("source_revision"),
                },
                operation_key=f"candidate-refresh:{candidate_id}",
            )
        except WorkflowConflict:
            return None

    def index_handler(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        context.report_progress(1, 3)
        paper_id = str(payload.get("paper_id") or "")
        if bool(payload.get("semantic_only")):
            status = library_index_service.status(principal, paper_id)
            result = {
                "paper_id": paper_id,
                "index_id": status.get("index_id"),
                "status": status.get("fulltext"),
                "chunk_count": status.get("chunk_count", 0),
                "source_lineage_hash": status.get("source_lineage_hash", ""),
                "chunker_version": status.get("chunker_version", ""),
            }
        else:
            result = library_index_service.build(
                principal,
                paper_id,
                expected_lineage_hash=str(payload.get("source_lineage_hash") or ""),
            )
        context.repository.update_job_progress(context.job_id, 2, 3)
        semantic = library_index_service.build_embeddings(
            principal,
            paper_id,
            index_id=str(result.get("index_id") or ""),
        )
        result["semantic"] = semantic
        context.repository.update_job_progress(context.job_id, 3, 3)
        refresh = payload.get("discovery_refresh")
        if isinstance(refresh, dict):
            refresh_job = submit_discovery_refresh(principal, refresh, paper_id)
            result["discovery_refresh_job_id"] = (
                refresh_job.id if refresh_job is not None else None
            )
        return result

    job_service.register_handler("library.index", index_handler)

    def semantic_backfill_handler(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        paper_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in (payload.get("paper_ids") or [])
                if str(item).strip()
            )
        )[:25]
        total = len(paper_ids)
        context.report_progress(0, total)
        results: dict[str, dict[str, Any]] = {}
        ready_count = 0
        for position, paper_id in enumerate(paper_ids, start=1):
            context.checkpoint()
            result = dict(
                library_index_service.build_embeddings(principal, paper_id) or {}
            )
            results[paper_id] = result
            if result.get("status") == "ready":
                ready_count += 1
            if result.get("error_code") == "INSUFFICIENT_CREDIT":
                remaining = paper_ids[position:]
                library_index_service.mark_semantic_backfill_failed(
                    principal,
                    remaining,
                    code="INSUFFICIENT_CREDIT",
                    message=str(result.get("error") or "余额不足，语义索引回填已暂停。"),
                )
                for deferred_paper_id in remaining:
                    results[deferred_paper_id] = {
                        "status": "deferred",
                        "error_code": "INSUFFICIENT_CREDIT",
                    }
                context.report_partial_result(
                    {
                        "paper_ids": paper_ids,
                        "completed_count": total,
                        "ready_count": ready_count,
                        "credit_blocked": True,
                        "results": results,
                    }
                )
                context.report_progress(total, total)
                break
            context.report_partial_result(
                {
                    "paper_ids": paper_ids,
                    "completed_count": position,
                    "ready_count": ready_count,
                    "results": results,
                }
            )
            context.report_progress(position, total)
        return {
            "paper_ids": paper_ids,
            "completed_count": total,
            "ready_count": ready_count,
            "credit_blocked": any(
                result.get("error_code") == "INSUFFICIENT_CREDIT"
                for result in results.values()
            ),
            "results": results,
        }

    job_service.register_handler(
        "library.semantic-backfill", semantic_backfill_handler
    )

    def semantic_backfill_state(principal: Principal) -> dict[str, Any]:
        plan = library_index_service.semantic_backfill_plan(principal)
        if not plan.get("enabled") or plan.get("status") == "unavailable":
            return plan

        operation_key = "semantic-backfill"
        current = job_service.repository.get_current_job(
            principal.user_id,
            scope="library",
            project_id=None,
            job_type="library.semantic-backfill",
            operation_key=operation_key,
        )
        if current is not None and current.status in {
            "queued",
            "running",
            "cancel_requested",
        }:
            return {
                **plan,
                "status": current.status,
                "current_job": _job_response(current).model_dump(),
            }

        paper_ids = list(plan.get("paper_ids") or [])
        if plan.get("status") != "pending" or not paper_ids:
            return {**plan, "current_job": None}
        try:
            current = job_service.submit(
                principal,
                scope="library",
                project_id=None,
                job_type="library.semantic-backfill",
                idempotency_key=f"auto:{uuid.uuid4()}",
                payload={
                    "paper_ids": paper_ids,
                    "profile": plan.get("profile"),
                    "model": plan.get("model"),
                    "dimension": plan.get("dimension"),
                },
                operation_key=operation_key,
            )
        except WorkflowConflict:
            current = job_service.repository.get_current_job(
                principal.user_id,
                scope="library",
                project_id=None,
                job_type="library.semantic-backfill",
                operation_key=operation_key,
            )
            if current is None:
                return {**plan, "current_job": None}
        library_index_service.mark_semantic_backfill_queued(
            principal,
            paper_ids,
            profile=str(plan.get("profile") or "retrieval_embedding"),
            model=str(plan.get("model") or ""),
            dimension=int(plan.get("dimension") or 0),
        )
        return {
            **plan,
            "status": current.status,
            "current_job": _job_response(current).model_dump(),
        }

    bibliography_builder = configured.get("library.bibliography-audit")

    if bibliography_builder is not None:

        def bibliography_handler(context, payload):
            built = dict(bibliography_builder(context, payload) or {})
            principal = Principal(context.user_id, frozenset({Role.USER}))
            current, built = library_service.apply_bibliography_audit_result(
                principal,
                str(payload.get("paper_id") or ""),
                built,
            )
            return {**built, "paper_id": current.paper_id}

        job_service.register_handler("library.bibliography-audit", bibliography_handler)

    def enqueue_index(
        principal: Principal,
        paper_id: str,
        *,
        force: bool = False,
        suppress_active_conflict: bool = False,
        discovery_refresh: dict[str, Any] | None = None,
    ):
        if not library_index_service.enabled:
            return None
        prepared = library_index_service.prepare(principal, paper_id, force=force)
        if not prepared.needs_job:
            if discovery_refresh:
                submit_discovery_refresh(principal, discovery_refresh, prepared.paper_id)
            return None
        try:
            return job_service.submit(
                principal,
                scope="library",
                project_id=None,
                job_type="library.index",
                idempotency_key=(
                    f"manual:{uuid.uuid4()}"
                    if force
                    else f"auto:{prepared.source_lineage_hash}"
                ),
                payload={
                    "paper_id": prepared.paper_id,
                    "source_lineage_hash": prepared.source_lineage_hash,
                    "semantic_only": prepared.semantic_only,
                    **(
                        {"discovery_refresh": dict(discovery_refresh)}
                        if discovery_refresh
                        else {}
                    ),
                },
                operation_key=f"index:{prepared.paper_id}",
            )
        except WorkflowConflict:
            if suppress_active_conflict:
                if discovery_refresh:
                    submit_discovery_refresh(principal, discovery_refresh, paper_id)
                return None
            raise

    def enqueue_bibliography_audit(
        principal: Principal,
        record: LibraryPaperRecord,
        *,
        suppress_active_conflict: bool = False,
        force_network: bool = False,
    ):
        if bibliography_builder is None:
            return None
        try:
            return job_service.submit(
                principal,
                scope="library",
                project_id=None,
                job_type="library.bibliography-audit",
                idempotency_key=(
                    f"manual:{uuid.uuid4()}"
                    if force_network
                    else (
                        f"bibliography:{record.paper_id}:"
                        f"{record.artifact_ids.get('metadata') or record.content_sha256}"
                    )
                ),
                payload={
                    "paper_id": record.paper_id,
                    "metadata": record.metadata,
                    "pdf_relative_path": record.pdf_relative_path,
                    "markdown_relative_path": record.markdown_relative_path,
                    "previous_audit": record.bibliography_audit,
                    "network_mode": "force" if force_network else "fallback",
                    "task_kind": "bibliography_verification",
                    "adds_candidate_papers": False,
                },
                operation_key=f"bibliography-audit:{record.paper_id}",
            )
        except WorkflowConflict:
            if suppress_active_conflict:
                return None
            raise

    search_handler = configured.get("library.search")
    if search_handler is not None:
        job_service.register_handler("library.search", search_handler)
    download_handler = configured.get("library.download")
    if download_handler is not None:

        def reconcile_download(context, payload):
            result = dict(download_handler(context, payload) or {})
            principal = Principal(context.user_id, frozenset({Role.USER}))
            records = library_service.reconcile_download_result(principal, result)
            result_entries = {
                str(entry.get("paper_id") or ""): entry
                for entry in result.get("results") or []
                if isinstance(entry, dict) and str(entry.get("paper_id") or "").strip()
            }
            acquisition_project_id = str(
                payload.get("acquisition_project_id") or ""
            ).strip()
            for record in records:
                entry = result_entries.get(record.paper_id) or {}
                candidate_id = str(entry.get("candidate_id") or "").strip()
                refresh = (
                    {
                        "project_id": acquisition_project_id,
                        "candidate_id": candidate_id,
                        "source_revision": payload.get("discovery_revision"),
                    }
                    if acquisition_project_id and candidate_id
                    else None
                )
                enqueue_index(
                    principal,
                    record.paper_id,
                    suppress_active_conflict=True,
                    discovery_refresh=refresh,
                )
                enqueue_bibliography_audit(
                    principal,
                    record,
                    suppress_active_conflict=True,
                )
            return result

        job_service.register_handler("library.download", reconcile_download)

    def upload_handler(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        staged = library_service.staged_upload_path(
            principal, str(payload.get("staging_id") or "")
        )
        filename = str(payload.get("filename") or "")
        context.report_progress(1, 3)
        record, outcome = library_service.admit_staged(
            principal,
            filename,
            staged,
            cancel_requested=context.cancellation_requested,
            job_id=context.job_id,
        )
        staged.unlink(missing_ok=True)
        index_job = enqueue_index(
            principal, record.paper_id, suppress_active_conflict=True
        )
        bibliography_job = enqueue_bibliography_audit(
            principal, record, suppress_active_conflict=True
        )
        context.repository.update_job_progress(context.job_id, 3, 3)
        return {
            **_paper_payload(record),
            "status": outcome,
            "mineru_ready": True,
            "library_count": library_service.count(principal),
            "index_job_id": index_job.id if index_job is not None else None,
            "bibliography_audit_job_id": (
                bibliography_job.id if bibliography_job is not None else None
            ),
        }

    job_service.register_handler("library.upload", upload_handler)

    def upload_job_payload(job) -> dict[str, Any]:
        payload = _job_response(job).model_dump()
        payload["filename"] = str((job.payload or {}).get("filename") or "")
        payload["batch_id"] = str((job.payload or {}).get("batch_id") or "")
        return payload

    @router.get("/papers")
    def papers(
        q: str = "",
        mode: str = "metadata",
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "metadata").strip().casefold()
        if normalized_mode not in {"metadata", "fulltext", "hybrid"}:
            raise WorkflowValidationError(
                "Library search mode must be metadata, fulltext, or hybrid."
            )
        semantic_backfill = semantic_backfill_state(principal)
        if not str(q or "").strip():
            rows = library_service.list(principal)
            search_matches: dict[str, dict[str, Any]] = {}
            retrieval_mode = (
                "metadata"
                if normalized_mode == "metadata" or not library_index_service.enabled
                else "lexical"
            )
        elif normalized_mode == "metadata" or not library_index_service.enabled:
            rows = library_service.list(principal, q)
            search_matches = {}
            retrieval_mode = "metadata"
        else:
            all_rows = library_service.list(principal)
            by_id = {row.paper_id: row for row in all_rows}
            lexical = library_index_service.lexical_scores(principal, q)
            metadata_rows = library_service.list(principal, q)
            combined: dict[str, float] = {}
            if normalized_mode == "hybrid":
                for rank, record in enumerate(metadata_rows, start=1):
                    combined[record.paper_id] = combined.get(record.paper_id, 0.0) + 1 / (library_index_service.tuning.rrf_constant + rank)
            for rank, (paper_id, _score) in enumerate(
                sorted(lexical.items(), key=lambda item: (-item[1], item[0])),
                start=1,
            ):
                combined[paper_id] = combined.get(paper_id, 0.0) + 1 / (library_index_service.tuning.rrf_constant + rank)
            rows = [
                by_id[paper_id]
                for paper_id, _score in sorted(
                    combined.items(), key=lambda item: (-item[1], item[0])
                )
                if paper_id in by_id
            ]
            hits = library_index_service.retrieve(
                principal,
                q,
                allowed_papers=list(by_id),
                top_k=50,
                include_neighbors=False,
            ) if by_id else []
            search_matches = {}
            for hit in hits:
                search_matches.setdefault(
                    hit.paper_id,
                    {
                        "chunk_id": hit.chunk_id,
                        "page_start": hit.page_start,
                        "page_end": hit.page_end,
                        "section_path": list(hit.section_path),
                        "content": hit.content,
                        "match_reason": hit.match_reason,
                    },
                )
            retrieval_mode = "lexical_only" if normalized_mode == "hybrid" else "lexical"
        summaries = library_index_service.summaries(
            principal, [row.paper_id for row in rows]
        )
        return {
            "items": [
                _paper_payload(
                    row,
                    summaries.get(row.paper_id),
                    search_matches.get(row.paper_id),
                )
                for row in rows
            ],
            "count": len(rows),
            "query": q,
            "requested_mode": normalized_mode,
            "retrieval_mode": retrieval_mode,
            "semantic_backfill": semantic_backfill,
        }

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
            index_job = enqueue_index(
                principal, record.paper_id, suppress_active_conflict=True
            )
            bibliography_job = enqueue_bibliography_audit(
                principal, record, suppress_active_conflict=True
            )
            payload = {
                **_paper_payload(record),
                "status": outcome,
                "mineru_ready": True,
                "library_count": library_service.count(principal),
                "index_job_id": index_job.id if index_job is not None else None,
                "bibliography_audit_job_id": (
                    bibliography_job.id if bibliography_job is not None else None
                ),
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

    @router.post("/upload-jobs", status_code=status.HTTP_202_ACCEPTED)
    async def create_upload_job(
        request: Request,
        filename: str,
        batch_id: str = "",
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].casefold()
        if content_type not in {"application/pdf", "application/octet-stream"}:
            raise WorkflowValidationError("Only PDF uploads are accepted.")
        try:
            normalized_batch_id = str(uuid.UUID(batch_id)) if batch_id else str(uuid.uuid4())
        except ValueError as exc:
            raise WorkflowValidationError("The upload batch identifier is invalid.") from exc
        staged, safe_name = library_service.begin_upload(principal, filename)
        staging_id = staged.name.removesuffix(".pdf.part")
        size = 0
        submitted = False
        try:
            with staged.open("xb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > library_service.MAX_PDF_BYTES:
                        raise WorkflowValidationError("Each PDF must be 80 MB or smaller.")
                    handle.write(chunk)
            library_service.validate_staged_upload(staged, size)
            job = job_service.submit(
                principal,
                scope="library",
                project_id=None,
                job_type="library.upload",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload={
                    "filename": safe_name,
                    "staging_id": staging_id,
                    "batch_id": normalized_batch_id,
                },
                operation_key=f"upload:{staging_id}",
            )
            submitted = True
            return upload_job_payload(job)
        finally:
            if not submitted:
                staged.unlink(missing_ok=True)

    @router.get("/upload-jobs/recent")
    def recent_upload_jobs(
        limit: int = 100,
        include_active: bool = True,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        rows = job_service.repository.list_library_jobs(
            principal.user_id,
            job_type="library.upload",
            limit=max(1, min(limit, 100)),
            include_all_active=include_active,
        )
        batch_summaries = job_service.repository.summarize_library_upload_batches(
            principal.user_id,
            limit=20,
        )
        return {
            "items": [upload_job_payload(row) for row in rows],
            "count": len(rows),
            "batch_summaries": [
                {
                    **summary,
                    "created_at": summary["created_at"].isoformat(),
                    "updated_at": summary["updated_at"].isoformat(),
                }
                for summary in batch_summaries
            ],
        }

    @router.get("/papers/{paper_id}/index-status")
    def document_index_status(
        paper_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return library_index_service.status(principal, paper_id)

    @router.post(
        "/papers/{paper_id}/reindex", status_code=status.HTTP_202_ACCEPTED
    )
    def reindex_paper(
        paper_id: str,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        prepared = library_index_service.prepare(principal, paper_id, force=True)
        job = job_service.submit(
            principal,
            scope="library",
            project_id=None,
            job_type="library.index",
            idempotency_key=idempotency_key.strip() or f"manual:{uuid.uuid4()}",
            payload={
                "paper_id": prepared.paper_id,
                "source_lineage_hash": prepared.source_lineage_hash,
            },
            operation_key=f"index:{prepared.paper_id}",
        )
        return _job_response(job).model_dump()

    @router.post("/reindex-jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_reindex_jobs(
        payload: dict[str, Any] | None = None,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        requested = payload.get("paper_ids") if isinstance(payload, dict) else None
        force = bool(payload.get("force")) if isinstance(payload, dict) else False
        paper_ids = (
            [str(item) for item in requested if str(item).strip()]
            if isinstance(requested, list)
            else [record.paper_id for record in library_service.list(principal)]
        )
        jobs = []
        for paper_id in dict.fromkeys(paper_ids):
            job = enqueue_index(principal, paper_id, force=force)
            if job is not None:
                jobs.append(_job_response(job).model_dump())
        return {"items": jobs, "count": len(jobs)}

    @router.get("/reindex-jobs/current")
    def current_reindex_jobs(
        paper_id: str = "",
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        if paper_id:
            job = job_service.repository.get_current_job(
                principal.user_id,
                scope="library",
                job_type="library.index",
                operation_key=f"index:{paper_id}",
            )
            return {"job": _job_response(job).model_dump() if job is not None else None}
        rows = job_service.repository.list_library_jobs(
            principal.user_id, job_type="library.index", limit=100
        )
        return {"items": [_job_response(row).model_dump() for row in rows], "count": len(rows)}

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

    @router.get("/papers/{paper_id}/bibliography-audit")
    def bibliography_audit(
        paper_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        record = library_service.get(principal, paper_id)
        current_job = job_service.repository.get_current_job(
            principal.user_id,
            scope="library",
            job_type="library.bibliography-audit",
            operation_key=f"bibliography-audit:{record.paper_id}",
        )
        return {
            "paper_id": record.paper_id,
            "task_kind": "bibliography_verification",
            "adds_candidate_papers": False,
            "audit": record.bibliography_audit,
            "candidates": bibliography_candidates(record.bibliography_audit),
            "job": (
                _job_response(current_job).model_dump()
                if current_job is not None
                else None
            ),
        }

    @router.post(
        "/papers/{paper_id}/bibliography-audit-jobs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_bibliography_audit(
        paper_id: str,
        principal: Principal = Depends(principal_dependency),
    ):
        record = library_service.get(principal, paper_id)
        # This endpoint is an explicit human request to consult the external
        # provider even when the local PDF evidence was already sufficient.
        job = enqueue_bibliography_audit(principal, record, force_network=True)
        if job is None:
            raise WorkflowValidationError(
                "Bibliography verification is not configured on this server."
            )
        return _job_response(job)

    @router.post("/papers/{paper_id}/bibliography-resolution")
    def resolve_bibliography_record(
        paper_id: str,
        payload: BibliographyResolutionRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        record, outcome = library_service.resolve_bibliography(
            principal,
            paper_id,
            payload.model_dump(mode="json"),
        )
        return {
            "paper": _paper_payload(record),
            "audit": outcome["audit"],
            "candidates": outcome["candidates"],
            "changed_fields": outcome["changed_fields"],
            "impact": outcome["impact"],
        }

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
        project_id: str = "",
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        operation_key = acquisition_operation_key(principal, project_id)
        payload_data = {
            **payload.model_dump(),
            **({"acquisition_project_id": project_id} if operation_key else {}),
        }
        key = idempotency_key.strip() or str(uuid.uuid4())
        job = job_service.submit(
            principal,
            scope="library",
            project_id=None,
            job_type="library.search",
            idempotency_key=key,
            payload=payload_data,
            operation_key=operation_key,
        )
        return _job_response(job)

    @router.get("/search-jobs/current")
    def current_search_job(
        project_id: str = "",
        principal: Principal = Depends(principal_dependency),
    ):
        operation_key = acquisition_operation_key(principal, project_id)
        job = job_service.repository.get_current_job(
            principal.user_id,
            scope="library",
            job_type="library.search",
            operation_key=operation_key,
        )
        return {"job": _job_response(job).model_dump() if job is not None else None}

    @router.post("/download-jobs", status_code=status.HTTP_202_ACCEPTED)
    def download_job(
        payload: LiteratureDownloadRequest,
        project_id: str = "",
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        operation_key = acquisition_operation_key(principal, project_id)
        payload_data = {
            **payload.model_dump(),
            **({"acquisition_project_id": project_id} if operation_key else {}),
        }
        job = job_service.submit(
            principal,
            scope="library",
            project_id=None,
            job_type="library.download",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            payload=payload_data,
            operation_key=operation_key,
        )
        return _job_response(job)

    @router.get("/download-jobs/current")
    def current_download_job(
        project_id: str = "",
        principal: Principal = Depends(principal_dependency),
    ):
        operation_key = acquisition_operation_key(principal, project_id)
        job = job_service.repository.get_current_job(
            principal.user_id,
            scope="library",
            job_type="library.download",
            operation_key=operation_key,
        )
        return {"job": _job_response(job).model_dump() if job is not None else None}

    return router
