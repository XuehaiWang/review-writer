"""Rebuildable Library full-text indexing over immutable MinerU artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from review_writer_api.database import database_session, utc_now
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


@dataclass(frozen=True)
class PreparedIndex:
    index_id: str
    paper_id: str
    source_lineage_hash: str
    status: str
    needs_job: bool


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
        tuning: RetrievalTuning | None = None,
    ):
        self.session_factory = session_factory
        self.workspace_manager = workspace_manager
        self.enabled = bool(enabled)
        self.tuning = tuning or RetrievalTuning()

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
                return PreparedIndex(
                    str(row.id), row.paper_id, lineage_hash, row.status, False
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
                row.finished_at = utc_now()
                row.updated_at = utc_now()
            return {
                "paper_id": paper.paper_id,
                "index_id": str(index_id),
                "status": "ready",
                "chunk_count": len(chunks),
                "source_lineage_hash": lineage_hash,
                "chunker_version": CHUNKER_VERSION,
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

    @staticmethod
    def _status_dict(
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
            "semantic": "disabled",
            "index_id": str(row.id) if row is not None else None,
            "chunk_count": row.chunk_count if row is not None else 0,
            "chunker_version": row.chunker_version if row is not None else CHUNKER_VERSION,
            "source_lineage_hash": row.source_lineage_hash if row is not None else "",
            "error_code": row.error_code if row is not None else "",
            "error_message": row.error_message if row is not None else "",
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
                phrase_checks = [
                    LibraryDocumentChunk.normalized_content.contains(phrase)
                    for phrase in normalized_phrases
                ]
                exact = (
                    or_(*phrase_checks)
                    if phrase_checks
                    else LibraryDocumentChunk.normalized_content.contains(normalized)
                )
                rows = session.execute(
                    base.add_columns(rank.label("rank"), exact.label("exact"))
                    .where(vector.op("@@")(tsquery))
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
