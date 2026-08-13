"""Small explicit dependency container for native workflow services."""

from __future__ import annotations

from dataclasses import dataclass

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.workflow_repository import WorkflowRepository


@dataclass(frozen=True)
class ApplicationContainer:
    workflow_repository: WorkflowRepository
    artifact_service: ArtifactService
