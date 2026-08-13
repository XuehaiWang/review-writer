"""Immutable artifact publication and user-owned file resolution."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from review_writer_api.errors import (
    ArtifactFileMissing,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_api.workspaces import HostedWorkspaceManager


@dataclass(frozen=True)
class ResolvedArtifact:
    artifact: ArtifactRecord
    path: Path


class ArtifactService:
    def __init__(
        self,
        repository: WorkflowRepository,
        workspace_manager: HostedWorkspaceManager,
    ):
        self.repository = repository
        self.workspace_manager = workspace_manager

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
        directory = (project_root / ".staging" / safe_run_id).resolve()
        expected_parent = (project_root / ".staging").resolve()
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

        artifact_id = str(uuid.uuid4())
        project_root = self.workspace_manager.project_path(user_id, project.slug)
        destination = (
            project_root
            / ".artifacts"
            / Path(*logical.parts)
            / artifact_id
            / source.name
        ).resolve()
        artifacts_root = (project_root / ".artifacts").resolve()
        try:
            destination.relative_to(artifacts_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Artifact destination escaped the project workspace."
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=False)
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise WorkflowValidationError(
                "Staging and artifact directories must use the same filesystem."
            )
        digest = self._sha256(source)
        stat = source.stat()
        source.replace(destination)
        relative_path = destination.relative_to(project_root).as_posix()
        return self.repository.publish_artifact(
            user_id=user_id,
            project_id=project_id,
            artifact_id=artifact_id,
            logical_name=logical.as_posix(),
            artifact_type=str(artifact_type or destination.suffix.lstrip(".") or "file"),
            relative_path=relative_path,
            content_sha256=digest,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            producer_stage=producer_stage,
            producer_run_id=run_id,
            metadata=metadata,
        )

    def resolve_owned_artifact(self, user_id: str, artifact_id: str) -> ResolvedArtifact:
        owned = self.repository.get_artifact(user_id, artifact_id)
        if owned is None:
            raise WorkflowNotFound("Artifact not found.")
        relative = self._safe_relative(
            owned.artifact.relative_path, label="Stored artifact path"
        )
        project_root = self.workspace_manager.project_path(user_id, owned.project_slug)
        path = (project_root / Path(*relative.parts)).resolve()
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
        trash_root = (user_root / ".trash").resolve()
        trash_root.mkdir(parents=True, exist_ok=True)
        if source.stat().st_dev != trash_root.stat().st_dev:
            raise WorkflowValidationError(
                "Project workspace and trash must use the same filesystem."
            )
        destination = (trash_root / f"{project_slug}-{uuid.uuid4()}").resolve()
        if destination.parent != trash_root:
            raise WorkflowValidationError("Project trash path escaped the user workspace.")
        source.replace(destination)
        return destination
