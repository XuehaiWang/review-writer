"""User-isolated native Library catalog and file lifecycle."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import BoundedSemaphore
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import WorkflowError, WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_api.scientific_runner import (
    SENSITIVE_ENVIRONMENT_KEY,
    ScientificRunError,
    ScientificRunner,
)


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
    artifact_ids: dict[str, str]
    updated_at: str


def _field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _scientific_failure_diagnostic(exc: ScientificRunError) -> str:
    stderr = str((getattr(exc, "details", None) or {}).get("stderr") or "")
    for raw_line in reversed(stderr.splitlines()):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or line.startswith(("Traceback ", "File ", "REVIEW_WRITER_ERROR:")):
            continue
        if set(line) <= {"^", "~", "-", " "}:
            continue
        return line[:1800]
    return ""


class LibraryService:
    MAX_PDF_BYTES = 80 * 1024 * 1024
    PAPER_ID = re.compile(r"^P[0-9]{1,93}$")
    SAFE_MINERU_ASSET_SUFFIXES = frozenset(
        {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    )

    def __init__(
        self,
        session_factory,
        workspace_manager: HostedWorkspaceManager,
        *,
        precise_ingest: Callable[[Path, str, Path], dict[str, Any]] | None = None,
        runtime_environment: Callable[[Principal], dict[str, str]] | None = None,
        scientific_runner: ScientificRunner | None = None,
    ):
        self.session_factory = session_factory
        self.workspace_manager = workspace_manager
        self.precise_ingest = precise_ingest
        self.runtime_environment = runtime_environment
        self.scientific_runner = scientific_runner or ScientificRunner()
        self._parse_slots = BoundedSemaphore(2)

    def _native_precise_ingest(
        self,
        principal: Principal,
        root: Path,
        filename: str,
        staged_pdf: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        output = staged_pdf.parent / f"{staged_pdf.stem}.parse-result.json"
        parse_root = staged_pdf.parent / f"{staged_pdf.stem}.parse-workspace"
        if parse_root.is_symlink():
            raise MinerUPreciseParseFailed("MinerU staging workspace is not trusted.")
        parse_root.mkdir(exist_ok=False)
        environment = (
            self.runtime_environment(principal) if self.runtime_environment else {}
        )
        normal = {
            key: value
            for key, value in environment.items()
            if not SENSITIVE_ENVIRONMENT_KEY.search(key)
        }
        secrets = {
            key: value
            for key, value in environment.items()
            if SENSITIVE_ENVIRONMENT_KEY.search(key)
        }
        result_ready = False
        try:
            self.scientific_runner.run(
                [
                    sys.executable,
                    "-m",
                    "review_writer_api.scientific_tasks",
                    "precise-ingest",
                    "--review-root",
                    str(parse_root),
                    "--filename",
                    filename,
                    "--input",
                    str(staged_pdf),
                    "--output",
                    str(output),
                ],
                cwd=Path(__file__).resolve().parents[2],
                staging_directory=staged_pdf.parent,
                expected_outputs=(output.name,),
                env=normal,
                secret_env=secrets,
                cancel_requested=cancel_requested,
                timeout_seconds=35 * 60,
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise MinerUPreciseParseFailed(
                    "MinerU precise parsing returned an invalid result."
                )
            result["_staging_root"] = str(parse_root)
            result_ready = True
            return result
        except ScientificRunError as exc:
            diagnostic = _scientific_failure_diagnostic(exc)
            message = str(exc).strip() or "Scientific task failed."
            if diagnostic and diagnostic not in message:
                message = f"{message} {diagnostic}"
            source_details = dict(getattr(exc, "details", None) or {})
            public_details = {
                key: source_details[key]
                for key in ("returncode", "category", "provider_call_completed")
                if key in source_details
            }
            if diagnostic:
                public_details["diagnostic"] = diagnostic
            raise MinerUPreciseParseFailed(
                message,
                details=public_details,
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise MinerUPreciseParseFailed(
                "MinerU precise parsing returned an unreadable result."
            ) from exc
        finally:
            output.unlink(missing_ok=True)
            if not result_ready:
                shutil.rmtree(parse_root, ignore_errors=True)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(str(filename or "").replace("\\", "/")).name.strip()
        name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).rstrip(". ")
        if not name or name in {".", ".."} or Path(name).suffix.casefold() != ".pdf":
            raise WorkflowValidationError(
                "Only .pdf files with a valid name can be uploaded."
            )
        return f"{Path(name).stem[:180].strip('. ') or 'uploaded-paper'}.pdf"

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
            raise MinerUPreciseParseFailed(
                f"MinerU did not produce the required {label} file."
            )
        candidate = Path(raw)
        candidate = (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise MinerUPreciseParseFailed(
                f"MinerU {label} escaped the user workspace."
            ) from exc
        if not candidate.is_file():
            raise MinerUPreciseParseFailed(
                f"MinerU did not produce the required {label} file."
            )
        return relative

    @classmethod
    def _validated_paper_id(cls, value: Any) -> str:
        paper_id = str(value or "").strip()
        if not cls.PAPER_ID.fullmatch(paper_id):
            raise WorkflowValidationError(
                "Library paper_id must be a P-prefixed numeric identifier."
            )
        return paper_id

    @staticmethod
    def _new_paper_id() -> str:
        """Generate a collision-resistant numeric P identifier without shared counters."""

        return f"P{uuid.uuid4().int}"

    @staticmethod
    def _is_isolated_output(root: Path, source: Path) -> bool:
        try:
            relative = source.resolve().relative_to(root.resolve())
        except ValueError:
            return False
        parts = set(relative.parts)
        return ".upload-staging" in parts or "job-staging" in parts

    def _write_compatibility_metadata(
        self, principal: Principal, paper_id: str, metadata: dict[str, Any]
    ) -> Path:
        directory = self.workspace_manager.trusted_user_directory(
            principal.user_id, "review-library", "metadata", "papers"
        )
        destination = directory / f"{paper_id}.metadata.json"
        if destination.is_symlink():
            raise WorkflowValidationError("Library metadata is not trusted.")
        temporary = directory / f".{paper_id}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _publish_library_triplet(
        self,
        principal: Principal,
        paper_id: str,
        *,
        pdf_source: Path,
        markdown_source: Path,
        metadata: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any], list[LibraryArtifact]]:
        """Copy validated outputs to unique immutable paths before catalog commit."""

        root = self.workspace_manager.user_root(principal.user_id)
        artifacts_root = self.workspace_manager.trusted_user_directory(
            principal.user_id, "review-library", ".artifacts"
        )
        paper_root = artifacts_root / paper_id
        if paper_root.is_symlink():
            raise WorkflowValidationError("Library artifact directory is not trusted.")
        paper_root.mkdir(exist_ok=True)
        artifact_ids = {
            "pdf": str(uuid.uuid4()),
            "markdown": str(uuid.uuid4()),
            "metadata": str(uuid.uuid4()),
        }

        def publish_file(kind: str, source: Path, suffix: str) -> Path:
            version = paper_root / artifact_ids[kind]
            version.mkdir(exist_ok=False)
            destination = version / f"{paper_id}{suffix}"
            temporary = version / f".{destination.name}.part"
            try:
                shutil.copy2(source, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination

        pdf_destination = publish_file("pdf", pdf_source, ".pdf")
        markdown_destination = publish_file("markdown", markdown_source, ".md")
        stored_metadata = dict(metadata)
        stored_metadata["paper_id"] = paper_id
        source_paths = dict(stored_metadata.get("source_paths") or {})
        source_paths["pdf"] = str(pdf_destination)
        source_paths["markdown"] = str(markdown_destination)
        mineru_content_destination: Path | None = None
        raw_extracted = str(source_paths.get("extracted_dir") or "").strip()
        raw_content_list = str(source_paths.get("content_list") or "").strip()
        if raw_extracted and raw_content_list:
            extracted_source = Path(raw_extracted).resolve()
            content_source = Path(raw_content_list).resolve()
            try:
                extracted_source.relative_to(root)
                content_relative = content_source.relative_to(extracted_source)
            except ValueError as exc:
                raise WorkflowValidationError(
                    "MinerU extracted output escaped the user workspace."
                ) from exc
            if not extracted_source.is_dir() or not content_source.is_file():
                raise WorkflowValidationError(
                    "MinerU extracted output is incomplete."
                )
            for item in extracted_source.rglob("*"):
                if item.is_symlink() or (
                    hasattr(item, "is_junction") and item.is_junction()
                ):
                    raise WorkflowValidationError(
                        "MinerU extracted output cannot contain symbolic links or junctions."
                    )
                try:
                    item.resolve().relative_to(extracted_source)
                except ValueError as exc:
                    raise WorkflowValidationError(
                        "MinerU extracted output escaped its directory."
                    ) from exc
            artifact_ids["mineru"] = str(uuid.uuid4())
            mineru_version = paper_root / artifact_ids["mineru"]
            mineru_destination = mineru_version / "extracted"
            shutil.copytree(extracted_source, mineru_destination)
            mineru_content_destination = mineru_destination / content_relative
            if not mineru_content_destination.is_file():
                raise WorkflowValidationError(
                    "Published MinerU content list is unavailable."
                )
            source_paths["extracted_dir"] = str(mineru_destination)
            source_paths["content_list"] = str(mineru_content_destination)
            extraction = dict(stored_metadata.get("extraction") or {})
            extraction_inputs = dict(extraction.get("inputs") or {})
            extraction_inputs["extracted_dir"] = str(mineru_destination)
            extraction_inputs["content_list"] = str(mineru_content_destination)
            extraction["inputs"] = extraction_inputs
            stored_metadata["extraction"] = extraction
        stored_metadata["source_paths"] = source_paths
        source_file = dict(stored_metadata.get("source_file") or {})
        source_file["pdf_name"] = pdf_destination.name
        source_file["relative_pdf_path"] = pdf_destination.relative_to(root).as_posix()
        source_file["sha256"] = self._digest(pdf_destination)
        stored_metadata["source_file"] = source_file
        artifact_paths = {
            "pdf": pdf_destination.relative_to(root).as_posix(),
            "markdown": markdown_destination.relative_to(root).as_posix(),
        }
        if mineru_content_destination is not None:
            artifact_paths["mineru"] = mineru_content_destination.relative_to(
                root
            ).as_posix()
        stored_metadata["_artifact_ids"] = dict(artifact_ids)
        metadata_version = paper_root / artifact_ids["metadata"]
        metadata_version.mkdir(exist_ok=False)
        metadata_destination = metadata_version / f"{paper_id}.metadata.json"
        artifact_paths["metadata"] = metadata_destination.relative_to(root).as_posix()
        stored_metadata["_artifact_paths"] = artifact_paths
        with metadata_destination.open("x", encoding="utf-8") as handle:
            handle.write(
                json.dumps(stored_metadata, ensure_ascii=False, indent=2) + "\n"
            )
        artifact_rows: list[LibraryArtifact] = []
        for kind, path in (
            ("pdf", pdf_destination),
            ("markdown", markdown_destination),
            ("metadata", metadata_destination),
            ("mineru", mineru_content_destination),
        ):
            if path is None:
                continue
            stat = path.stat()
            artifact_rows.append(
                LibraryArtifact(
                    id=uuid.UUID(artifact_ids[kind]),
                    user_id=uuid.UUID(principal.user_id),
                    paper_id=paper_id,
                    kind=kind,
                    relative_path=path.relative_to(root).as_posix(),
                    content_sha256=self._digest(path),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    availability="available",
                )
            )
        return (
            artifact_paths["pdf"],
            artifact_paths["markdown"],
            stored_metadata,
            artifact_rows,
        )

    def _publish_metadata_version(
        self,
        principal: Principal,
        paper_id: str,
        metadata: dict[str, Any],
        current: LibraryPaperRecord,
    ) -> tuple[dict[str, Any], LibraryArtifact]:
        artifacts_root = self.workspace_manager.trusted_user_directory(
            principal.user_id, "review-library", ".artifacts"
        )
        paper_root = artifacts_root / paper_id
        if paper_root.is_symlink():
            raise WorkflowValidationError("Library artifact directory is not trusted.")
        paper_root.mkdir(exist_ok=True)
        metadata_artifact_id = str(uuid.uuid4())
        version = paper_root / metadata_artifact_id
        version.mkdir(exist_ok=False)
        destination = version / f"{paper_id}.metadata.json"
        stored = dict(metadata)
        stored["paper_id"] = paper_id
        source_paths = dict(stored.get("source_paths") or {})
        root = self.workspace_manager.user_root(principal.user_id)
        source_paths["pdf"] = str(
            root / Path(*PurePosixPath(current.pdf_relative_path).parts)
        )
        source_paths["markdown"] = str(
            root / Path(*PurePosixPath(current.markdown_relative_path).parts)
        )
        stored["source_paths"] = source_paths
        artifact_ids = dict(current.artifact_ids)
        artifact_ids["metadata"] = metadata_artifact_id
        artifact_paths = dict((current.metadata or {}).get("_artifact_paths") or {})
        artifact_paths["metadata"] = destination.relative_to(root).as_posix()
        stored["_artifact_ids"] = artifact_ids
        stored["_artifact_paths"] = artifact_paths
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(stored, ensure_ascii=False, indent=2) + "\n")
        stat = destination.stat()
        artifact = LibraryArtifact(
            id=uuid.UUID(metadata_artifact_id),
            user_id=uuid.UUID(principal.user_id),
            paper_id=paper_id,
            kind="metadata",
            relative_path=destination.relative_to(root).as_posix(),
            content_sha256=self._digest(destination),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            availability="available",
        )
        return stored, artifact

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
            artifact_ids=dict((row.metadata_json or {}).get("_artifact_ids") or {}),
            updated_at=row.updated_at.isoformat(),
        )

    def stage_upload(
        self, principal: Principal, filename: str, content: bytes
    ) -> tuple[Path, str]:
        principal.require(Permission.PROJECT_WRITE)
        safe_name = self._safe_filename(filename)
        if not content:
            raise WorkflowValidationError("The uploaded PDF is empty.")
        if len(content) > self.MAX_PDF_BYTES:
            raise WorkflowValidationError("Each PDF must be 80 MB or smaller.")
        if not content[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith(b"%PDF-"):
            raise WorkflowValidationError("The uploaded file does not contain a PDF signature.")
        staging = self.workspace_manager.trusted_user_directory(
            principal.user_id, "review-library", ".upload-staging"
        )
        path = staging / f"{uuid.uuid4()}.pdf.part"
        path.write_bytes(content)
        return path, safe_name

    def begin_upload(self, principal: Principal, filename: str) -> tuple[Path, str]:
        principal.require(Permission.PROJECT_WRITE)
        safe_name = self._safe_filename(filename)
        staging = self.workspace_manager.trusted_user_directory(
            principal.user_id, "review-library", ".upload-staging"
        )
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

    def admit_staged(
        self,
        principal: Principal,
        filename: str,
        staged_pdf: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[LibraryPaperRecord, str]:
        principal.require(Permission.PROJECT_WRITE)
        root = self.workspace_manager.user_root(principal.user_id)
        digest = self._digest(staged_pdf)
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            duplicate = session.scalar(
                select(LibraryPaper)
                .where(
                    LibraryPaper.user_id == user_uuid,
                    LibraryPaper.content_sha256 == digest,
                    LibraryPaper.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if duplicate is not None:
                return self._record(duplicate), "duplicate_file"
        try:
            if self.precise_ingest:
                result = self.precise_ingest(root, filename, staged_pdf)
            else:
                while not self._parse_slots.acquire(timeout=0.25):
                    if cancel_requested is not None and cancel_requested():
                        raise MinerUPreciseParseFailed(
                            "MinerU precise parsing was cancelled."
                        )
                try:
                    result = self._native_precise_ingest(
                        principal,
                        root,
                        filename,
                        staged_pdf,
                        cancel_requested=cancel_requested,
                    )
                finally:
                    self._parse_slots.release()
        except MinerUPreciseParseFailed:
            raise
        except RuntimeError as exc:
            raise MinerUPreciseParseFailed(str(exc)) from exc
        if not result.get("mineru_ready"):
            raise MinerUPreciseParseFailed(
                "MinerU precise parsing was incomplete; the PDF was not admitted to Library."
            )
        cleanup_root = Path(str(result.get("_staging_root") or ""))
        try:
            return self._record_parsed_result(principal, filename, digest, result)
        finally:
            if cleanup_root.name.endswith(".parse-workspace"):
                try:
                    cleanup_root.resolve().relative_to(staged_pdf.parent.resolve())
                except ValueError:
                    pass
                else:
                    shutil.rmtree(cleanup_root, ignore_errors=True)

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
        source_pdf_relative = self._relative_file(
            root, result.get("pdf_path"), label="PDF"
        )
        source_markdown_relative = self._relative_file(
            root, result.get("markdown_path"), label="Markdown"
        )
        pdf_source = root / Path(*PurePosixPath(source_pdf_relative).parts)
        markdown_source = root / Path(*PurePosixPath(source_markdown_relative).parts)
        try:
            paper_id = self._validated_paper_id(
                result.get("paper_id") or metadata.get("paper_id")
            )
        except WorkflowValidationError as exc:
            raise MinerUPreciseParseFailed("MinerU metadata has an invalid paper_id.") from exc
        try:
            with database_session(self.session_factory) as session:
                reusable = session.scalar(
                    select(LibraryPaper)
                    .where(
                        LibraryPaper.user_id == user_uuid,
                        LibraryPaper.content_sha256 == digest,
                    )
                    .with_for_update()
                )
                if reusable is not None and reusable.deleted_at is None:
                    return self._record(reusable), "duplicate_file"
                occupied = None
                if reusable is None:
                    occupied = session.scalar(
                        select(LibraryPaper.id).where(
                            LibraryPaper.user_id == user_uuid,
                            LibraryPaper.paper_id == paper_id,
                        )
                    )
                if reusable is not None:
                    paper_id = reusable.paper_id
                elif occupied is not None or self._is_isolated_output(
                    root, pdf_source
                ):
                    paper_id = self._new_paper_id()
                pdf_relative, markdown_relative, metadata, artifact_rows = (
                    self._publish_library_triplet(
                        principal,
                        paper_id,
                        pdf_source=pdf_source,
                        markdown_source=markdown_source,
                        metadata=metadata,
                    )
                )
                title = str(
                    _field_value(metadata.get("title"))
                    or result.get("title")
                    or paper_id
                )
                authors = _field_value(metadata.get("authors")) or []
                keywords = _field_value(metadata.get("keywords")) or []
                tags = _field_value(metadata.get("structured_tags")) or {}
                session.add_all(artifact_rows)
                if reusable is None:
                    row = LibraryPaper(
                        user_id=user_uuid,
                        paper_id=paper_id,
                        content_sha256=digest,
                        original_filename=filename,
                        title=title,
                        authors_json=(
                            authors if isinstance(authors, list) else [authors]
                        ),
                        keywords_json=(
                            keywords if isinstance(keywords, list) else [keywords]
                        ),
                        tags_json=tags,
                        metadata_json=metadata,
                        pdf_relative_path=pdf_relative,
                        markdown_relative_path=markdown_relative,
                        status="active",
                    )
                    session.add(row)
                    outcome = str(result.get("status") or "uploaded")
                else:
                    row = session.scalar(
                        select(LibraryPaper)
                        .where(LibraryPaper.id == reusable.id)
                        .with_for_update()
                    )
                    if row is None:
                        raise MinerUPreciseParseFailed(
                            "Deleted Library paper could not be restored."
                        )
                    row.original_filename = filename
                    row.title = title
                    row.authors_json = (
                        authors if isinstance(authors, list) else [authors]
                    )
                    row.keywords_json = (
                        keywords if isinstance(keywords, list) else [keywords]
                    )
                    row.tags_json = tags
                    row.metadata_json = metadata
                    row.pdf_relative_path = pdf_relative
                    row.markdown_relative_path = markdown_relative
                    row.status = "active"
                    row.deleted_at = None
                    row.updated_at = utc_now()
                    outcome = "restored"
                session.flush()
                record = self._record(row)
                self._write_compatibility_metadata(principal, paper_id, metadata)
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
        return record, outcome

    def reconcile_download_result(
        self, principal: Principal, result: dict[str, Any]
    ) -> list[LibraryPaperRecord]:
        """Index successfully acquired Library files after the job has produced them."""
        principal.require(Permission.PROJECT_WRITE)
        root = self.workspace_manager.user_root(principal.user_id)
        user_uuid = uuid.UUID(principal.user_id)
        prepared: list[dict[str, Any]] = []
        for entry in result.get("results") or []:
            if not isinstance(entry, dict) or entry.get("status") not in {
                "downloaded",
                "already_in_library",
                "duplicate_file",
            }:
                continue
            paper_id = self._validated_paper_id(entry.get("paper_id"))
            metadata_source = entry.get("metadata_path") or (
                root
                / "review-library"
                / "metadata"
                / "papers"
                / f"{paper_id}.metadata.json"
            )
            metadata_relative = self._relative_file(
                root, metadata_source, label="metadata"
            )
            metadata_path = root / Path(*PurePosixPath(metadata_relative).parts)
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MinerUPreciseParseFailed(
                    "Library download metadata is unreadable."
                ) from exc
            if not isinstance(metadata, dict):
                raise MinerUPreciseParseFailed(
                    "Library download metadata has an invalid structure."
                )
            metadata_paper_id = self._validated_paper_id(
                metadata.get("paper_id") or paper_id
            )
            if metadata_paper_id != paper_id:
                raise MinerUPreciseParseFailed(
                    "Library download metadata paper_id does not match its result."
                )
            source_paths = metadata.get("source_paths") or {}
            if not isinstance(source_paths, dict):
                raise MinerUPreciseParseFailed(
                    "Library download metadata source_paths are invalid."
                )
            source_pdf_relative = self._relative_file(
                root, entry.get("path") or source_paths.get("pdf"), label="PDF"
            )
            pdf_source = root / Path(*PurePosixPath(source_pdf_relative).parts)
            source_markdown_relative = self._relative_file(
                root, source_paths.get("markdown"), label="Markdown"
            )
            markdown_source = root / Path(
                *PurePosixPath(source_markdown_relative).parts
            )
            authors = _field_value(metadata.get("authors")) or []
            keywords = _field_value(metadata.get("keywords")) or []
            prepared.append(
                {
                    "entry": entry,
                    "suggested_paper_id": paper_id,
                    "content_sha256": self._digest(pdf_source),
                    "original_filename": pdf_source.name,
                    "title": str(_field_value(metadata.get("title")) or paper_id),
                    "authors": authors if isinstance(authors, list) else [authors],
                    "keywords": (
                        keywords if isinstance(keywords, list) else [keywords]
                    ),
                    "tags": _field_value(metadata.get("structured_tags")) or {},
                    "metadata": metadata,
                    "pdf_source": pdf_source,
                    "markdown_source": markdown_source,
                }
            )
        # Every input is validated before any immutable publication or catalog write.
        records: list[LibraryPaperRecord] = []
        compatibility: list[tuple[str, dict[str, Any]]] = []
        try:
            with database_session(self.session_factory) as session:
                inserted_by_digest: dict[str, LibraryPaper] = {}
                for item in prepared:
                    duplicate = session.scalar(
                        select(LibraryPaper)
                        .where(
                            LibraryPaper.user_id == user_uuid,
                            LibraryPaper.content_sha256 == item["content_sha256"],
                        )
                        .with_for_update()
                    )
                    if duplicate is not None and duplicate.deleted_at is None:
                        records.append(self._record(duplicate))
                        item["entry"].update(
                            {
                                "status": "already_in_library",
                                "paper_id": duplicate.paper_id,
                                "artifact_ids": dict(
                                    (duplicate.metadata_json or {}).get(
                                        "_artifact_ids"
                                    )
                                    or {}
                                ),
                            }
                        )
                        continue
                    inserted_duplicate = inserted_by_digest.get(
                        item["content_sha256"]
                    )
                    if inserted_duplicate is not None:
                        records.append(self._record(inserted_duplicate))
                        item["entry"].update(
                            {
                                "status": "duplicate_file",
                                "paper_id": inserted_duplicate.paper_id,
                                "artifact_ids": dict(
                                    (inserted_duplicate.metadata_json or {}).get(
                                        "_artifact_ids"
                                    )
                                    or {}
                                ),
                            }
                        )
                        continue
                    paper_id = item["suggested_paper_id"]
                    occupied = None
                    if duplicate is None:
                        occupied = session.scalar(
                            select(LibraryPaper.id).where(
                                LibraryPaper.user_id == user_uuid,
                                LibraryPaper.paper_id == paper_id,
                            )
                        )
                    if duplicate is not None:
                        paper_id = duplicate.paper_id
                    elif occupied is not None or self._is_isolated_output(
                        root, item["pdf_source"]
                    ):
                        paper_id = self._new_paper_id()
                    pdf_relative, markdown_relative, metadata, artifact_rows = (
                        self._publish_library_triplet(
                            principal,
                            paper_id,
                            pdf_source=item["pdf_source"],
                            markdown_source=item["markdown_source"],
                            metadata=item["metadata"],
                        )
                    )
                    session.add_all(artifact_rows)
                    if duplicate is None:
                        row = LibraryPaper(
                            user_id=user_uuid,
                            paper_id=paper_id,
                            content_sha256=item["content_sha256"],
                            original_filename=item["original_filename"],
                            title=item["title"],
                            authors_json=item["authors"],
                            keywords_json=item["keywords"],
                            tags_json=item["tags"],
                            metadata_json=metadata,
                            pdf_relative_path=pdf_relative,
                            markdown_relative_path=markdown_relative,
                            status="active",
                        )
                        session.add(row)
                        catalog_outcome = "downloaded"
                    else:
                        row = session.scalar(
                            select(LibraryPaper)
                            .where(LibraryPaper.id == duplicate.id)
                            .with_for_update()
                        )
                        if row is None:
                            raise MinerUPreciseParseFailed(
                                "Deleted Library paper could not be restored."
                            )
                        row.original_filename = item["original_filename"]
                        row.title = item["title"]
                        row.authors_json = item["authors"]
                        row.keywords_json = item["keywords"]
                        row.tags_json = item["tags"]
                        row.metadata_json = metadata
                        row.pdf_relative_path = pdf_relative
                        row.markdown_relative_path = markdown_relative
                        row.status = "active"
                        row.deleted_at = None
                        row.updated_at = utc_now()
                        catalog_outcome = "restored"
                    session.flush()
                    inserted_by_digest[item["content_sha256"]] = row
                    records.append(self._record(row))
                    compatibility.append((paper_id, metadata))
                    item["entry"].update(
                        {
                            "status": "downloaded",
                            "catalog_outcome": catalog_outcome,
                            "paper_id": paper_id,
                            "path": str(
                                root / Path(*PurePosixPath(pdf_relative).parts)
                            ),
                            "artifact_ids": dict(
                                metadata.get("_artifact_ids") or {}
                            ),
                        }
                    )
                for compatibility_paper_id, compatibility_metadata in compatibility:
                    self._write_compatibility_metadata(
                        principal, compatibility_paper_id, compatibility_metadata
                    )
        except IntegrityError as exc:
            raise MinerUPreciseParseFailed(
                "Library download catalog reconciliation conflicted."
            ) from exc
        outcomes = [
            str(entry.get("status") or "")
            for entry in result.get("results") or []
            if isinstance(entry, dict)
        ]
        result["added_count"] = sum(status == "downloaded" for status in outcomes)
        result["already_present_count"] = sum(
            status in {"already_in_library", "duplicate_file"}
            for status in outcomes
        )
        result["failed_count"] = sum(
            status not in {"downloaded", "already_in_library", "duplicate_file"}
            for status in outcomes
        )
        return records

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
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("paper_id") or paper_id) != paper_id
        ):
            raise WorkflowValidationError("Metadata paper_id cannot be changed.")
        user_uuid = uuid.UUID(principal.user_id)
        paper_id = self._validated_paper_id(paper_id)
        current = self.get(principal, paper_id)
        stored_metadata, metadata_artifact = self._publish_metadata_version(
            principal, paper_id, metadata, current
        )
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
            session.add(metadata_artifact)
            title = _field_value(stored_metadata.get("title")) or row.title
            authors = _field_value(stored_metadata.get("authors")) or []
            keywords = _field_value(stored_metadata.get("keywords")) or []
            row.title = str(title)
            row.authors_json = authors if isinstance(authors, list) else [authors]
            row.keywords_json = keywords if isinstance(keywords, list) else [keywords]
            row.tags_json = _field_value(stored_metadata.get("structured_tags")) or {}
            row.metadata_json = stored_metadata
            row.updated_at = utc_now()
            session.flush()
            record = self._record(row)
        self._write_compatibility_metadata(principal, paper_id, stored_metadata)
        return record

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

    def mineru_asset(self, principal: Principal, paper_id: str, raw_path: str) -> Path:
        """Resolve one file inside the current immutable MinerU extraction."""

        record = self.get(principal, paper_id)
        artifact_id = str(record.artifact_ids.get("mineru") or "")
        try:
            artifact_uuid = uuid.UUID(artifact_id)
        except ValueError as exc:
            raise WorkflowNotFound("Library file not found.") from exc
        with database_session(self.session_factory) as session:
            artifact = session.scalar(
                select(LibraryArtifact).where(
                    LibraryArtifact.id == artifact_uuid,
                    LibraryArtifact.user_id == uuid.UUID(principal.user_id),
                    LibraryArtifact.paper_id == record.paper_id,
                    LibraryArtifact.kind == "mineru",
                    LibraryArtifact.availability == "available",
                )
            )
        if artifact is None:
            raise WorkflowNotFound("Library file not found.")
        root = self.workspace_manager.user_root(principal.user_id)
        artifacts_root = root / "review-library" / ".artifacts"
        raw_version_root = artifacts_root / record.paper_id / artifact_id
        raw_extracted_root = raw_version_root / "extracted"
        for boundary in (
            root / "review-library",
            artifacts_root,
            artifacts_root / record.paper_id,
            raw_version_root,
            raw_extracted_root,
        ):
            if boundary.is_symlink() or (
                hasattr(boundary, "is_junction") and boundary.is_junction()
            ):
                raise WorkflowNotFound("Library file not found.")
        trusted_artifacts_root = artifacts_root.resolve()
        version_root = raw_version_root.resolve()
        try:
            version_root.relative_to(trusted_artifacts_root)
        except ValueError as exc:
            raise WorkflowNotFound("Library file not found.") from exc
        # Resolve the registered content-list leaf first so a forged database
        # path or symlinked immutable version can never widen the file scope.
        content_list = self._safe_stored_path(root, artifact.relative_path)
        extracted_root = (version_root / "extracted").resolve()
        try:
            content_list.relative_to(extracted_root)
        except ValueError as exc:
            raise WorkflowNotFound("Library file not found.") from exc
        normalized = str(raw_path or "").replace("\\", "/")
        relative = PurePosixPath(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or PureWindowsPath(normalized).drive
            or any(part in {"", ".", ".."} for part in relative.parts)
            or PurePosixPath(normalized).suffix.casefold()
            not in self.SAFE_MINERU_ASSET_SUFFIXES
        ):
            raise WorkflowNotFound("Library file not found.")
        candidate = extracted_root.joinpath(*relative.parts).resolve()
        try:
            candidate.relative_to(extracted_root)
        except ValueError as exc:
            raise WorkflowNotFound("Library file not found.") from exc
        current = raw_extracted_root
        if current.is_symlink() or not current.is_dir():
            raise WorkflowNotFound("Library file not found.")
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            ):
                raise WorkflowNotFound("Library file not found.")
        if not candidate.is_file():
            raise WorkflowNotFound("Library file not found.")
        return candidate

    def delete(self, principal: Principal, paper_id: str) -> None:
        principal.require(Permission.PROJECT_DELETE)
        paper_id = self._validated_paper_id(paper_id)
        root = self.workspace_manager.user_root(principal.user_id)
        trash_root = self.workspace_manager.trusted_user_directory(
            principal.user_id, ".trash", "library"
        )
        trash = trash_root / f"{paper_id}-{uuid.uuid4()}"
        trash.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        session = self.session_factory()
        try:
            row = session.scalar(
                select(LibraryPaper)
                .where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id == paper_id,
                    LibraryPaper.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                raise WorkflowNotFound("Library paper not found.")
            record = self._record(row)
            sources = [
                (self._safe_stored_path(root, record.pdf_relative_path), "paper.pdf"),
                (
                    self._safe_stored_path(root, record.markdown_relative_path),
                    "paper.md",
                ),
            ]
            artifact_metadata_relative = (record.metadata.get("_artifact_paths") or {}).get(
                "metadata"
            )
            if artifact_metadata_relative:
                sources.append(
                    (
                        self._safe_stored_path(root, artifact_metadata_relative),
                        "metadata-artifact.json",
                    )
                )
            mineru_artifact_id = record.artifact_ids.get("mineru")
            mineru_relative = (record.metadata.get("_artifact_paths") or {}).get(
                "mineru"
            )
            if mineru_artifact_id and mineru_relative:
                content_path = self._safe_stored_path(root, mineru_relative)
                version_root = (
                    root
                    / "review-library"
                    / ".artifacts"
                    / paper_id
                    / str(uuid.UUID(mineru_artifact_id))
                )
                raw_version_root = version_root
                version_root = version_root.resolve()
                try:
                    content_path.relative_to(version_root)
                    relative_version = version_root.relative_to(root)
                except ValueError as exc:
                    raise WorkflowValidationError(
                        "MinerU artifact path does not match its immutable version."
                    ) from exc
                current = root
                for part in relative_version.parts:
                    current = current / part
                    if current.is_symlink() or (
                        hasattr(current, "is_junction") and current.is_junction()
                    ):
                        raise WorkflowValidationError(
                            "MinerU artifact directory is not trusted."
                        )
                current = version_root
                for part in content_path.relative_to(version_root).parts:
                    current = current / part
                    if current.is_symlink() or (
                        hasattr(current, "is_junction") and current.is_junction()
                    ):
                        raise WorkflowValidationError(
                            "MinerU artifact content path is not trusted."
                        )
                if raw_version_root.is_symlink() or not version_root.is_dir():
                    raise WorkflowValidationError(
                        "MinerU artifact directory is unavailable."
                    )
                sources.append((version_root, "mineru-artifact"))
            compatibility_metadata = self.workspace_manager.trusted_user_directory(
                principal.user_id, "review-library", "metadata", "papers"
            ) / f"{paper_id}.metadata.json"
            if compatibility_metadata.is_symlink():
                raise WorkflowValidationError("Library metadata is not trusted.")
            if compatibility_metadata.is_file():
                sources.append((compatibility_metadata, "metadata-compatibility.json"))
            unique_sources: dict[Path, tuple[Path, str]] = {}
            for source, destination_name in sources:
                unique_sources.setdefault(
                    source.resolve(), (source, destination_name)
                )
            sources = list(unique_sources.values())
            for source, destination_name in sources:
                destination = trash / destination_name
                if source.stat().st_dev != trash.stat().st_dev:
                    raise WorkflowValidationError(
                        "Library files and trash must share a filesystem."
                    )
                source.replace(destination)
                moved.append((destination, source))
            if row is not None:
                current_artifact_ids = [
                    uuid.UUID(artifact_id)
                    for artifact_id in record.artifact_ids.values()
                ]
                if current_artifact_ids:
                    session.execute(
                        update(LibraryArtifact)
                        .where(
                            LibraryArtifact.user_id == uuid.UUID(principal.user_id),
                            LibraryArtifact.id.in_(current_artifact_ids),
                        )
                        .values(availability="trashed")
                    )
                row.status = "deleted"
                row.deleted_at = utc_now()
                row.updated_at = utc_now()
            session.commit()
        except Exception:
            session.rollback()
            for destination, source in reversed(moved):
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
            shutil.rmtree(trash, ignore_errors=True)
            raise
        finally:
            session.close()
