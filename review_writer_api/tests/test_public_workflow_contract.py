"""Seven public stages, independently versioned checkpoints, no live providers."""
from dataclasses import replace
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_writer_api.repositories import ProjectRecord
from review_writer_api.schemas import ProjectListResponse, ProjectResponse
from review_writer_api.workflow_contracts import (
    USER_STAGES, WORKFLOW_CHECKPOINTS, completed_user_stages, current_checkpoint,
)
from review_writer_api.routers.planning import build_planning_router
from review_writer_api.routers.sections import build_chapter_planning_router
from review_writer_api.routers.images import build_images_router


def test_frontend_stage_order_matches_backend():
    source = (Path(__file__).resolve().parents[2] / "frontend/src/workflow.ts").read_text(encoding="utf-8")
    assert tuple(re.findall(r'\{ id: "([a-z]+)"', source)) == USER_STAGES


def test_completion_requires_every_checkpoint_and_preserves_independence():
    assert completed_user_stages(("blueprint", "figure-review")) == ()
    assert completed_user_stages(("blueprint", "sections")) == ("sections",)
    assert completed_user_stages(("figure-review", "figures")) == ("images",)
    assert completed_user_stages(WORKFLOW_CHECKPOINTS) == USER_STAGES[1:]
    assert current_checkpoint({"discovery": {"status": " complete "}}) == "matrix"
    assert current_checkpoint({"discovery": "approved", "matrix": "stale"}) == "matrix"


def test_new_and_existing_project_response_does_not_mutate_checkpoint_records():
    record = ProjectRecord("p", "project", "owner", "topic", "pending", ())
    assert ProjectResponse.model_validate(record).current_stage == "library"
    existing = replace(record, discovery_status="approved", current_stage="blueprint",
                       completed_stages=("discovery", "matrix", "blueprint"))
    public = ProjectResponse.model_validate(existing)
    assert public.current_stage == "sections"
    assert public.completed_stages == ("discovery", "planning")
    assert existing.completed_stages == ("discovery", "matrix", "blueprint")
    # Nested validation and serialized round trips must not re-aggregate.
    assert ProjectListResponse(items=[public], count=1).items[0] == public
    assert ProjectResponse.model_validate(public.model_dump()) == public


def test_canonical_and_legacy_read_routes_share_services_and_project_context():
    class Planning:
        def get(self, principal, project_id):
            return {"project_id": project_id, "owner": principal, "revision": 7}

        def confirm_blueprint(self, principal, project_id, *, revision, artifact_id):
            return {"project_id": project_id, "owner": principal,
                    "revision": revision, "artifact_id": artifact_id}

    class Images:
        def get_review(self, principal, project_id):
            return {"project_id": project_id, "owner": principal, "revision": 9}

    principal = lambda: "isolated-owner"
    app = FastAPI()
    service = Planning()
    app.include_router(build_planning_router(principal, service, None))
    app.include_router(build_chapter_planning_router(principal, service, None))
    images = build_images_router(principal, Images(), None)
    app.include_router(images, prefix="/api/v1/projects/{project_id}/images")
    app.include_router(images, prefix="/api/v1/projects/{project_id}/figures", include_in_schema=False)
    with TestClient(app) as client:
        for canonical, legacy in (("sections/planning", "planning"), ("images/review", "figures/review")):
            first = client.get(f"/api/v1/projects/isolated-p/{canonical}")
            previous = client.get(f"/api/v1/projects/isolated-p/{legacy}")
            assert first.status_code == previous.status_code == 200
            assert first.json() == previous.json()
            assert first.json()["project_id"] == "isolated-p"
        for prefix in ("sections", "planning"):
            artifact_id = "00000000-0000-0000-0000-000000000007"
            confirmed = client.post(f"/api/v1/projects/isolated-p/{prefix}/blueprint/confirm",
                                    json={"revision": 7, "artifact_id": artifact_id})
            assert confirmed.status_code == 200
            assert confirmed.json() == {"project_id": "isolated-p", "owner": "isolated-owner",
                                        "revision": 7, "artifact_id": artifact_id}
    paths = app.openapi()["paths"]
    assert "/api/v1/projects/{project_id}/sections/blueprint/jobs" in paths
    assert "/api/v1/projects/{project_id}/planning/blueprint/jobs" not in paths
    assert not any("/figures" in path for path in paths)
