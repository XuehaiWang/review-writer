"""User-isolated native Library catalog and file lifecycle."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import WorkflowError, WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workspaces import HostedWorkspaceManager


class MinerUPreciseParseFailed(WorkflowError):
    code = "MINERU_PRECISE_PARSE_FAILED"
    status_code = 502


@dataclass(frozen=True)
class LibraryPaperRecord:
    id: str
    paper_id: str
    title: str
    authors: list[Any]
    keywords: list[Any]
    tags: Any
    original_filename: str
    content_sha256: str
    metadata: dict[str, Any]
    pdf_relative_path: str
    markdown_relative_path: str
    updated_at: str


def _field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


class LibraryService:
    MAX_PDF_BYTES = 80 * 1024 * 1024

    def __init__(
        self,
        session_factory,
        workspace_manager: HostedWorkspaceManager,
        *,
        precise_ingest: Callable[[Path, str, Path], dict[str, Any]] | None = None,
        runtime_environment: Callable[[Principal], dict[str, str]] | None = None,
    ):
        self.session_factory = session_factory
        self.workspace_manager = workspace_manager
        self.precise_ingest = precise_ingest or self._legacy_precise_ingest
        self.runtime_environment = runtime_environment

    @staticmethod
    def _legacy_precise_ingest(root: Path, filename: str, staged_pdf: Path) -> dict[str, Any]:
        from view.local_pdf_ingestion import ingest_local_pdf

        return ingest_local_pdf(root, filename, staged_pdf)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        from view.local_pdf_ingestion import sanitize_pdf_filename

        try:
            return sanitize_pdf_filename(filename)
        except ValueError as exc:
            raise WorkflowValidationError(str(exc)) from exc

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _relative_file(root: Path, raw_path: Any, *, label: str) -> str:
        raw = str(raw_path or "").strip()
        if not raw:
            raise MinerUPreciseParseFailed(f"MinerU did not produce the required {label} file.")
        candidate = Path(raw)
        candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise MinerUPreciseParseFailed(f"MinerU {label} escaped the user workspace.") from exc
        if not candidate.is_file():
            raise MinerUPreciseParseFailed(f"MinerU did not produce the required {label} file.")
        return relative

    @staticmethod
    def _record(row: LibraryPaper) -> LibraryPaperRecord:
        return LibraryPaperRecord(
            id=str(row.id),
            paper_id=row.paper_id,
            title=row.title,
            authors=list(row.authors_json or []),
            keywords=list(row.keywords_json or []),
            tags=row.tags_json,
            original_filename=row.original_filename,
            content_sha256=row.content_sha256,
            metadata=dict(row.metadata_json or {}),
            pdf_relative_path=row.pdf_relative_path,
            markdown_relative_path=row.markdown_relative_path,
            updated_at=row.updated_at.isoformat(),
        )

    def stage_upload(self, principal: Principal, filename: str, content: bytes) -> tuple[Path, str]:
        principal.require(Permission.PROJECT_WRITE)
        safe_name = self._safe_filename(filename)
        if not content:
            raise WorkflowValidationError("The uploaded PDF is empty.")
        if len(content) > self.MAX_PDF_BYTES:
            raise WorkflowValidationError("Each PDF must be 80 MB or smaller.")
        if not content[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith(b"%PDF-"):
            raise WorkflowValidationError("The uploaded file does not contain a PDF signature.")
        root = self.workspace_manager.user_root(principal.user_id)
        staging = root / "review-library" / ".upload-staging"
        if staging.is_symlink():
            raise WorkflowValidationError("Library upload staging is not trusted.")
        staging.mkdir(parents=True, exist_ok=True)
        path = staging / f"{uuid.uuid4()}.pdf.part"
        path.write_bytes(content)
        return path, safe_name

    def begin_upload(self, principal: Principal, filename: str) -> tuple[Path, str]:
        principal.require(Permission.PROJECT_WRITE)
        safe_name = self._safe_filename(filename)
        root = self.workspace_manager.user_root(principal.user_id)
        staging = root / "review-library" / ".upload-staging"
        if staging.is_symlink():
            raise WorkflowValidationError("Library upload staging is not trusted.")
        staging.mkdir(parents=True, exist_ok=True)
        return staging / f"{uuid.uuid4()}.pdf.part", safe_name

    def validate_staged_upload(self, path: Path, size: int) -> None:
        if size <= 0:
            raise WorkflowValidationError("The uploaded PDF is empty.")
        if size > self.MAX_PDF_BYTES:
            raise WorkflowValidationError("Each PDF must be 80 MB or smaller.")
        with path.open("rb") as handle:
            head = handle.read(1024).lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
        if not head.startswith(b"%PDF-"):
            raise WorkflowValidationError("The uploaded file does not contain a PDF signature.")

    def admit_staged(self, principal: Principal, filename: str, staged_pdf: Path) -> tuple[LibraryPaperRecord, str]:
        principal.require(Permission.PROJECT_WRITE)
        root = self.workspace_manager.user_root(principal.user_id)
        digest = self._digest(staged_pdf)
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            duplicate = session.scalar(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == user_uuid,
                    LibraryPaper.content_sha256 == digest,
                    LibraryPaper.deleted_at.is_(None),
                )
            )
            if duplicate is not None:
                return self._record(duplicate), "duplicate_file"
        try:
            if self.runtime_environment is not None:
                from view.provider_settings import register_runtime_provider_environment

                register_runtime_provider_environment(
                    root,
                    self.runtime_environment(principal),
                    isolated=True,
                )
            result = self.precise_ingest(root, filename, staged_pdf)
        except MinerUPreciseParseFailed:
            raise
        except RuntimeError as exc:
            raise MinerUPreciseParseFailed(str(exc)) from exc
        if not result.get("mineru_ready"):
            raise MinerUPreciseParseFailed(
                "MinerU precise parsing was incomplete; the PDF was not admitted to Library."
            )
        return self._record_parsed_result(principal, filename, digest, result)

    def _record_parsed_result(
        self,
        principal: Principal,
        filename: str,
        digest: str,
        result: dict[str, Any],
    ) -> tuple[LibraryPaperRecord, str]:
        """Persist one already-produced Library PDF/metadata/Markdown triplet."""
        root = self.workspace_manager.user_root(principal.user_id)
        user_uuid = uuid.UUID(principal.user_id)
        metadata_relative = self._relative_file(
            root, result.get("metadata_path"), label="metadata"
        )
        metadata_path = root / Path(*PurePosixPath(metadata_relative).parts)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MinerUPreciseParseFailed("MinerU metadata is unreadable.") from exc
        if not isinstance(metadata, dict):
            raise MinerUPreciseParseFailed("MinerU metadata has an invalid structure.")
        pdf_relative = self._relative_file(root, result.get("pdf_path"), label="PDF")
        markdown_relative = self._relative_file(
            root, result.get("markdown_path"), label="Markdown"
        )
        paper_id = str(result.get("paper_id") or metadata.get("paper_id") or "").strip()
        if not paper_id:
            raise MinerUPreciseParseFailed("MinerU metadata is missing paper_id.")
        title = str(_field_value(metadata.get("title")) or result.get("title") or paper_id)
        authors = _field_value(metadata.get("authors")) or []
        keywords = _field_value(metadata.get("keywords")) or []
        tags = _field_value(metadata.get("structured_tags")) or {}
        row = LibraryPaper(
            user_id=user_uuid,
            paper_id=paper_id,
            content_sha256=digest,
            original_filename=filename,
            title=title,
            authors_json=authors if isinstance(authors, list) else [authors],
            keywords_json=keywords if isinstance(keywords, list) else [keywords],
            tags_json=tags,
            metadata_json=metadata,
            pdf_relative_path=pdf_relative,
            markdown_relative_path=markdown_relative,
            status="active",
        )
        try:
            with database_session(self.session_factory) as session:
                session.add(row)
                session.flush()
                return self._record(row), str(result.get("status") or "uploaded")
        except IntegrityError:
            with database_session(self.session_factory) as session:
                duplicate = session.scalar(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == user_uuid,
                        LibraryPaper.content_sha256 == digest,
                        LibraryPaper.deleted_at.is_(None),
                    )
                )
                if duplicate is None:
                    raise
                return self._record(duplicate), "duplicate_file"

    def reconcile_download_result(self, principal: Principal, result: dict[str, Any]) -> list[LibraryPaperRecord]:
        """Index successfully acquired Library files after the job has produced them."""
        principal.require(Permission.PROJECT_WRITE)
        root = self.workspace_manager.user_root(principal.user_id)
        admitted: list[LibraryPaperRecord] = []
        for entry in result.get("results") or []:
            if not isinstance(entry, dict) or entry.get("status") != "downloaded":
                continue
            pdf_relative = self._relative_file(root, entry.get("path"), label="PDF")
            pdf_path = root / Path(*PurePosixPath(pdf_relative).parts)
            metadata_relative = self._relative_file(
                root, entry.get("metadata_path"), label="metadata"
            )
            metadata_path = root / Path(*PurePosixPath(metadata_relative).parts)
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MinerUPreciseParseFailed("Library download metadata is unreadable.") from exc
            record, _outcome = self._record_parsed_result(
                principal,
                pdf_path.name,
                self._digest(pdf_path),
                {
                    **entry,
                    "pdf_path": str(pdf_path),
                    "markdown_path": (
                        (metadata.get("source_paths") or {}).get("markdown")
                    ),
                    "mineru_ready": True,
                },
            )
            admitted.append(record)
        return admitted

    def count(self, principal: Principal) -> int:
        return len(self.list(principal))

    def list(self, principal: Principal, query: str = "") -> list[LibraryPaperRecord]:
        principal.require(Permission.PROJECT_READ)
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            rows = session.scalars(
                select(LibraryPaper)
                .where(
                    LibraryPaper.user_id == user_uuid,
                    LibraryPaper.deleted_at.is_(None),
                )
                .order_by(LibraryPaper.updated_at.desc())
            ).all()
            records = [self._record(row) for row in rows]
        needle = str(query or "").strip().casefold()
        if not needle:
            return records
        return [
            record
            for record in records
            if needle
            in " ".join(
                [
                    record.title,
                    " ".join(map(str, record.authors)),
                    " ".join(map(str, record.keywords)),
                    json.dumps(record.tags, ensure_ascii=False),
                ]
            ).casefold()
        ]

    def get(self, principal: Principal, paper_id: str) -> LibraryPaperRecord:
        principal.require(Permission.PROJECT_READ)
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == user_uuid,
                    LibraryPaper.paper_id == paper_id,
                    LibraryPaper.deleted_at.is_(None),
                )
            )
            if row is None:
                raise WorkflowNotFound("Library paper not found.")
            return self._record(row)

    def update_metadata(
        self, principal: Principal, paper_id: str, metadata: dict[str, Any]
    ) -> LibraryPaperRecord:
        principal.require(Permission.PROJECT_WRITE)
        if not isinstance(metadata, dict) or str(metadata.get("paper_id") or paper_id) != paper_id:
            raise WorkflowValidationError("Metadata paper_id cannot be changed.")
        user_uuid = uuid.UUID(principal.user_id)
        root = self.workspace_manager.user_root(principal.user_id)
        compatibility_path = root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
        compatibility_path.parent.mkdir(parents=True, exist_ok=True)
        previous = compatibility_path.read_bytes() if compatibility_path.is_file() else None
        temporary = compatibility_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(compatibility_path)
        try:
            with database_session(self.session_factory) as session:
                row = session.scalar(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == user_uuid,
                        LibraryPaper.paper_id == paper_id,
                        LibraryPaper.deleted_at.is_(None),
                    )
                )
                if row is None:
                    raise WorkflowNotFound("Library paper not found.")
                title = _field_value(metadata.get("title")) or row.title
                authors = _field_value(metadata.get("authors")) or []
                keywords = _field_value(metadata.get("keywords")) or []
                row.title = str(title)
                row.authors_json = authors if isinstance(authors, list) else [authors]
                row.keywords_json = keywords if isinstance(keywords, list) else [keywords]
                row.tags_json = _field_value(metadata.get("structured_tags")) or {}
                row.metadata_json = dict(metadata)
                row.updated_at = utc_now()
                session.flush()
                return self._record(row)
        except Exception:
            if previous is None:
                compatibility_path.unlink(missing_ok=True)
            else:
                compatibility_path.write_bytes(previous)
            raise

    @staticmethod
    def _safe_stored_path(root: Path, relative_path: str) -> Path:
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
            raise WorkflowNotFound("Library file not found.")
        path = (root / Path(*posix.parts)).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise WorkflowNotFound("Library file not found.") from exc
        if not path.is_file():
            raise WorkflowNotFound("Library file not found.")
        return path

    def file(self, principal: Principal, paper_id: str, kind: str) -> Path:
        record = self.get(principal, paper_id)
        relative = (
            record.pdf_relative_path if kind == "pdf" else record.markdown_relative_path
        )
        return self._safe_stored_path(
            self.workspace_manager.user_root(principal.user_id), relative
        )

    def delete(self, principal: Principal, paper_id: str) -> None:
        principal.require(Permission.PROJECT_DELETE)
        record = self.get(principal, paper_id)
        root = self.workspace_manager.user_root(principal.user_id)
        trash_root = root / ".trash" / "library"
        if trash_root.is_symlink():
            raise WorkflowValidationError("Library trash is not trusted.")
        trash = trash_root / f"{paper_id}-{uuid.uuid4()}"
        trash.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        try:
            for relative in (record.pdf_relative_path, record.markdown_relative_path):
                source = self._safe_stored_path(root, relative)
                destination = trash / source.name
                if source.stat().st_dev != trash.stat().st_dev:
                    raise WorkflowValidationError("Library files and trash must share a filesystem.")
                source.replace(destination)
                moved.append((destination, source))
            with database_session(self.session_factory) as session:
                row = session.get(LibraryPaper, uuid.UUID(record.id))
                row.status = "deleted"
                row.deleted_at = utc_now()
                row.updated_at = utc_now()
        except Exception:
            for destination, source in reversed(moved):
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
            shutil.rmtree(trash, ignore_errors=True)
            raise
