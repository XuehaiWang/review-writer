"""Immutable artifact publication and user-owned file resolution."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import select

from review_writer_api.database import database_session
from review_writer_api.errors import (
    ArtifactFileMissing,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_api.workflow_models import LibraryArtifact
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_api.persistent_storage import LocalPersistentStorage, PersistentStorage


@dataclass(frozen=True)
class ResolvedArtifact:
    artifact: ArtifactRecord
    path: Path


class ArtifactService:
    def __init__(
        self,
        repository: WorkflowRepository,
        workspace_manager: HostedWorkspaceManager,
        storage: PersistentStorage | None = None,
    ):
        self.repository = repository
        self.workspace_manager = workspace_manager
        self.storage = storage or LocalPersistentStorage()

    @staticmethod
    def _safe_relative(value: str, *, label: str) -> PurePosixPath:
        raw = str(value or "").strip()
        posix = PurePosixPath(raw.replace("\\", "/"))
        windows = PureWindowsPath(raw)
        if (
            not raw
            or Path(raw).is_absolute()
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise WorkflowValidationError(f"{label} must be a safe relative path.")
        return posix

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _lineage_sha256(metadata: dict[str, Any] | None) -> str:
        canonical = json.dumps(
            dict(metadata or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _secure_storage_root(parent: Path, name: str) -> Path:
        """Create one trusted internal directory without following a root symlink."""

        parent = parent.resolve()
        candidate = parent / name
        if candidate.is_symlink():
            raise WorkflowValidationError(
                f"Internal storage directory {name} must not be a symbolic link."
            )
        resolved = candidate.resolve()
        if resolved.parent != parent:
            raise WorkflowValidationError(
                f"Internal storage directory {name} escaped its workspace."
            )
        resolved.mkdir(parents=True, exist_ok=True)
        if candidate.is_symlink() or resolved.parent != parent:
            raise WorkflowValidationError(
                f"Internal storage directory {name} is not trusted."
            )
        return resolved

    def _project(self, user_id: str, project_id: str):
        project = self.repository.get_owned_project(user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

    def stage_run_directory(self, user_id: str, project_id: str, run_id: str) -> Path:
        project = self._project(user_id, project_id)
        run = self.repository.get_stage_run(user_id, project_id, run_id)
        if run is None:
            raise WorkflowNotFound("Stage run not found.")
        try:
            safe_run_id = str(uuid.UUID(str(run_id)))
        except ValueError as exc:
            raise WorkflowValidationError("Stage run ID is invalid.") from exc
        project_root = self.workspace_manager.project_path(user_id, project.slug)
        expected_parent = self._secure_storage_root(project_root, ".staging")
        directory = (expected_parent / safe_run_id).resolve()
        if directory.parent != expected_parent:
            raise WorkflowValidationError("Stage directory escaped the project workspace.")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def publish(
        self,
        user_id: str,
        project_id: str,
        run_id: str,
        staged_relative_path: str,
        *,
        logical_name: str,
        artifact_type: str,
        producer_stage: str,
        make_current: bool = True,
        validator: Callable[[Path], None] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        project = self._project(user_id, project_id)
        stage_directory = self.stage_run_directory(user_id, project_id, run_id)
        staged_relative = self._safe_relative(
            staged_relative_path, label="Staged artifact path"
        )
        logical = self._safe_relative(logical_name, label="Artifact logical name")
        source = (stage_directory / Path(*staged_relative.parts)).resolve()
        try:
            source.relative_to(stage_directory)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Staged artifact escaped the stage directory."
            ) from exc
        if not source.is_file():
            raise WorkflowValidationError("Staged artifact file does not exist.")
        if validator is not None:
            try:
                validator(source)
            except Exception as exc:
                raise WorkflowValidationError(
                    "Artifact validation failed.", details={"reason": str(exc)}
                ) from exc

        digest = self._sha256(source)
        artifact_metadata = dict(metadata or {})
        lineage_sha256 = self._lineage_sha256(artifact_metadata)
        existing = self.repository.get_artifact_by_content(
            user_id,
            project_id,
            logical.as_posix(),
            digest,
            lineage_sha256,
        )
        if existing is not None:
            source.unlink()
            if make_current:
                return self.repository.set_current_artifact(
                    user_id, project_id, logical.as_posix(), existing.id
                )
            return existing

        artifact_id = str(uuid.uuid4())
        project_root = self.workspace_manager.project_path(user_id, project.slug)
        artifacts_root = self._secure_storage_root(project_root, ".artifacts")
        destination = (
            artifacts_root
            / Path(*logical.parts)
            / artifact_id
            / source.name
        ).resolve()
        try:
            destination.relative_to(artifacts_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Artifact destination escaped the project workspace."
            ) from exc
        self.repository.require_bound_job_lease()
        stat = self.storage.commit_staged(source, destination)
        relative_path = destination.relative_to(project_root).as_posix()
        try:
            return self.repository.publish_artifact(
                user_id=user_id,
                project_id=project_id,
                artifact_id=artifact_id,
                logical_name=logical.as_posix(),
                artifact_type=str(
                    artifact_type or destination.suffix.lstrip(".") or "file"
                ),
                relative_path=relative_path,
                content_sha256=digest,
                lineage_sha256=lineage_sha256,
                size_bytes=stat.size_bytes,
                mtime_ns=stat.mtime_ns,
                producer_stage=producer_stage,
                producer_run_id=run_id,
                metadata=artifact_metadata,
                make_current=make_current,
            )
        except WorkflowConflict:
            # Two first-open requests can finish the same immutable artifact at
            # the same time (for example React StrictMode mounting an editor).
            # Treat the unique-content winner as the idempotent result and
            # remove only this request's unpublished destination.
            existing = self.repository.get_artifact_by_content(
                user_id,
                project_id,
                logical.as_posix(),
                digest,
                lineage_sha256,
            )
            if existing is None:
                destination.unlink(missing_ok=True)
                try:
                    destination.parent.rmdir()
                except OSError:
                    pass
                raise
            destination.unlink(missing_ok=True)
            try:
                destination.parent.rmdir()
            except OSError:
                pass
            if make_current:
                return self.repository.set_current_artifact(
                    user_id, project_id, logical.as_posix(), existing.id
                )
            return existing
        except Exception:
            # A lease loss or database failure must not expose unpublished
            # bytes as a usable artifact.
            destination.unlink(missing_ok=True)
            try:
                destination.parent.rmdir()
            except OSError:
                pass
            raise

    def resolve_owned_artifact(self, user_id: str, artifact_id: str) -> ResolvedArtifact:
        owned = self.repository.get_artifact(user_id, artifact_id)
        if owned is None:
            try:
                artifact_uuid = uuid.UUID(str(artifact_id))
                user_uuid = uuid.UUID(str(user_id))
            except ValueError as exc:
                raise WorkflowNotFound("Artifact not found.") from exc
            with database_session(self.repository.session_factory) as session:
                library = session.scalar(
                    select(LibraryArtifact).where(
                        LibraryArtifact.id == artifact_uuid,
                        LibraryArtifact.user_id == user_uuid,
                    )
                )
                if library is None:
                    raise WorkflowNotFound("Artifact not found.")
                relative = self._safe_relative(
                    library.relative_path, label="Stored Library artifact path"
                )
                user_root = self.workspace_manager.user_root(user_id)
                path = self.storage.resolve(user_root, Path(*relative.parts))
                try:
                    path.relative_to(user_root)
                except ValueError as exc:
                    raise WorkflowNotFound("Artifact not found.") from exc
                record = ArtifactRecord(
                    id=str(library.id),
                    project_id="",
                    logical_name=f"library/{library.paper_id}/{library.kind}",
                    artifact_type=library.kind,
                    relative_path=library.relative_path,
                    content_sha256=library.content_sha256,
                    lineage_sha256="",
                    size_bytes=library.size_bytes,
                    mtime_ns=library.mtime_ns,
                    availability=library.availability,
                    producer_stage="library",
                    producer_run_id=None,
                    metadata={"paper_id": library.paper_id, "kind": library.kind},
                    created_at=library.created_at,
                )
            if record.availability != "available" or not path.is_file():
                raise ArtifactFileMissing(
                    "The Library artifact record exists but its file is missing.",
                    details={"artifact_id": artifact_id},
                )
            return ResolvedArtifact(artifact=record, path=path)
        relative = self._safe_relative(
            owned.artifact.relative_path, label="Stored artifact path"
        )
        project_root = self.workspace_manager.project_path(user_id, owned.project_slug)
        path = self.storage.resolve(project_root, Path(*relative.parts))
        try:
            path.relative_to(project_root)
        except ValueError as exc:
            raise WorkflowNotFound("Artifact not found.") from exc
        if owned.artifact.availability != "available" or not path.is_file():
            raise ArtifactFileMissing(
                "The artifact record exists but its file is missing.",
                details={"artifact_id": artifact_id},
            )
        return ResolvedArtifact(artifact=owned.artifact, path=path)

    def trash_project(self, user_id: str, project_slug: str) -> Path | None:
        """Atomically move a soft-deleted project into the owning user's trash."""

        source = self.workspace_manager.project_path(user_id, project_slug)
        if not source.exists():
            return None
        user_root = self.workspace_manager.user_root(user_id)
        trash_root = self._secure_storage_root(user_root, ".trash")
        destination = (trash_root / f"{project_slug}-{uuid.uuid4()}").resolve()
        if destination.parent != trash_root:
            raise WorkflowValidationError("Project trash path escaped the user workspace.")
        return self.storage.trash(source, destination)
