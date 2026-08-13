"""Small explicit dependency container for native workflow services."""

from __future__ import annotations

from dataclasses import dataclass

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.domain_services.discovery import DiscoveryService
from review_writer_api.domain_services.drafts import DraftsService
from review_writer_api.domain_services.figures import FiguresService
from review_writer_api.domain_services.final import FinalService
from review_writer_api.domain_services.library import LibraryService
from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.job_service import JobService
from review_writer_api.scientific_runner import ScientificRunner
from review_writer_api.workflow_repository import WorkflowRepository


@dataclass(frozen=True)
class ApplicationContainer:
    workflow_repository: WorkflowRepository
    artifact_service: ArtifactService
    job_service: JobService
    scientific_runner: ScientificRunner
    library_service: LibraryService
    discovery_service: DiscoveryService
    planning_service: PlanningService
    sections_service: SectionsService
    figures_service: FiguresService
    drafts_service: DraftsService
    final_service: FinalService
