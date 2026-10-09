"""Stage 00 (project workspace) routes: project lifecycle and taxonomy catalog.

Moved out of ``app.py`` unchanged so every workflow stage owns one router
module and the application factory only assembles them.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from review_writer_api.errors import ProjectArchiveFailed
from review_writer_api.security import Principal
from review_writer_core.taxonomy import NEW_PROJECT_TAXONOMY_PROFILE, taxonomy_profile_catalog
from review_writer_api.schemas import (
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectModelTierUpdateRequest,
    ProjectResponse,
    ProjectTaxonomyProfileUpdateRequest,
    ProjectTaxonomyProfileUpdateResponse,
    TaxonomyProfileCatalogResponse,
)


def build_projects_router(
    principal_dependency: Callable[..., Principal],
    project_service,
    artifact_service=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["projects"])

    @router.get("/projects", response_model=ProjectListResponse)
    def projects(principal: Principal = Depends(principal_dependency)) -> ProjectListResponse:
        items = project_service.list_projects(principal)
        return ProjectListResponse(items=items, count=len(items))

    @router.get("/taxonomy-profiles", response_model=TaxonomyProfileCatalogResponse)
    def taxonomy_profiles(
        _principal: Principal = Depends(principal_dependency),
    ) -> TaxonomyProfileCatalogResponse:
        return TaxonomyProfileCatalogResponse(
            items=taxonomy_profile_catalog(),
            default_profile=NEW_PROJECT_TAXONOMY_PROFILE,
        )

    @router.post(
        "/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_project(
        payload: ProjectCreateRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> ProjectResponse:
        record = project_service.create_project(
            principal,
            slug=payload.slug,
            topic=payload.topic,
            taxonomy_profile=payload.taxonomy_profile,
            model_tier=payload.model_tier,
        )
        return ProjectResponse.model_validate(record)

    @router.patch("/projects/{project_id}/model-tier", response_model=ProjectResponse)
    def update_project_model_tier(
        project_id: str,
        payload: ProjectModelTierUpdateRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> ProjectResponse:
        record = project_service.update_project_model_tier(
            principal, project_id, model_tier=payload.model_tier
        )
        return ProjectResponse.model_validate(record)

    @router.patch(
        "/projects/{project_id}/taxonomy-profile",
        response_model=ProjectTaxonomyProfileUpdateResponse,
    )
    def update_project_taxonomy_profile(
        project_id: str,
        payload: ProjectTaxonomyProfileUpdateRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> ProjectTaxonomyProfileUpdateResponse:
        result = project_service.update_project_taxonomy_profile(
            principal,
            project_id,
            taxonomy_profile=payload.taxonomy_profile,
            confirm_downstream_invalidation=payload.confirm_downstream_invalidation,
        )
        return ProjectTaxonomyProfileUpdateResponse(
            project=ProjectResponse.model_validate(result.project),
            changed=result.changed,
            matrix_entered=result.matrix_entered,
            downstream_stale=result.downstream_stale,
        )

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    def project(project_id: str, principal: Principal = Depends(principal_dependency)) -> ProjectResponse:
        record = project_service.get_project(principal, project_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return ProjectResponse.model_validate(record)

    @router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_project(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> None:
        record = project_service.get_project(principal, project_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        if not project_service.delete_project(principal, record.project_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        if artifact_service is not None:
            try:
                artifact_service.trash_project(principal.user_id, record.slug)
            except Exception as exc:
                restored = project_service.restore_project(principal, record.project_id)
                raise ProjectArchiveFailed(
                    "The project files could not be archived; database deletion was rolled back.",
                    details={"database_restored": restored},
                ) from exc

    return router
