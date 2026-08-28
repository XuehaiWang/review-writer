"""Rebuildable Library full-text indexing over immutable MinerU artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from review_writer_api.database import UserCreditAccount, database_session, utc_now
from review_writer_api.config import RetrievalTuning
from review_writer_api.errors import WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import (
    LibraryArtifact,
    LibraryDocumentChunk,
    LibraryDocumentIndex,
    LibraryPaper,
)
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.retrieval import (
    CHUNKER_VERSION,
    build_document_chunks,
)


_QUERY_TOKEN = re.compile(r"[\u3400-\u9fff]|[\w]+(?:[-'][\w]+)*", re.UNICODE)


def postgres_term_group_constraint(vector, groups: list[list[str]]):
    """Build ``(A OR B) AND (C OR D)`` without websearch parser loss."""

    return (
        and_(
            *(
                or_(
                    *(
                        vector.op("@@")(
                            func.plainto_tsquery("simple", term)
                        )
                        for term in group
                    )
                )
                for group in groups
                if group
            )
        )
        if groups
        else None
    )


@dataclass(frozen=True)
class PreparedIndex:
    index_id: str
    paper_id: str
    source_lineage_hash: str
    status: str
    needs_job: bool
    semantic_only: bool = False


@dataclass(frozen=True)
class EvidenceHit:
    paper_id: str
    chunk_id: str
    content: str
    page_start: int | None
    page_end: int | None
    section_path: tuple[str, ...]
    content_type: str
    asset_refs: tuple[str, ...]
    score: float
    match_reason: str
    is_neighbor: bool
    index_id: str
    source_lineage_hash: str
    previous_chunk_id: str = ""
    next_chunk_id: str = ""


class LibraryIndexService:
    """Own derived chunks without changing Library admission semantics."""

    def __init__(
        self,
        session_factory,
        workspace_manager: HostedWorkspaceManager,
        *,
        enabled: bool = True,
        vector_enabled: bool = False,
        embedding_gateway: Any | None = None,
        embedding_profile_provider: Any | None = None,
        tuning: RetrievalTuning | None = None,
    ):
        self.session_factory = session_factory
        self.workspace_manager = workspace_manager
        self.enabled = bool(enabled)
        self.vector_enabled = bool(vector_enabled)
        self.embedding_gateway = embedding_gateway
        self.embedding_profile_provider = (
            embedding_profile_provider or embedding_gateway
        )
        self.tuning = tuning or RetrievalTuning()
        self._vector_available: bool | None = None
        self._query_embedding_cache: dict[
            str, tuple[float, str, int, list[float]]
        ] = {}
        self._semantic_failure_until = 0.0

    def _current_embedding_profile(self) -> dict[str, Any]:
        provider = self.embedding_profile_provider or self.embedding_gateway
        if provider is None or not hasattr(provider, "embedding_profile"):
            return {
                "profile": "retrieval_embedding",
                "enabled": False,
                "model": "",
                "dimension": 0,
            }
        try:
            profile = dict(provider.embedding_profile() or {})
        except Exception:
            return {
                "profile": "retrieval_embedding",
                "enabled": False,
                "model": "",
                "dimension": 0,
            }
        return {
            "profile": str(profile.get("profile") or "retrieval_embedding"),
            "enabled": bool(profile.get("enabled")),
            "model": str(profile.get("model") or "").strip(),
            "dimension": int(profile.get("dimension") or 0),
        }

    def semantic_backfill_plan(
        self,
        principal: Principal,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return one bounded user-owned semantic-index backfill batch.

        The planner is intentionally read-only. The router first submits a
        durable job and only then marks its selected rows queued, so a failed
        submission cannot strand an index in a misleading active state.
        """

        principal.require(Permission.PROJECT_READ)
        batch_size = max(
            1,
            min(
                int(limit or self.tuning.semantic_backfill_paper_batch),
                25,
            ),
        )
        base = {
            "enabled": bool(self.vector_enabled),
            "status": "disabled",
            "paper_ids": [],
            "batch_size": batch_size,
            "total_count": 0,
            "ready_count": 0,
            "pending_count": 0,
            "eligible_count": 0,
            "profile": "retrieval_embedding",
            "model": "",
            "dimension": 0,
        }
        if not self.vector_enabled:
            return base

        profile = self._current_embedding_profile()
        base.update(
            {
                "profile": profile["profile"],
                "model": profile["model"],
                "dimension": profile["dimension"],
            }
        )
        if (
            not profile["enabled"]
            or not profile["model"]
            or int(profile["dimension"]) <= 0
        ):
            base["status"] = "unavailable"
            return base

        user_id = uuid.UUID(principal.user_id)
        target_model = str(profile["model"])
        target_dimension = int(profile["dimension"])
        with database_session(self.session_factory) as session:
            rows = list(
                session.scalars(
                    select(LibraryDocumentIndex)
                    .join(
                        LibraryPaper,
                        LibraryPaper.id == LibraryDocumentIndex.library_paper_id,
                    )
                    .where(
                        LibraryDocumentIndex.user_id == user_id,
                        LibraryDocumentIndex.status == "ready",
                        LibraryDocumentIndex.is_current.is_(True),
                        LibraryPaper.user_id == user_id,
                        LibraryPaper.status == "active",
                        LibraryPaper.deleted_at.is_(None),
                    )
                    .order_by(LibraryDocumentIndex.updated_at.asc())
                )
            )
            credit_account = session.get(UserCreditAccount, user_id)
            credit_updated_at = (
                credit_account.updated_at if credit_account is not None else None
            )

        total_count = len(rows)
        pending_rows = [
            row
            for row in rows
            if not (
                row.semantic_status == "ready"
                and row.embedding_profile == str(profile["profile"])
                and row.embedding_model_snapshot == target_model
                and int(row.embedding_dimension or 0) == target_dimension
            )
        ]
        retry_cutoff = utc_now() - timedelta(minutes=5)

        def eligible(row: LibraryDocumentIndex) -> bool:
            if row.semantic_status == "building":
                return False
            if row.semantic_error_code == "INSUFFICIENT_CREDIT":
                if credit_updated_at is None or row.updated_at is None:
                    return False
                account_changed_at = credit_updated_at
                row_updated_at = row.updated_at
                if account_changed_at.tzinfo is None and row_updated_at.tzinfo is not None:
                    account_changed_at = account_changed_at.replace(
                        tzinfo=row_updated_at.tzinfo
                    )
                elif account_changed_at.tzinfo is not None and row_updated_at.tzinfo is None:
                    account_changed_at = account_changed_at.replace(tzinfo=None)
                return account_changed_at > row_updated_at
            if row.semantic_status not in {"failed", "unavailable"}:
                return True
            updated_at = row.updated_at
            if updated_at is None:
                return True
            # SQLite returns naive datetimes even for timezone-aware columns.
            cutoff = retry_cutoff
            if updated_at.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=None)
            return updated_at <= cutoff

        eligible_rows = [row for row in pending_rows if eligible(row)]
        credit_blocked = any(
            row.semantic_error_code == "INSUFFICIENT_CREDIT"
            for row in pending_rows
        )
        status = "complete" if not pending_rows else (
            "pending" if eligible_rows else (
                "blocked_credit" if credit_blocked else "waiting_retry"
            )
        )
        base.update(
            {
                "status": status,
                "paper_ids": [row.paper_id for row in eligible_rows[:batch_size]],
                "total_count": total_count,
                "ready_count": total_count - len(pending_rows),
                "pending_count": len(pending_rows),
                "eligible_count": len(eligible_rows),
            }
        )
        return base

    def mark_semantic_backfill_failed(
        self,
        principal: Principal,
        paper_ids: list[str],
        *,
        code: str,
        message: str,
    ) -> int:
        """Defer unstarted rows after a batch-wide gateway failure."""

        principal.require(Permission.PROJECT_WRITE)
        normalized = list(
            dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
        )
        if not normalized:
            return 0
        with database_session(self.session_factory) as session:
            result = session.execute(
                update(LibraryDocumentIndex)
                .where(
                    LibraryDocumentIndex.user_id == uuid.UUID(principal.user_id),
                    LibraryDocumentIndex.paper_id.in_(tuple(normalized)),
                    LibraryDocumentIndex.status == "ready",
                    LibraryDocumentIndex.is_current.is_(True),
                    LibraryDocumentIndex.semantic_status != "ready",
                )
                .values(
                    semantic_status="failed",
                    semantic_error_code=str(code or "EMBEDDING_BUILD_FAILED")[:96],
                    semantic_error_message=str(message or "")[:2000],
                    updated_at=utc_now(),
                )
            )
            return int(result.rowcount or 0)

    def mark_semantic_backfill_queued(
        self,
        principal: Principal,
        paper_ids: list[str],
        *,
        profile: str,
        model: str,
        dimension: int,
    ) -> int:
        """Expose durable queued state without overwriting a completed worker."""

        principal.require(Permission.PROJECT_WRITE)
        normalized = list(
            dict.fromkeys(str(item).strip() for item in paper_ids if str(item).strip())
        )
        if not normalized:
            return 0
        user_id = uuid.UUID(principal.user_id)
        exact_ready = and_(
            LibraryDocumentIndex.semantic_status == "ready",
            LibraryDocumentIndex.embedding_profile == str(profile),
            LibraryDocumentIndex.embedding_model_snapshot == str(model),
            LibraryDocumentIndex.embedding_dimension == int(dimension),
        )
        with database_session(self.session_factory) as session:
            result = session.execute(
                update(LibraryDocumentIndex)
                .where(
                    LibraryDocumentIndex.user_id == user_id,
                    LibraryDocumentIndex.paper_id.in_(tuple(normalized)),
                    LibraryDocumentIndex.status == "ready",
                    LibraryDocumentIndex.is_current.is_(True),
                    LibraryDocumentIndex.semantic_status.notin_(
                        ("building", "failed", "unavailable")
                    ),
                    ~exact_ready,
                )
                .values(
                    semantic_status="queued",
                    semantic_error_code="",
                    semantic_error_message="",
                    updated_at=utc_now(),
                )
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _safe_file(root: Path, relative_path: str) -> Path:
        raw = str(relative_path or "")
        posix = PurePosixPath(raw)
        windows = PureWindowsPath(raw)
        if (
            not raw
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise WorkflowNotFound("Library source artifact not found.")
        candidate = root.joinpath(*posix.parts).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise WorkflowNotFound("Library source artifact not found.") from exc
        if not candidate.is_file():
            raise WorkflowNotFound("Library source artifact not found.")
        return candidate

    @staticmethod
    def _lineage_payload(
        paper: LibraryPaper, artifacts: list[LibraryArtifact]
    ) -> tuple[dict[str, Any], str]:
        by_kind = {artifact.kind: artifact for artifact in artifacts}
        entries = []
        for kind in ("mineru", "markdown"):
            artifact = by_kind.get(kind)
            if artifact is None:
                continue
            entries.append(
                {
                    "kind": kind,
                    "artifact_id": str(artifact.id),
                    "content_sha256": artifact.content_sha256,
                    "relative_path": artifact.relative_path,
                    "created_at": artifact.created_at.isoformat(),
                }
            )
        payload = {
            "paper_id": paper.paper_id,
            "library_paper_id": str(paper.id),
            "document_content_sha256": paper.content_sha256,
            "artifacts": entries,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _paper_and_lineage(
        self, principal: Principal, paper_id: str
    ) -> tuple[LibraryPaper, list[LibraryArtifact], dict[str, Any], str]:
        principal.require(Permission.PROJECT_READ)
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            paper = session.scalar(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == user_id,
                    LibraryPaper.paper_id == str(paper_id),
                    LibraryPaper.deleted_at.is_(None),
                    LibraryPaper.status == "active",
                )
            )
            if paper is None:
                raise WorkflowNotFound("Library paper not found.")
            current_ids = dict((paper.metadata_json or {}).get("_artifact_ids") or {})
            artifact_uuids: list[uuid.UUID] = []
            for kind in ("mineru", "markdown"):
                raw_id = str(current_ids.get(kind) or "")
                try:
                    artifact_uuids.append(uuid.UUID(raw_id))
                except ValueError:
                    continue
            artifacts = list(
                session.scalars(
                    select(LibraryArtifact).where(
                        LibraryArtifact.user_id == user_id,
                        LibraryArtifact.paper_id == paper.paper_id,
                        LibraryArtifact.id.in_(tuple(artifact_uuids)),
                        LibraryArtifact.availability == "available",
                    )
                )
            ) if artifact_uuids else []
            # Older imported records may predate immutable artifact IDs.  Their
            # registered Markdown remains a valid compatibility source.
            if not any(item.kind == "markdown" for item in artifacts):
                pseudo_created = paper.created_at
                pseudo = LibraryArtifact(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{principal.user_id}:{paper.paper_id}:markdown"),
                    user_id=user_id,
                    paper_id=paper.paper_id,
                    kind="markdown",
                    relative_path=paper.markdown_relative_path,
                    content_sha256=hashlib.sha256(
                        self._safe_file(
                            self.workspace_manager.user_root(principal.user_id),
                            paper.markdown_relative_path,
                        ).read_bytes()
                    ).hexdigest(),
                    size_bytes=0,
                    mtime_ns=0,
                    availability="available",
                    created_at=pseudo_created,
                )
                artifacts.append(pseudo)
            lineage, lineage_hash = self._lineage_payload(paper, artifacts)
            session.expunge(paper)
            for artifact in artifacts:
                if artifact in session:
                    session.expunge(artifact)
            return paper, artifacts, lineage, lineage_hash

    def prepare(
        self, principal: Principal, paper_id: str, *, force: bool = False
    ) -> PreparedIndex:
        """Record queued state before a durable job is submitted."""

        principal.require(Permission.PROJECT_WRITE)
        if not self.enabled:
            raise WorkflowValidationError("Full-text document retrieval is disabled.")
        paper, _artifacts, lineage, lineage_hash = self._paper_and_lineage(
            principal, paper_id
        )
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(LibraryDocumentIndex).where(
                    LibraryDocumentIndex.user_id == user_id,
                    LibraryDocumentIndex.paper_id == paper.paper_id,
                    LibraryDocumentIndex.source_lineage_hash == lineage_hash,
                    LibraryDocumentIndex.chunker_version == CHUNKER_VERSION,
                )
            )
            if row is not None and row.status == "ready" and not force:
                if not row.is_current:
                    session.execute(
                        update(LibraryDocumentIndex)
                        .where(
                            LibraryDocumentIndex.user_id == user_id,
                            LibraryDocumentIndex.paper_id == paper.paper_id,
                        )
                        .values(is_current=False)
                    )
                    row.is_current = True
                    row.updated_at = utc_now()
                semantic_needs_job = bool(
                    self.vector_enabled and row.semantic_status != "ready"
                )
                if semantic_needs_job:
                    row.semantic_status = "queued"
                    row.semantic_error_code = ""
                    row.semantic_error_message = ""
                    row.updated_at = utc_now()
                return PreparedIndex(
                    str(row.id),
                    row.paper_id,
                    lineage_hash,
                    row.status,
                    semantic_needs_job,
                    semantic_only=semantic_needs_job,
                )
            if row is None:
                has_current = session.scalar(
                    select(LibraryDocumentIndex.id).where(
                        LibraryDocumentIndex.user_id == user_id,
                        LibraryDocumentIndex.paper_id == paper.paper_id,
                        LibraryDocumentIndex.is_current.is_(True),
                        LibraryDocumentIndex.status == "ready",
                    )
                )
                row = LibraryDocumentIndex(
                    library_paper_id=paper.id,
                    user_id=user_id,
                    paper_id=paper.paper_id,
                    source_lineage_json=lineage,
                    source_lineage_hash=lineage_hash,
                    chunker_version=CHUNKER_VERSION,
                    is_current=has_current is None,
                )
                session.add(row)
            row.status = "queued"
            row.chunk_count = 0
            row.error_code = ""
            row.error_message = ""
            row.semantic_status = "queued" if self.vector_enabled else "disabled"
            row.embedding_count = 0
            row.semantic_error_code = ""
            row.semantic_error_message = ""
            row.started_at = None
            row.finished_at = None
            row.updated_at = utc_now()
            session.flush()
            return PreparedIndex(str(row.id), row.paper_id, lineage_hash, row.status, True)

    def build(
        self,
        principal: Principal,
        paper_id: str,
        *,
        expected_lineage_hash: str = "",
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        paper, artifacts, lineage, lineage_hash = self._paper_and_lineage(
            principal, paper_id
        )
        if expected_lineage_hash and expected_lineage_hash != lineage_hash:
            # The immutable source pointer changed after the job was queued.
            # Index the new current lineage instead of publishing stale chunks.
            prepared = self.prepare(principal, paper_id, force=False)
            if not prepared.needs_job:
                status = self.status(principal, paper_id)
                return {
                    "paper_id": paper_id,
                    "index_id": status["index_id"],
                    "status": status["fulltext"],
                    "chunk_count": status["chunk_count"],
                    "source_lineage_hash": status["source_lineage_hash"],
                    "chunker_version": status["chunker_version"],
                }
            lineage_hash = prepared.source_lineage_hash
            paper, artifacts, lineage, lineage_hash = self._paper_and_lineage(
                principal, paper_id
            )
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(LibraryDocumentIndex)
                .where(
                    LibraryDocumentIndex.user_id == user_id,
                    LibraryDocumentIndex.paper_id == paper.paper_id,
                    LibraryDocumentIndex.source_lineage_hash == lineage_hash,
                    LibraryDocumentIndex.chunker_version == CHUNKER_VERSION,
                )
                .with_for_update()
            )
            if row is None:
                row = LibraryDocumentIndex(
                    library_paper_id=paper.id,
                    user_id=user_id,
                    paper_id=paper.paper_id,
                    source_lineage_json=lineage,
                    source_lineage_hash=lineage_hash,
                    chunker_version=CHUNKER_VERSION,
                    is_current=False,
                )
                session.add(row)
                session.flush()
            row.status = "building"
            row.started_at = utc_now()
            row.finished_at = None
            row.error_code = ""
            row.error_message = ""
            row.updated_at = utc_now()
            index_id = row.id

        root = self.workspace_manager.user_root(principal.user_id)
        by_kind = {artifact.kind: artifact for artifact in artifacts}
        markdown_artifact = by_kind.get("markdown")
        mineru_artifact = by_kind.get("mineru")
        try:
            markdown_path = self._safe_file(
                root,
                markdown_artifact.relative_path
                if markdown_artifact is not None
                else paper.markdown_relative_path,
            )
            markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
            content_list: Any = []
            if mineru_artifact is not None:
                content_path = self._safe_file(root, mineru_artifact.relative_path)
                content_list = json.loads(content_path.read_text(encoding="utf-8"))
            chunks = build_document_chunks(
                paper.paper_id,
                lineage_hash,
                content_list,
                markdown_fallback=markdown,
                min_tokens=self.tuning.chunk_min_tokens,
                max_tokens=self.tuning.chunk_max_tokens,
                overlap_tokens=self.tuning.oversized_overlap_tokens,
            )
            if not chunks:
                raise WorkflowValidationError(
                    "The parsed document did not contain indexable text."
                )
            with database_session(self.session_factory) as session:
                row = session.scalar(
                    select(LibraryDocumentIndex)
                    .where(
                        LibraryDocumentIndex.id == index_id,
                        LibraryDocumentIndex.user_id == user_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise WorkflowNotFound("Library document index not found.")
                session.execute(
                    delete(LibraryDocumentChunk).where(
                        LibraryDocumentChunk.index_id == index_id
                    )
                )
                session.add_all(
                    [
                        LibraryDocumentChunk(
                            index_id=index_id,
                            user_id=user_id,
                            paper_id=paper.paper_id,
                            chunk_id=chunk.chunk_id,
                            ordinal=chunk.ordinal,
                            content=chunk.content,
                            content_sha256=hashlib.sha256(
                                chunk.content.encode("utf-8")
                            ).hexdigest(),
                            normalized_content=chunk.normalized_content,
                            content_type=chunk.content_type,
                            section_path_json=list(chunk.section_path),
                            page_start=chunk.page_start,
                            page_end=chunk.page_end,
                            block_start=chunk.block_start,
                            block_end=chunk.block_end,
                            asset_refs_json=list(chunk.asset_refs),
                            is_reference=chunk.is_reference,
                            previous_chunk_id=chunk.previous_chunk_id,
                            next_chunk_id=chunk.next_chunk_id,
                        )
                        for chunk in chunks
                    ]
                )
                session.execute(
                    update(LibraryDocumentIndex)
                    .where(
                        LibraryDocumentIndex.user_id == user_id,
                        LibraryDocumentIndex.paper_id == paper.paper_id,
                        LibraryDocumentIndex.id != index_id,
                    )
                    .values(is_current=False)
                )
                row.status = "ready"
                row.is_current = True
                row.chunk_count = len(chunks)
                row.semantic_status = (
                    "pending" if self.vector_enabled else "disabled"
                )
                row.embedding_count = 0
                row.semantic_error_code = ""
                row.semantic_error_message = ""
                row.finished_at = utc_now()
                row.updated_at = utc_now()
            return {
                "paper_id": paper.paper_id,
                "index_id": str(index_id),
                "status": "ready",
                "chunk_count": len(chunks),
                "source_lineage_hash": lineage_hash,
                "chunker_version": CHUNKER_VERSION,
                "semantic_status": (
                    "pending" if self.vector_enabled else "disabled"
                ),
            }
        except Exception as exc:
            with database_session(self.session_factory) as session:
                row = session.get(LibraryDocumentIndex, index_id)
                if row is not None:
                    row.status = "failed"
                    row.chunk_count = 0
                    row.error_code = "LIBRARY_INDEX_BUILD_FAILED"
                    row.error_message = (
                        str(exc)
                        if isinstance(exc, (WorkflowNotFound, WorkflowValidationError))
                        else "The full-text document index could not be built."
                    )
                    row.finished_at = utc_now()
                    row.updated_at = utc_now()
            raise

    def _pgvector_available(self) -> bool:
        if not self.vector_enabled:
            return False
        if self._vector_available is not None:
            return self._vector_available
        try:
            with database_session(self.session_factory) as session:
                if session.get_bind().dialect.name != "postgresql":
                    self._vector_available = False
                else:
                    self._vector_available = bool(
                        session.execute(
                            text(
                                """
                                SELECT EXISTS (
                                  SELECT 1 FROM pg_extension WHERE extname = 'vector'
                                ) AND to_regclass('public.library_chunk_embeddings') IS NOT NULL
                                """
                            )
                        ).scalar()
                    )
        except Exception:
            self._vector_available = False
        return bool(self._vector_available)

    @staticmethod
    def _embedding_input(chunk: LibraryDocumentChunk) -> str:
        section = " > ".join(
            str(item) for item in (chunk.section_path_json or []) if str(item).strip()
        )
        prefix = []
        if section:
            prefix.append(f"Section: {section}")
        if chunk.content_type:
            prefix.append(f"Content type: {chunk.content_type}")
        prefix.append(chunk.content)
        return "\n".join(prefix)

    def _set_semantic_failure(
        self,
        index_id: uuid.UUID,
        *,
        status: str,
        code: str,
        message: str,
    ) -> None:
        with database_session(self.session_factory) as session:
            row = session.get(LibraryDocumentIndex, index_id)
            if row is not None:
                row.semantic_status = status
                row.semantic_error_code = code
                row.semantic_error_message = str(message or "")[:2000]
                row.updated_at = utc_now()

    def build_embeddings(
        self,
        principal: Principal,
        paper_id: str,
        *,
        index_id: str = "",
    ) -> dict[str, Any]:
        """Populate optional chunk embeddings without failing lexical indexing."""

        principal.require(Permission.PROJECT_WRITE)
        if not self.vector_enabled:
            return {"status": "disabled", "embedding_count": 0}
        user_id = uuid.UUID(principal.user_id)
        try:
            resolved_index_id = uuid.UUID(str(index_id)) if index_id else None
        except ValueError:
            resolved_index_id = None
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(LibraryDocumentIndex).where(
                    LibraryDocumentIndex.user_id == user_id,
                    LibraryDocumentIndex.paper_id == str(paper_id),
                    LibraryDocumentIndex.status == "ready",
                    LibraryDocumentIndex.is_current.is_(True),
                    *(
                        (LibraryDocumentIndex.id == resolved_index_id,)
                        if resolved_index_id is not None
                        else ()
                    ),
                )
            )
            if row is None:
                return {"status": "not_indexed", "embedding_count": 0}
            resolved_index_id = row.id
            row.semantic_status = "building"
            row.semantic_error_code = ""
            row.semantic_error_message = ""
            row.updated_at = utc_now()

        if not self._pgvector_available():
            self._set_semantic_failure(
                resolved_index_id,
                status="unavailable",
                code="PGVECTOR_UNAVAILABLE",
                message=(
                    "The PostgreSQL vector extension or semantic table is unavailable; "
                    "lexical retrieval remains active."
                ),
            )
            return {"status": "unavailable", "embedding_count": 0}
        if self.embedding_gateway is None or not hasattr(
            self.embedding_gateway, "embed_for_active_job"
        ):
            self._set_semantic_failure(
                resolved_index_id,
                status="unavailable",
                code="EMBEDDING_GATEWAY_UNAVAILABLE",
                message=(
                    "The embedding gateway is unavailable; lexical retrieval remains active."
                ),
            )
            return {"status": "unavailable", "embedding_count": 0}

        try:
            with database_session(self.session_factory) as session:
                chunks = list(
                    session.scalars(
                        select(LibraryDocumentChunk)
                        .where(
                            LibraryDocumentChunk.index_id == resolved_index_id,
                            LibraryDocumentChunk.user_id == user_id,
                            LibraryDocumentChunk.is_reference.is_(False),
                            func.length(func.trim(LibraryDocumentChunk.content)) > 0,
                        )
                        .order_by(LibraryDocumentChunk.ordinal)
                    )
                )
                prepared = [
                    (
                        chunk.id,
                        self._embedding_input(chunk),
                    )
                    for chunk in chunks
                ]
            if not prepared:
                raise WorkflowValidationError(
                    "The document has no non-reference chunks to embed."
                )

            batch_size = max(1, min(self.tuning.embedding_batch_size, 64))
            total = 0
            model_snapshot = ""
            dimension = 0
            for offset in range(0, len(prepared), batch_size):
                batch = prepared[offset : offset + batch_size]
                inputs = [item[1] for item in batch]
                batch_hash = hashlib.sha256(
                    json.dumps(
                        inputs,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                response = self.embedding_gateway.embed_for_active_job(
                    inputs,
                    request_key=(
                        f"embed-doc:{str(resolved_index_id)[:12]}:"
                        f"{offset // batch_size}:{batch_hash[:16]}"
                    ),
                    stage="library.index.embedding",
                )
                vectors = response.get("embeddings")
                if not isinstance(vectors, list) or len(vectors) != len(batch):
                    raise RuntimeError(
                        "The embedding gateway returned an invalid batch."
                    )
                current_model = str(response.get("model") or "").strip()
                current_dimension = int(response.get("dimension") or 0)
                if not current_model or current_dimension <= 0:
                    raise RuntimeError(
                        "The embedding gateway omitted its model snapshot or dimension."
                    )
                if model_snapshot and current_model != model_snapshot:
                    raise RuntimeError(
                        "The embedding model changed during one document build."
                    )
                if dimension and current_dimension != dimension:
                    raise RuntimeError(
                        "The embedding dimension changed during one document build."
                    )
                model_snapshot = current_model
                dimension = current_dimension
                with database_session(self.session_factory) as session:
                    for (chunk_row_id, source), vector in zip(batch, vectors):
                        if not isinstance(vector, list) or len(vector) != dimension:
                            raise RuntimeError(
                                "The embedding gateway returned an invalid vector."
                            )
                        content_hash = hashlib.sha256(
                            source.encode("utf-8")
                        ).hexdigest()
                        session.execute(
                            text(
                                """
                                INSERT INTO library_chunk_embeddings (
                                  id, chunk_row_id, user_id, paper_id,
                                  content_sha256, embedding_profile,
                                  embedding_model_snapshot, dimension, embedding,
                                  status, error_code, error_message, created_at, updated_at
                                ) VALUES (
                                  CAST(:id AS uuid), CAST(:chunk_row_id AS uuid),
                                  CAST(:user_id AS uuid), :paper_id,
                                  :content_sha256, 'retrieval_embedding',
                                  :model, :dimension, CAST(:embedding AS vector),
                                  'ready', '', '', now(), now()
                                )
                                ON CONFLICT (chunk_row_id, embedding_model_snapshot)
                                DO UPDATE SET
                                  content_sha256 = EXCLUDED.content_sha256,
                                  dimension = EXCLUDED.dimension,
                                  embedding = EXCLUDED.embedding,
                                  status = 'ready', error_code = '', error_message = '',
                                  updated_at = now()
                                """
                            ),
                            {
                                "id": str(uuid.uuid4()),
                                "chunk_row_id": str(chunk_row_id),
                                "user_id": principal.user_id,
                                "paper_id": str(paper_id),
                                "content_sha256": content_hash,
                                "model": model_snapshot,
                                "dimension": dimension,
                                "embedding": json.dumps(
                                    [float(value) for value in vector],
                                    separators=(",", ":"),
                                ),
                            },
                        )
                total += len(batch)

            with database_session(self.session_factory) as session:
                row = session.get(LibraryDocumentIndex, resolved_index_id)
                if row is not None:
                    row.semantic_status = "ready"
                    row.embedding_profile = "retrieval_embedding"
                    row.embedding_model_snapshot = model_snapshot
                    row.embedding_dimension = dimension
                    row.embedding_count = total
                    row.semantic_error_code = ""
                    row.semantic_error_message = ""
                    row.updated_at = utc_now()
            return {
                "status": "ready",
                "embedding_count": total,
                "embedding_model": model_snapshot,
                "embedding_dimension": dimension,
            }
        except Exception as exc:
            error_message = str(exc)
            normalized_error = error_message.casefold()
            error_code = (
                "INSUFFICIENT_CREDIT"
                if (
                    "insufficient_credit" in normalized_error
                    or "insufficient credit" in normalized_error
                    or "余额不足" in error_message
                )
                else "EMBEDDING_BUILD_FAILED"
            )
            stored_message = (
                "余额不足，语义索引回填已暂停；充值后会自动继续。"
                if error_code == "INSUFFICIENT_CREDIT"
                else error_message
            )
            self._set_semantic_failure(
                resolved_index_id,
                status="failed",
                code=error_code,
                message=stored_message,
            )
            return {
                "status": "failed",
                "embedding_count": 0,
                "error_code": error_code,
                "error": stored_message,
            }

    def ensure_embeddings(
        self, principal: Principal, paper_ids: list[str]
    ) -> dict[str, str]:
        """Incrementally backfill semantic indexes for an active workflow job."""

        if not self.vector_enabled or time.monotonic() < self._semantic_failure_until:
            return {}
        if self.embedding_gateway is None or not hasattr(
            self.embedding_gateway, "embedding_profile"
        ):
            return {}
        try:
            profile = dict(self.embedding_gateway.embedding_profile() or {})
        except Exception:
            self._semantic_failure_until = time.monotonic() + 60
            return {}
        if not profile.get("enabled"):
            self._semantic_failure_until = time.monotonic() + 5 * 60
            return {}
        current_model = str(profile.get("model") or "")
        current_dimension = int(profile.get("dimension") or 0)
        summaries = self.summaries(principal, paper_ids)
        results: dict[str, str] = {}
        for paper_id in dict.fromkeys(str(item) for item in paper_ids if str(item)):
            summary = dict(summaries.get(paper_id) or {})
            if summary.get("fulltext") != "ready":
                continue
            if (
                summary.get("semantic") == "ready"
                and summary.get("embedding_model") == current_model
                and int(summary.get("embedding_dimension") or 0)
                == current_dimension
            ):
                results[paper_id] = "ready"
                continue
            result = self.build_embeddings(
                principal,
                paper_id,
                index_id=str(summary.get("index_id") or ""),
            )
            status = str(result.get("status") or "failed")
            results[paper_id] = status
            if status in {"failed", "unavailable"}:
                message = str(result.get("error") or "").casefold()
                if (
                    status == "unavailable"
                    or "not configured" in message
                    or "gateway" in message
                ):
                    self._semantic_failure_until = time.monotonic() + 5 * 60
                    break
        return results

    def _semantic_ranked_ids(
        self,
        principal: Principal,
        query: str,
        *,
        allowed_papers: list[str],
        limit: int,
    ) -> list[tuple[uuid.UUID, float]]:
        if (
            not self.vector_enabled
            or time.monotonic() < self._semantic_failure_until
            or not self._pgvector_available()
            or self.embedding_gateway is None
            or not hasattr(self.embedding_gateway, "embed_for_active_job")
        ):
            return []
        normalized = " ".join(str(query or "").casefold().split())
        if not normalized:
            return []
        cache_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        cached = self._query_embedding_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            _expires, model, dimension, query_vector = cached
        else:
            try:
                response = self.embedding_gateway.embed_for_active_job(
                    [normalized],
                    request_key=f"embed-query:{cache_key[:32]}",
                    stage="retrieval.query.embedding",
                )
            except Exception:
                self._semantic_failure_until = time.monotonic() + 5 * 60
                raise
            vectors = response.get("embeddings")
            if not isinstance(vectors, list) or len(vectors) != 1:
                return []
            query_vector = [float(value) for value in vectors[0]]
            model = str(response.get("model") or "")
            dimension = int(response.get("dimension") or len(query_vector))
            if not model or dimension != len(query_vector):
                return []
            self._query_embedding_cache[cache_key] = (
                time.monotonic() + 10 * 60,
                model,
                dimension,
                query_vector,
            )
        vector_text = json.dumps(query_vector, separators=(",", ":"))
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            rows = session.execute(
                text(
                    """
                    SELECT c.id,
                           1 - (e.embedding <=> CAST(:query_vector AS vector)) AS similarity
                    FROM library_chunk_embeddings e
                    JOIN library_document_chunks c ON c.id = e.chunk_row_id
                    JOIN library_document_indexes i ON i.id = c.index_id
                    WHERE e.user_id = CAST(:user_id AS uuid)
                      AND e.paper_id = ANY(CAST(:allowed_papers AS varchar[]))
                      AND e.embedding_model_snapshot = :model
                      AND e.dimension = :dimension
                      AND e.status = 'ready'
                      AND i.status = 'ready'
                      AND i.is_current = true
                      AND c.is_reference = false
                      AND (e.embedding <=> CAST(:query_vector AS vector))
                          <= :max_distance
                    ORDER BY e.embedding <=> CAST(:query_vector AS vector), c.ordinal
                    LIMIT :limit
                    """
                ),
                {
                    "query_vector": vector_text,
                    "user_id": str(user_id),
                    "allowed_papers": allowed_papers,
                    "model": model,
                    "dimension": dimension,
                    "max_distance": 1.0 - float(self.tuning.semantic_min_similarity),
                    "limit": max(1, min(int(limit), 200)),
                },
            ).all()
        return [(uuid.UUID(str(row_id)), float(similarity or 0)) for row_id, similarity in rows]

    def _status_dict(
        self,
        row: LibraryDocumentIndex | None,
        *,
        mineru_ready: bool,
        current_artifact_ids: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fulltext_status = row.status if row is not None else "not_indexed"
        if row is not None and row.status == "ready":
            lineage_ids = {
                str(item.get("kind") or ""): str(item.get("artifact_id") or "")
                for item in (row.source_lineage_json or {}).get("artifacts") or []
                if isinstance(item, dict)
            }
            current_ids = {
                kind: str((current_artifact_ids or {}).get(kind) or "")
                for kind in ("mineru", "markdown")
            }
            artifact_changed = any(
                current_ids[kind]
                and current_ids[kind] != lineage_ids.get(kind, "")
                for kind in current_ids
            )
            if row.chunker_version != CHUNKER_VERSION or artifact_changed:
                fulltext_status = "rebuild_required"
        return {
            "mineru": "ready" if mineru_ready else "unavailable",
            "fulltext": fulltext_status,
            "semantic": (
                "disabled"
                if not self.vector_enabled
                else (row.semantic_status if row is not None else "not_indexed")
            ),
            "index_id": str(row.id) if row is not None else None,
            "chunk_count": row.chunk_count if row is not None else 0,
            "chunker_version": row.chunker_version if row is not None else CHUNKER_VERSION,
            "source_lineage_hash": row.source_lineage_hash if row is not None else "",
            "error_code": row.error_code if row is not None else "",
            "error_message": row.error_message if row is not None else "",
            "embedding_profile": (
                row.embedding_profile if row is not None else "retrieval_embedding"
            ),
            "embedding_model": (
                row.embedding_model_snapshot if row is not None else ""
            ),
            "embedding_dimension": (
                row.embedding_dimension if row is not None else 0
            ),
            "embedding_count": row.embedding_count if row is not None else 0,
            "semantic_error_code": (
                row.semantic_error_code if row is not None else ""
            ),
            "semantic_error_message": (
                row.semantic_error_message if row is not None else ""
            ),
            "updated_at": row.updated_at.isoformat() if row is not None else None,
        }

    def summaries(
        self, principal: Principal, paper_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        principal.require(Permission.PROJECT_READ)
        normalized = list(dict.fromkeys(str(item) for item in paper_ids if str(item)))
        if not normalized:
            return {}
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            papers = list(
                session.scalars(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == user_id,
                        LibraryPaper.paper_id.in_(tuple(normalized)),
                        LibraryPaper.deleted_at.is_(None),
                    )
                )
            )
            rows = list(
                session.scalars(
                    select(LibraryDocumentIndex)
                    .where(
                        LibraryDocumentIndex.user_id == user_id,
                        LibraryDocumentIndex.paper_id.in_(tuple(normalized)),
                    )
                    .order_by(LibraryDocumentIndex.updated_at.desc())
                )
            )
        current: dict[str, LibraryDocumentIndex] = {}
        for row in rows:
            current.setdefault(row.paper_id, row)
        return {
            paper.paper_id: self._status_dict(
                current.get(paper.paper_id),
                mineru_ready=bool(
                    ((paper.metadata_json or {}).get("_artifact_ids") or {}).get("mineru")
                ),
                current_artifact_ids=dict(
                    (paper.metadata_json or {}).get("_artifact_ids") or {}
                ),
            )
            for paper in papers
        }

    def status(self, principal: Principal, paper_id: str) -> dict[str, Any]:
        # Validate ownership even when no index version exists.
        paper, _artifacts, _lineage, _lineage_hash = self._paper_and_lineage(
            principal, paper_id
        )
        return self.summaries(principal, [paper.paper_id])[paper.paper_id]

    def lexical_scores(self, principal: Principal, query: str) -> dict[str, float]:
        principal.require(Permission.PROJECT_READ)
        normalized = " ".join(str(query or "").casefold().split())
        if not normalized:
            return {}
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                tsquery = func.websearch_to_tsquery("simple", normalized)
                vector = func.to_tsvector("simple", LibraryDocumentChunk.content)
                rank = func.ts_rank_cd(vector, tsquery)
                exact_bonus = case(
                    (LibraryDocumentChunk.normalized_content.contains(normalized), 2.0),
                    else_=0.0,
                )
                rows = session.execute(
                    select(
                        LibraryDocumentChunk.paper_id,
                        func.max(rank + exact_bonus).label("score"),
                    )
                    .join(
                        LibraryDocumentIndex,
                        LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                    )
                    .where(
                        LibraryDocumentChunk.user_id == user_id,
                        LibraryDocumentIndex.status == "ready",
                        LibraryDocumentIndex.is_current.is_(True),
                        LibraryDocumentChunk.is_reference.is_(False),
                        or_(
                            vector.op("@@")(tsquery),
                            LibraryDocumentChunk.normalized_content.contains(normalized),
                        ),
                    )
                    .group_by(LibraryDocumentChunk.paper_id)
                ).all()
                return {str(paper_id): float(score or 0) for paper_id, score in rows}
            rows = session.execute(
                select(
                    LibraryDocumentChunk.paper_id,
                    LibraryDocumentChunk.normalized_content,
                )
                .join(
                    LibraryDocumentIndex,
                    LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                )
                .where(
                    LibraryDocumentChunk.user_id == user_id,
                    LibraryDocumentIndex.status == "ready",
                    LibraryDocumentIndex.is_current.is_(True),
                    LibraryDocumentChunk.is_reference.is_(False),
                )
            ).all()
        tokens = [match.group(0) for match in _QUERY_TOKEN.finditer(normalized)]
        scores: dict[str, float] = {}
        for paper_id, content in rows:
            text = str(content or "")
            exact = 2.0 if normalized in text else 0.0
            matched = sum(1 for token in tokens if token in text)
            if not exact and not matched:
                continue
            score = exact + matched / max(1, len(tokens)) + math.log1p(sum(text.count(token) for token in tokens)) * 0.05
            scores[str(paper_id)] = max(scores.get(str(paper_id), 0.0), score)
        return scores

    def embed_screening_texts(
        self,
        principal: Principal,
        texts: list[str],
        *,
        request_key: str,
        stage: str = "discovery.external.embedding",
    ) -> dict[str, Any]:
        """Embed a bounded screening batch through the active job gateway."""

        principal.require(Permission.PROJECT_READ)
        normalized = [
            " ".join(str(value or "").split())
            for value in texts
            if " ".join(str(value or "").split())
        ]
        if (
            not normalized
            or not self.vector_enabled
            or self.embedding_gateway is None
            or not hasattr(self.embedding_gateway, "embed_for_active_job")
            or time.monotonic() < self._semantic_failure_until
        ):
            return {"status": "unavailable", "embeddings": []}
        all_vectors: list[Any] = []
        model = ""
        dimension = 0
        batch_size = max(1, min(int(self.tuning.embedding_batch_size), 64))
        try:
            for offset in range(0, len(normalized), batch_size):
                batch = normalized[offset : offset + batch_size]
                response = dict(
                    self.embedding_gateway.embed_for_active_job(
                        batch,
                        request_key=f"{request_key}:batch:{offset // batch_size}",
                        stage=stage,
                    )
                    or {}
                )
                vectors = response.get("embeddings")
                if not isinstance(vectors, list) or len(vectors) != len(batch):
                    raise WorkflowValidationError(
                        "Embedding response count did not match the screening batch."
                    )
                response_model = str(response.get("model") or "")
                response_dimension = int(
                    response.get("dimension")
                    or (len(vectors[0]) if vectors else 0)
                )
                if model and response_model != model:
                    raise WorkflowValidationError(
                        "Embedding model changed within one screening request."
                    )
                if dimension and response_dimension != dimension:
                    raise WorkflowValidationError(
                        "Embedding dimension changed within one screening request."
                    )
                model = response_model
                dimension = response_dimension
                all_vectors.extend(vectors)
        except Exception as exc:
            self._semantic_failure_until = time.monotonic() + 5 * 60
            return {
                "status": "failed",
                "embeddings": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        if len(all_vectors) != len(normalized):
            return {
                "status": "failed",
                "embeddings": [],
                "error": "Embedding response count did not match the screening batch.",
            }
        return {
            "status": "ready",
            "embeddings": all_vectors,
            "model": model,
            "dimension": dimension,
        }

    def _screening_lexical_chunks(
        self,
        principal: Principal,
        query: str,
        *,
        allowed_papers: list[str],
        limit: int,
        term_groups: list[list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        normalized = " ".join(str(query or "").casefold().split())
        if not normalized or not allowed_papers:
            return []
        user_id = uuid.UUID(principal.user_id)
        phrases = [
            " ".join(value.split())
            for value in re.split(r"[;\n]+", normalized)
            if " ".join(value.split())
        ][:12]
        groups = [
            [
                " ".join(str(value or "").casefold().split())
                for value in group
                if " ".join(str(value or "").split())
            ]
            for group in (term_groups or [])
            if isinstance(group, list)
        ]
        groups = [group for group in groups if group]
        if not groups:
            groups = [phrases]
        with database_session(self.session_factory) as session:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                escaped = [value.replace('"', " ") for value in phrases]
                web_query = " OR ".join(f'"{value}"' for value in escaped)
                tsquery = func.websearch_to_tsquery("simple", web_query)
                vector = func.to_tsvector("simple", LibraryDocumentChunk.content)
                rank = func.ts_rank_cd(vector, tsquery)
                rows = session.execute(
                    select(
                        LibraryDocumentChunk,
                        LibraryDocumentIndex.source_lineage_hash,
                        rank.label("lexical_score"),
                    )
                    .join(
                        LibraryDocumentIndex,
                        LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                    )
                    .where(
                        LibraryDocumentChunk.user_id == user_id,
                        LibraryDocumentChunk.paper_id.in_(tuple(allowed_papers)),
                        LibraryDocumentIndex.status == "ready",
                        LibraryDocumentIndex.is_current.is_(True),
                        LibraryDocumentChunk.is_reference.is_(False),
                        postgres_term_group_constraint(vector, groups),
                    )
                    .order_by(rank.desc(), LibraryDocumentChunk.ordinal)
                    .limit(max(1, min(int(limit), 400)))
                ).all()
                return [
                    {
                        "paper_id": chunk.paper_id,
                        "chunk_id": chunk.chunk_id,
                        "content": chunk.content,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                        "section_path": list(chunk.section_path_json or []),
                        "content_type": chunk.content_type,
                        "score": float(score or 0),
                        "index_id": str(chunk.index_id),
                        "source_lineage_hash": str(lineage_hash or ""),
                    }
                    for chunk, lineage_hash, score in rows
                ]
            rows = session.execute(
                select(
                    LibraryDocumentChunk,
                    LibraryDocumentIndex.source_lineage_hash,
                )
                .join(
                    LibraryDocumentIndex,
                    LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                )
                .where(
                    LibraryDocumentChunk.user_id == user_id,
                    LibraryDocumentChunk.paper_id.in_(tuple(allowed_papers)),
                    LibraryDocumentIndex.status == "ready",
                    LibraryDocumentIndex.is_current.is_(True),
                    LibraryDocumentChunk.is_reference.is_(False),
                )
            ).all()
        ranked: list[dict[str, Any]] = []
        tokens = [match.group(0) for match in _QUERY_TOKEN.finditer(normalized)]

        def phrase_matches(content: str, phrase: str) -> bool:
            if phrase in content:
                return True
            phrase_tokens = [
                match.group(0) for match in _QUERY_TOKEN.finditer(phrase)
            ]
            return bool(phrase_tokens) and all(
                token in content for token in phrase_tokens
            )

        for chunk, lineage_hash in rows:
            content = str(chunk.normalized_content or "")
            if not all(
                any(phrase_matches(content, phrase) for phrase in group)
                for group in groups
            ):
                continue
            phrase_hits = sum(1 for phrase in phrases if phrase in content)
            token_hits = sum(1 for token in tokens if token in content)
            if not phrase_hits and not token_hits:
                continue
            score = phrase_hits * 2.0 + token_hits / max(1, len(tokens))
            ranked.append(
                {
                    "paper_id": chunk.paper_id,
                    "chunk_id": chunk.chunk_id,
                    "content": chunk.content,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section_path": list(chunk.section_path_json or []),
                    "content_type": chunk.content_type,
                    "score": float(score),
                    "index_id": str(chunk.index_id),
                    "source_lineage_hash": str(lineage_hash or ""),
                }
            )
        ranked.sort(key=lambda row: (-float(row["score"]), str(row["chunk_id"])))
        return ranked[: max(1, min(int(limit), 400))]

    def _screening_semantic_chunks(
        self,
        principal: Principal,
        query: str,
        *,
        allowed_papers: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        ranked_ids = self._semantic_ranked_ids(
            principal,
            query,
            allowed_papers=allowed_papers,
            limit=limit,
        )
        if not ranked_ids:
            return []
        score_by_id = {row_id: score for row_id, score in ranked_ids}
        order = {row_id: index for index, (row_id, _score) in enumerate(ranked_ids)}
        with database_session(self.session_factory) as session:
            rows = session.execute(
                select(
                    LibraryDocumentChunk,
                    LibraryDocumentIndex.source_lineage_hash,
                )
                .join(
                    LibraryDocumentIndex,
                    LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                )
                .where(
                    LibraryDocumentChunk.id.in_(tuple(score_by_id)),
                    LibraryDocumentChunk.is_reference.is_(False),
                )
            ).all()
        output = [
            {
                "paper_id": chunk.paper_id,
                "chunk_id": chunk.chunk_id,
                "content": chunk.content,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "section_path": list(chunk.section_path_json or []),
                "content_type": chunk.content_type,
                "score": float(score_by_id[chunk.id]),
                "index_id": str(chunk.index_id),
                "source_lineage_hash": str(lineage_hash or ""),
                "_order": order[chunk.id],
            }
            for chunk, lineage_hash in rows
        ]
        output.sort(key=lambda row: int(row.pop("_order")))
        return output

    def retrieve_paper_relevance(
        self,
        principal: Principal,
        queries: list[dict[str, Any]],
        allowed_papers: list[str],
        *,
        per_paper_chunk_limit: int = 3,
    ) -> dict[str, Any]:
        """Aggregate lexical and semantic Chunk hits into paper-level signals."""

        principal.require(Permission.PROJECT_READ)
        allowed = list(
            dict.fromkeys(str(value).strip() for value in allowed_papers if str(value).strip())
        )
        if not allowed:
            return {"status": "ready", "semantic_status": "not_applicable", "papers": {}}
        user_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            owned = set(
                session.scalars(
                    select(LibraryPaper.paper_id).where(
                        LibraryPaper.user_id == user_id,
                        LibraryPaper.paper_id.in_(tuple(allowed)),
                        LibraryPaper.deleted_at.is_(None),
                        LibraryPaper.status == "active",
                    )
                )
            )
        if owned != set(allowed):
            raise WorkflowValidationError(
                "Discovery retrieval includes a paper outside the current user's Library."
            )

        clean_queries = [
            dict(item)
            for item in queries[:13]
            if isinstance(item, dict)
            and str(item.get("query_id") or "").strip()
            and str(item.get("query") or "").strip()
        ]
        # Admission queries must run before partition discriminators even when
        # an older artifact stored them in another order. Partition searches
        # may annotate only the papers already admitted by a Topic query.
        clean_queries.sort(
            key=lambda item: 1
            if str(item.get("kind") or "") == "topic_partition"
            else 0
        )
        # Admission queries must run before partition discriminators even when
        # an older artifact stored them in another order. Partition searches
        # may annotate only the papers already admitted by a Topic query.
        clean_queries.sort(
            key=lambda item: 1
            if str(item.get("kind") or "") == "topic_partition"
            else 0
        )
        output: dict[str, dict[str, Any]] = {}
        max_chunks = max(1, min(int(per_paper_chunk_limit), 3))
        semantic_degraded = False
        semantic_errors: list[str] = []
        semantic_attempted = False
        semantic_cooldown_active = time.monotonic() < self._semantic_failure_until
        embedding_gateway_available = bool(
            self.embedding_gateway is not None
            and hasattr(self.embedding_gateway, "embed_for_active_job")
        )
        pgvector_available = bool(
            self.vector_enabled and self._pgvector_available()
        )
        semantic_capable = bool(
            self.vector_enabled
            and not semantic_cooldown_active
            and embedding_gateway_available
            and pgvector_available
        )
        candidate_limit = min(400, max(60, len(allowed) * max_chunks * 2))

        def aggregate(chunks: list[dict[str, Any]], weights: tuple[float, ...]) -> dict[str, dict[str, Any]]:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for chunk in chunks:
                rows = grouped.setdefault(str(chunk["paper_id"]), [])
                if len(rows) < max_chunks:
                    rows.append(chunk)
            result: dict[str, dict[str, Any]] = {}
            for paper_id, rows in grouped.items():
                result[paper_id] = {
                    "score": sum(
                        float(row.get("score") or 0) * weights[index]
                        for index, row in enumerate(rows)
                    ),
                    "chunks": rows,
                }
            return result

        for query in clean_queries:
            query_id = str(query["query_id"])
            query_text = str(query["query"])
            is_partition_query = str(query.get("kind") or "") == "topic_partition"
            lexical = aggregate(
                self._screening_lexical_chunks(
                    principal,
                    query_text,
                    allowed_papers=allowed,
                    limit=candidate_limit,
                    term_groups=(
                        query.get("lexical_term_groups")
                        if isinstance(query.get("lexical_term_groups"), list)
                        else None
                    ),
                ),
                (0.50, 0.30, 0.20),
            )
            semantic: dict[str, dict[str, Any]] = {}
            if semantic_capable:
                semantic_attempted = True
                try:
                    semantic = aggregate(
                        self._screening_semantic_chunks(
                            principal,
                            query_text,
                            allowed_papers=allowed,
                            limit=candidate_limit,
                        ),
                        (0.50, 0.30, 0.20),
                    )
                except Exception as exc:
                    semantic_degraded = True
                    message = " ".join(str(exc or "").split()).strip()
                    semantic_errors.append(
                        f"{type(exc).__name__}: {message}"[:500]
                        if message
                        else type(exc).__name__
                    )

            lexical_rank = {
                paper_id: index
                for index, (paper_id, _row) in enumerate(
                    sorted(lexical.items(), key=lambda item: (-item[1]["score"], item[0])),
                    start=1,
                )
            }
            semantic_rank = {
                paper_id: index
                for index, (paper_id, _row) in enumerate(
                    sorted(semantic.items(), key=lambda item: (-item[1]["score"], item[0])),
                    start=1,
                )
            }
            matched_paper_ids = set(lexical) | set(semantic)
            if is_partition_query:
                # Partition searches annotate papers already admitted by the
                # Topic query. They neither admit new papers nor inflate the
                # paper-level relevance score.
                matched_paper_ids &= set(output)
            for paper_id in matched_paper_ids:
                row = output.setdefault(
                    paper_id,
                    {
                        "paper_id": paper_id,
                        "rrf_score": 0.0,
                        "matched_query_ids": [],
                        "matched_partitions": [],
                        "semantic_partition_candidates": [],
                        "retrieval_channels": [],
                        "query_matches": {},
                        "top_chunks": [],
                    },
                )
                lex_rank = lexical_rank.get(paper_id)
                sem_rank = semantic_rank.get(paper_id)
                if not is_partition_query:
                    row["rrf_score"] += (
                        (1.0 / (self.tuning.rrf_constant + lex_rank) if lex_rank else 0.0)
                        + (0.8 / (self.tuning.rrf_constant + sem_rank) if sem_rank else 0.0)
                    )
                row["matched_query_ids"].append(query_id)
                if str(query.get("kind") or "") == "topic_partition":
                    if lex_rank:
                        # Vector similarity is a recall signal, not evidence
                        # that the paper belongs to this particular facet.
                        row["matched_partitions"].append(query_id)
                    elif sem_rank:
                        row["semantic_partition_candidates"].append(query_id)
                if lex_rank and "fulltext_lexical" not in row["retrieval_channels"]:
                    row["retrieval_channels"].append("fulltext_lexical")
                if sem_rank and "semantic" not in row["retrieval_channels"]:
                    row["retrieval_channels"].append("semantic")
                row["query_matches"][query_id] = {
                    "kind": str(query.get("kind") or ""),
                    "label": str(query.get("label") or query_id),
                    "lexical_rank": lex_rank,
                    "semantic_rank": sem_rank,
                    "lexical_score": float((lexical.get(paper_id) or {}).get("score") or 0),
                    "semantic_score": float((semantic.get(paper_id) or {}).get("score") or 0),
                    "lexical_chunk_count": len((lexical.get(paper_id) or {}).get("chunks") or []),
                    "semantic_chunk_count": len((semantic.get(paper_id) or {}).get("chunks") or []),
                    "partition_assignment": (
                        "lexical_evidence"
                        if str(query.get("kind") or "") == "topic_partition" and lex_rank
                        else "semantic_candidate_only"
                        if str(query.get("kind") or "") == "topic_partition" and sem_rank
                        else "not_applicable"
                    ),
                }
                combined_chunks = [
                    *[(chunk, "fulltext_lexical") for chunk in (lexical.get(paper_id) or {}).get("chunks") or []],
                    *[(chunk, "semantic") for chunk in (semantic.get(paper_id) or {}).get("chunks") or []],
                ]
                known = {str(item.get("chunk_id") or "") for item in row["top_chunks"]}
                for chunk, channel in combined_chunks:
                    chunk_id = str(chunk.get("chunk_id") or "")
                    if not chunk_id or chunk_id in known or len(row["top_chunks"]) >= 3:
                        continue
                    row["top_chunks"].append(
                        {
                            "chunk_id": chunk_id,
                            "page_start": chunk.get("page_start"),
                            "page_end": chunk.get("page_end"),
                            "section_path": chunk.get("section_path") or [],
                            "content_type": chunk.get("content_type"),
                            "score": round(float(chunk.get("score") or 0), 6),
                            "channel": channel,
                            "excerpt": " ".join(str(chunk.get("content") or "").split())[:500],
                        }
                    )
                    known.add(chunk_id)

        summaries = self.summaries(principal, allowed)
        ready_semantic = sum(
            1 for row in summaries.values() if str(row.get("semantic") or "") == "ready"
        )
        for row in output.values():
            row["matched_query_ids"] = list(dict.fromkeys(row["matched_query_ids"]))
            row["matched_partitions"] = list(dict.fromkeys(row["matched_partitions"]))
            row["semantic_partition_candidates"] = list(
                dict.fromkeys(row["semantic_partition_candidates"])
            )
            row["rrf_score"] = round(float(row["rrf_score"]), 8)
            row["semantic_index_status"] = str(
                (summaries.get(row["paper_id"]) or {}).get("semantic") or "not_indexed"
            )
        profile = {}
        if self.embedding_gateway is not None and hasattr(self.embedding_gateway, "embedding_profile"):
            try:
                profile = dict(self.embedding_gateway.embedding_profile() or {})
            except Exception:
                profile = {}
        semantic_reason = ""
        if semantic_degraded:
            semantic_reason = semantic_errors[0] if semantic_errors else "semantic_query_failed"
        elif self.vector_enabled and not semantic_capable:
            if semantic_cooldown_active:
                semantic_reason = "semantic_retry_cooldown"
            elif not embedding_gateway_available:
                semantic_reason = "embedding_gateway_unavailable"
            elif not pgvector_available:
                semantic_reason = "pgvector_unavailable"
        return {
            "status": "degraded" if semantic_degraded else "ready",
            "semantic_status": (
                "disabled"
                if not self.vector_enabled
                else "degraded"
                if semantic_degraded
                else "ready"
                if semantic_attempted
                else "unavailable"
            ),
            "semantic_indexed_paper_count": ready_semantic,
            "semantic_reason": semantic_reason,
            "semantic_error_count": len(semantic_errors),
            "paper_count": len(allowed),
            "embedding_model": str(profile.get("model") or ""),
            "embedding_dimension": int(profile.get("dimension") or 0),
            "rrf_constant": self.tuning.rrf_constant,
            "index_statuses": {
                paper_id: {
                    "fulltext": str(row.get("fulltext") or "not_indexed"),
                    "semantic": str(row.get("semantic") or "not_indexed"),
                }
                for paper_id, row in summaries.items()
            },
            "papers": output,
        }

    def primary_coverage_hits(
        self,
        principal: Principal,
        *,
        allowed_papers: list[str],
        per_paper_limit: int = 1,
    ) -> list[EvidenceHit]:
        """Return a small, page-addressable evidence seed for every paper.

        Section queries are intentionally claim-centered, so one global lexical
        Top-K can omit otherwise valid primary papers.  This fallback is only
        used for primary papers missing from that ranked result; it preserves
        the hard source contract instead of silently dropping required papers.
        """

        principal.require(Permission.PROJECT_READ)
        allowed = list(
            dict.fromkeys(str(item) for item in allowed_papers if str(item).strip())
        )
        if not allowed:
            return []
        user_id = uuid.UUID(principal.user_id)
        limit = max(1, min(int(per_paper_limit), 3))
        output: list[EvidenceHit] = []
        with database_session(self.session_factory) as session:
            owned = set(
                session.scalars(
                    select(LibraryPaper.paper_id).where(
                        LibraryPaper.user_id == user_id,
                        LibraryPaper.paper_id.in_(tuple(allowed)),
                        LibraryPaper.deleted_at.is_(None),
                        LibraryPaper.status == "active",
                    )
                )
            )
            if owned != set(allowed):
                raise WorkflowValidationError(
                    "Evidence retrieval includes a paper outside the current user's Library."
                )
            preferred_content = case(
                (
                    LibraryDocumentChunk.content_type.in_(
                        ("text", "merged_text", "markdown")
                    ),
                    0,
                ),
                (LibraryDocumentChunk.content_type == "table", 1),
                else_=2,
            )
            for paper_id in allowed:
                rows = session.execute(
                    select(
                        LibraryDocumentChunk,
                        LibraryDocumentIndex.source_lineage_hash,
                    )
                    .join(
                        LibraryDocumentIndex,
                        LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                    )
                    .where(
                        LibraryDocumentChunk.user_id == user_id,
                        LibraryDocumentChunk.paper_id == paper_id,
                        LibraryDocumentIndex.status == "ready",
                        LibraryDocumentIndex.is_current.is_(True),
                        LibraryDocumentChunk.is_reference.is_(False),
                        func.length(func.trim(LibraryDocumentChunk.content)) > 0,
                    )
                    .order_by(preferred_content, LibraryDocumentChunk.ordinal)
                    .limit(limit)
                ).all()
                output.extend(
                    EvidenceHit(
                        paper_id=chunk.paper_id,
                        chunk_id=chunk.chunk_id,
                        content=chunk.content,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        section_path=tuple(
                            str(item) for item in chunk.section_path_json or []
                        ),
                        content_type=chunk.content_type,
                        asset_refs=tuple(
                            str(item) for item in chunk.asset_refs_json or []
                        ),
                        score=0.0,
                        match_reason="primary_paper_coverage_fallback",
                        is_neighbor=False,
                        index_id=str(chunk.index_id),
                        source_lineage_hash=str(lineage_hash),
                        previous_chunk_id=chunk.previous_chunk_id,
                        next_chunk_id=chunk.next_chunk_id,
                    )
                    for chunk, lineage_hash in rows
                )
        return output

    def retrieve(
        self,
        principal: Principal,
        query: str,
        *,
        allowed_papers: list[str],
        top_k: int = 12,
        include_neighbors: bool = True,
        per_paper_limit: int | None = None,
        term_groups: list[list[str]] | None = None,
        exact_phrases: list[str] | None = None,
    ) -> list[EvidenceHit]:
        """Return page-addressable lexical evidence within an explicit paper scope.

        ``term_groups`` carries the structured query contract used by section
        evidence retrieval: alternatives inside a group are OR-ed, while every
        non-empty group must match.  ``query`` remains the PostgreSQL
        ``websearch_to_tsquery`` representation and keeps older callers
        backward compatible.
        """

        principal.require(Permission.PROJECT_READ)
        normalized = " ".join(str(query or "").casefold().split())
        allowed = list(
            dict.fromkeys(str(item) for item in allowed_papers if str(item).strip())
        )
        if not normalized or not allowed:
            return []
        normalized_groups = [
            list(
                dict.fromkeys(
                    " ".join(str(term or "").casefold().split())
                    for term in group
                    if " ".join(str(term or "").casefold().split())
                )
            )
            for group in (term_groups or [])
        ]
        normalized_groups = [group for group in normalized_groups if group]
        normalized_phrases = list(
            dict.fromkeys(
                " ".join(str(phrase or "").casefold().split())
                for phrase in (exact_phrases or [])
                if " ".join(str(phrase or "").casefold().split())
            )
        )
        user_id = uuid.UUID(principal.user_id)
        limit = max(1, min(int(top_k), 50))
        paper_limit = max(
            1,
            min(
                int(per_paper_limit or self.tuning.subsection_per_paper_limit),
                limit,
            ),
        )
        with database_session(self.session_factory) as session:
            owned = set(
                session.scalars(
                    select(LibraryPaper.paper_id).where(
                        LibraryPaper.user_id == user_id,
                        LibraryPaper.paper_id.in_(tuple(allowed)),
                        LibraryPaper.deleted_at.is_(None),
                        LibraryPaper.status == "active",
                    )
                )
            )
            if owned != set(allowed):
                raise WorkflowValidationError(
                    "Evidence retrieval includes a paper outside the current user's Library."
                )
            base = (
                select(
                    LibraryDocumentChunk,
                    LibraryDocumentIndex.source_lineage_hash,
                )
                .join(
                    LibraryDocumentIndex,
                    LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                )
                .where(
                    LibraryDocumentChunk.user_id == user_id,
                    LibraryDocumentChunk.paper_id.in_(tuple(allowed)),
                    LibraryDocumentIndex.status == "ready",
                    LibraryDocumentIndex.is_current.is_(True),
                    LibraryDocumentChunk.is_reference.is_(False),
                )
            )
            dialect = session.get_bind().dialect.name
            scored: list[tuple[LibraryDocumentChunk, str, float, str]] = []
            if dialect == "postgresql":
                tsquery = func.websearch_to_tsquery("simple", normalized)
                vector = func.to_tsvector("simple", LibraryDocumentChunk.content)
                rank = func.ts_rank_cd(vector, tsquery)
                # ``websearch_to_tsquery`` does not retain the structured
                # question contract. Require every term group independently.
                group_constraint = postgres_term_group_constraint(
                    vector,
                    normalized_groups,
                )
                phrase_checks = [
                    LibraryDocumentChunk.normalized_content.contains(phrase)
                    for phrase in normalized_phrases
                ]
                exact = (
                    or_(*phrase_checks)
                    if phrase_checks
                    else LibraryDocumentChunk.normalized_content.contains(normalized)
                )
                statement = base.add_columns(
                    rank.label("rank"), exact.label("exact")
                ).where(vector.op("@@")(tsquery))
                if group_constraint is not None:
                    statement = statement.where(group_constraint)
                rows = session.execute(
                    statement
                    .order_by(exact.desc(), rank.desc(), LibraryDocumentChunk.ordinal)
                    .limit(min(200, limit * max(2, paper_limit)))
                ).all()
                for chunk, lineage_hash, rank_value, exact_value in rows:
                    score = float(rank_value or 0) + (2.0 if exact_value else 0.0)
                    scored.append(
                        (
                            chunk,
                            str(lineage_hash),
                            score,
                            "normalized_exact_phrase" if exact_value else "postgresql_fulltext",
                        )
                    )
            else:
                rows = session.execute(base).all()
                tokens = [match.group(0) for match in _QUERY_TOKEN.finditer(normalized)]
                for chunk, lineage_hash in rows:
                    text = str(chunk.normalized_content or "")
                    if normalized_groups and not all(
                        any(term in text for term in group)
                        for group in normalized_groups
                    ):
                        continue
                    exact_value = any(
                        phrase in text for phrase in normalized_phrases
                    ) if normalized_phrases else normalized in text
                    matched = sum(1 for token in tokens if token in text)
                    if not exact_value and not matched:
                        continue
                    score = (
                        (2.0 if exact_value else 0.0)
                        + matched / max(1, len(tokens))
                        + math.log1p(sum(text.count(token) for token in tokens)) * 0.05
                    )
                    scored.append(
                        (
                            chunk,
                            str(lineage_hash),
                            score,
                            "normalized_exact_phrase" if exact_value else "lexical_token_match",
                        )
                    )
            scored.sort(key=lambda item: (-item[2], item[0].paper_id, item[0].ordinal))

            # Fuse independent lexical and semantic rankings.  Semantic
            # retrieval is an optional recall layer: any gateway/vector error
            # leaves the lexical result untouched.
            semantic_ranked: list[tuple[uuid.UUID, float]] = []
            if dialect == "postgresql" and self.vector_enabled:
                try:
                    semantic_ranked = self._semantic_ranked_ids(
                        principal,
                        normalized,
                        allowed_papers=allowed,
                        limit=max(limit, self.tuning.semantic_top_k),
                    )
                except Exception:
                    semantic_ranked = []
            if semantic_ranked:
                lexical_by_id = {item[0].id: item for item in scored}
                semantic_ids = [item[0] for item in semantic_ranked]
                semantic_rows = session.execute(
                    select(
                        LibraryDocumentChunk,
                        LibraryDocumentIndex.source_lineage_hash,
                    )
                    .join(
                        LibraryDocumentIndex,
                        LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                    )
                    .where(
                        LibraryDocumentChunk.id.in_(tuple(semantic_ids)),
                        LibraryDocumentChunk.user_id == user_id,
                        LibraryDocumentChunk.paper_id.in_(tuple(allowed)),
                        LibraryDocumentIndex.status == "ready",
                        LibraryDocumentIndex.is_current.is_(True),
                        LibraryDocumentChunk.is_reference.is_(False),
                    )
                ).all()
                semantic_by_id = {
                    chunk.id: (chunk, str(lineage_hash))
                    for chunk, lineage_hash in semantic_rows
                }
                lexical_rank = {
                    item[0].id: rank
                    for rank, item in enumerate(scored, start=1)
                }
                semantic_rank = {
                    chunk_id: rank
                    for rank, (chunk_id, _similarity) in enumerate(
                        semantic_ranked, start=1
                    )
                }
                all_ids = set(lexical_rank) | set(semantic_rank)
                fused: list[tuple[LibraryDocumentChunk, str, float, str]] = []
                for chunk_row_id in all_ids:
                    lexical_item = lexical_by_id.get(chunk_row_id)
                    semantic_item = semantic_by_id.get(chunk_row_id)
                    if lexical_item is None and semantic_item is None:
                        continue
                    chunk = (
                        lexical_item[0] if lexical_item is not None else semantic_item[0]
                    )
                    lineage_hash = (
                        lexical_item[1] if lexical_item is not None else semantic_item[1]
                    )
                    rrf_score = 0.0
                    if chunk_row_id in lexical_rank:
                        rrf_score += 1.0 / (
                            self.tuning.rrf_constant + lexical_rank[chunk_row_id]
                        )
                    if chunk_row_id in semantic_rank:
                        rrf_score += 1.0 / (
                            self.tuning.rrf_constant + semantic_rank[chunk_row_id]
                        )
                    if lexical_item is not None and chunk_row_id in semantic_rank:
                        reason = "hybrid_lexical_semantic"
                    elif lexical_item is not None:
                        reason = lexical_item[3]
                    else:
                        reason = "semantic_cosine"
                    fused.append((chunk, lineage_hash, rrf_score, reason))
                scored = sorted(
                    fused,
                    key=lambda item: (-item[2], item[0].paper_id, item[0].ordinal),
                )

            balanced: list[tuple[LibraryDocumentChunk, str, float, str]] = []
            per_paper: dict[str, int] = {}
            for item in scored:
                paper_id = item[0].paper_id
                if per_paper.get(paper_id, 0) >= paper_limit:
                    continue
                balanced.append(item)
                per_paper[paper_id] = per_paper.get(paper_id, 0) + 1
                if len(balanced) >= limit:
                    break
            scored = balanced

            if include_neighbors and scored:
                selected_keys = {(item[0].index_id, item[0].chunk_id) for item in scored}
                neighbor_ids = {
                    neighbor_id
                    for chunk, _lineage, _score, _reason in scored
                    for neighbor_id in (chunk.previous_chunk_id, chunk.next_chunk_id)
                    if neighbor_id
                }
                if neighbor_ids:
                    neighbors = session.execute(
                        select(
                            LibraryDocumentChunk,
                            LibraryDocumentIndex.source_lineage_hash,
                        )
                        .join(
                            LibraryDocumentIndex,
                            LibraryDocumentIndex.id == LibraryDocumentChunk.index_id,
                        )
                        .where(
                            LibraryDocumentChunk.user_id == user_id,
                            LibraryDocumentChunk.paper_id.in_(tuple(allowed)),
                            LibraryDocumentChunk.chunk_id.in_(tuple(neighbor_ids)),
                            LibraryDocumentIndex.status == "ready",
                            LibraryDocumentIndex.is_current.is_(True),
                            LibraryDocumentChunk.is_reference.is_(False),
                        )
                    ).all()
                    for chunk, lineage_hash in neighbors:
                        key = (chunk.index_id, chunk.chunk_id)
                        if key in selected_keys:
                            continue
                        selected_keys.add(key)
                        anchor_scores = [
                            score
                            for anchor, _lineage, score, _reason in scored
                            if anchor.index_id == chunk.index_id
                            and chunk.chunk_id
                            in {anchor.previous_chunk_id, anchor.next_chunk_id}
                        ]
                        scored.append(
                            (
                                chunk,
                                str(lineage_hash),
                                (max(anchor_scores) * 0.85 if anchor_scores else 0.0),
                                "adjacent_context",
                            )
                        )

        return [
            EvidenceHit(
                paper_id=chunk.paper_id,
                chunk_id=chunk.chunk_id,
                content=chunk.content,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_path=tuple(str(item) for item in chunk.section_path_json or []),
                content_type=chunk.content_type,
                asset_refs=tuple(str(item) for item in chunk.asset_refs_json or []),
                score=round(score, 8),
                match_reason=reason,
                is_neighbor=reason == "adjacent_context",
                index_id=str(chunk.index_id),
                source_lineage_hash=lineage_hash,
                previous_chunk_id=chunk.previous_chunk_id,
                next_chunk_id=chunk.next_chunk_id,
            )
            for chunk, lineage_hash, score, reason in scored
        ]
