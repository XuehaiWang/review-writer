"""Compatibility boundary for the seven-stage dashboard and legacy task routes.

New identity, project and provider APIs live in versioned FastAPI routes.  The
current dashboard pages still call unversioned endpoints, so this gateway owns
all translation between those requests and the isolated workflow workspace.
No compatibility-specific orchestration should be added to ``app.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from review_writer_core.dashboard_assets import dashboard_page_paths
from review_writer_core.project_catalog import list_review_projects
from review_writer_core.project_config import save_project_config
from review_writer_core.taxonomy import suggest_taxonomy_profile

from .credentials import ProviderSettingsService
from .dashboard_executor import DashboardRequestExecutor
from .repositories import current_stage_from_states
from .security import Principal
from .services import ProjectService
from .workspaces import HostedWorkspaceManager


COMPATIBILITY_HEADERS = {
    "Deprecation": "true",
    "Link": '</api/v1>; rel="successor-version"',
}

DASHBOARD_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, max-age=0, must-revalidate",
    "Pragma": "no-cache",
}


class WorkflowCompatibilityGateway:
    """Run dashboard routes without leaking compatibility logic into the API app."""

    def __init__(
        self,
        *,
        review_root: Path,
        project_service: ProjectService,
        workspace_manager: HostedWorkspaceManager | None,
        provider_settings_service: ProviderSettingsService | None,
        executor: DashboardRequestExecutor | None = None,
    ) -> None:
        self.base_review_root = Path(review_root).resolve()
        self.project_service = project_service
        self.workspace_manager = workspace_manager
        self.provider_settings_service = provider_settings_service
        self.executor = executor or DashboardRequestExecutor()
        self.view_root = Path(__file__).resolve().parents[1] / "view"
        self.asset_root = (self.view_root / "assets").resolve()
        self.page_paths = dashboard_page_paths(self.view_root)

    @property
    def hosted(self) -> bool:
        return self.workspace_manager is not None

    def review_root(self, principal: Principal) -> Path:
        if self.workspace_manager is None:
            return self.base_review_root
        return self.workspace_manager.user_root(principal.user_id)

    @staticmethod
    def project_id_from_path(raw_path: str) -> str:
        parts = raw_path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "project"]:
            return unquote(parts[2])
        if len(parts) == 3 and parts[:2] in (["api", "discovery"], ["api", "projects"]):
            return unquote(parts[2])
        return ""

    @staticmethod
    def rewrite_project_path(raw_path: str, project_slug: str) -> str:
        parts = raw_path.strip("/").split("/")
        if len(parts) >= 3:
            parts[2] = quote(project_slug, safe="")
        return "/" + "/".join(parts)

    def project_list(self, principal: Principal, root: Path) -> list[dict[str, Any]]:
        filesystem = {
            str(item.get("project_id")): item for item in list_review_projects(root)
        }
        projects: list[dict[str, Any]] = []
        for record in self.refresh_project_states(principal, root=root):
            item = dict(filesystem.get(record.slug) or {})
            if not item:
                item = {
                    "project_id": record.slug,
                    "has_discovery": False,
                    "has_matrix_outline": False,
                    "has_blueprint": False,
                    "has_section_drafting": False,
                    "has_figure_redraw": False,
                    "has_first_draft": False,
                    "has_final_audit": False,
                }
            item.update(
                {
                    "project_id": record.slug,
                    "topic": record.topic,
                    "taxonomy_profile": record.taxonomy_profile,
                    "discovery_status": record.discovery_status,
                    "current_stage": record.current_stage,
                    "completed_stages": list(record.completed_stages),
                }
            )
            projects.append(item)
        return projects

    def sync_project_state(
        self,
        principal: Principal,
        project_id: str,
        project_slug: str,
        root: Path,
    ) -> None:
        if self.workspace_manager is None:
            return
        project_path = self.workspace_manager.project_path(principal.user_id, project_slug)
        if not project_path.is_dir():
            return
        try:
            from view.serve_review_dashboard import (
                reconcile_project_semantic_states,
                workflow_store,
            )

            reconcile_project_semantic_states(root, project_slug)
            snapshot = workflow_store(root).workflow_snapshot(project_slug)
            states = {
                str(item.get("stage_id")): dict(item)
                for item in snapshot.get("stage_state") or []
                if isinstance(item, dict) and item.get("stage_id")
            }
            self.project_service.sync_stage_states(
                principal,
                project_id,
                states,
                current_stage_from_states(states),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workflow state synchronization failed; PostgreSQL was not updated.",
            ) from exc

    def refresh_project_states(
        self,
        principal: Principal,
        *,
        root: Path | None = None,
    ) -> list[Any]:
        """Reconcile workflow artifacts, then return database-owned summaries."""
        records = self.project_service.list_projects(principal)
        if self.workspace_manager is None:
            return records
        workspace_root = root or self.review_root(principal)
        refreshed = False
        for record in records:
            project_path = self.workspace_manager.project_path(principal.user_id, record.slug)
            if not project_path.is_dir():
                continue
            self.sync_project_state(principal, record.project_id, record.slug, workspace_root)
            refreshed = True
        return self.project_service.list_projects(principal) if refreshed else records

    @staticmethod
    def _compatibility_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(headers or {})
        merged.update(COMPATIBILITY_HEADERS)
        return merged

    def page_response(self, raw_path: str) -> FileResponse:
        page = self.page_paths.get(raw_path)
        if page is None or not page.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found.")
        return FileResponse(
            page,
            media_type="text/html",
            headers=DASHBOARD_NO_CACHE_HEADERS,
        )

    def workspace_page_response(self, workspace: str, tab: str) -> FileResponse:
        """Serve one legacy tool through its canonical merged workspace route."""
        normalized = str(tab or "").strip().casefold()
        if workspace == "planning":
            page_route = "/blueprint" if normalized == "blueprint" else "/matrix"
        elif workspace == "images":
            page_route = "/figures" if normalized == "redraw" else "/figure-review"
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
        return self.page_response(page_route)

    @staticmethod
    def legacy_workspace_redirect(request: Request, workspace: str, tab: str) -> RedirectResponse:
        pairs = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != "tab"
        ]
        pairs.append(("tab", tab))
        query = urlencode(pairs, doseq=True)
        return RedirectResponse(
            url=f"/{workspace}?{query}",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )

    def asset_response(self, asset_path: str) -> FileResponse:
        candidate = (self.asset_root / asset_path).resolve()
        if candidate == self.asset_root or self.asset_root not in candidate.parents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")
        if not candidate.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")
        return FileResponse(candidate, headers=DASHBOARD_NO_CACHE_HEADERS)

    async def dispatch(self, request: Request, principal: Principal) -> Response:
        root = self.review_root(principal)
        raw_path = request.url.path
        path = raw_path
        body = await request.body()
        record = None
        discovery_payload: dict[str, Any] | None = None
        raw_project_id = self.project_id_from_path(raw_path)

        if request.method in {"POST", "PUT"} and (
            raw_path == "/api/discovery" or raw_path.startswith("/api/discovery/")
        ):
            try:
                payload = json.loads(body.decode("utf-8"))
                discovery_payload = payload if isinstance(payload, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                discovery_payload = None

        if self.hosted and raw_path.startswith("/api/settings"):
            return JSONResponse(
                status_code=status.HTTP_410_GONE,
                content={"detail": "请在用户门户的个人 API 设置中管理模型密钥。"},
                headers=self._compatibility_headers(),
            )

        if self.hosted and request.method != "GET":
            from view.provider_settings import register_runtime_provider_environment

            runtime_environment = (
                self.provider_settings_service.runtime_environment(principal)
                if self.provider_settings_service is not None
                else {}
            )
            register_runtime_provider_environment(root, runtime_environment, isolated=True)

        if self.hosted and raw_path == "/api/projects" and request.method == "GET":
            return JSONResponse(
                self.project_list(principal, root),
                headers=self._compatibility_headers(),
            )

        if self.hosted and raw_path == "/api/discovery" and request.method == "POST":
            requested_slug = (
                str(discovery_payload.get("project_id") or "")
                if discovery_payload is not None
                else ""
            )
            if requested_slug:
                record = self.project_service.get_project(principal, requested_slug)
                if record is None and discovery_payload is not None:
                    topic = str(discovery_payload.get("topic") or "")
                    record = self.project_service.create_project(
                        principal,
                        slug=requested_slug,
                        topic=topic,
                        taxonomy_profile=suggest_taxonomy_profile(topic),
                    )

        if self.hosted and raw_project_id:
            record = record or self.project_service.get_project(principal, raw_project_id)
            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
            path = self.rewrite_project_path(raw_path, record.slug)

        query = request.url.query
        path_and_query = f"{path}?{query}" if query else path
        dashboard_response = await run_in_threadpool(
            self.executor.dispatch,
            root,
            method=request.method,
            path_and_query=path_and_query,
            headers=dict(request.headers),
            body=body,
        )

        discovery_wrote_state = (
            self.hosted
            and request.method in {"POST", "PUT"}
            and (raw_path == "/api/discovery" or raw_path.startswith("/api/discovery/"))
            and dashboard_response.status_code in {200, 201, 409}
            and record is not None
            and discovery_payload is not None
        )
        if discovery_wrote_state:
            topic = str(
                discovery_payload.get("topic")
                if "topic" in discovery_payload
                else record.topic
            ).strip()
            taxonomy_profile = suggest_taxonomy_profile(topic)
            if topic != record.topic or taxonomy_profile != record.taxonomy_profile:
                record = self.project_service.update_project_topic(
                    principal,
                    record.project_id,
                    topic=topic,
                    taxonomy_profile=taxonomy_profile,
                )
            save_project_config(
                root,
                record.slug,
                topic=record.topic,
                taxonomy_profile=record.taxonomy_profile,
            )

        if (
            self.hosted
            and request.method == "DELETE"
            and raw_path.startswith("/api/projects/")
            and record is not None
            and dashboard_response.status_code in {200, 404}
        ):
            self.project_service.delete_project(principal, record.project_id)
            record = None

        should_sync_workflow = request.method != "GET" or raw_path.endswith(
            ("/workflow-state", "/figures/redraw-all")
        )
        if record is not None and should_sync_workflow:
            self.sync_project_state(principal, record.project_id, record.slug, root)

        response_headers = dashboard_response.headers
        if raw_path.startswith("/api/"):
            response_headers = self._compatibility_headers(response_headers)
        return Response(
            content=dashboard_response.body,
            status_code=dashboard_response.status_code,
            headers=response_headers,
        )

    def register_routes(
        self,
        app: FastAPI,
        principal_dependency: Callable[..., Principal],
    ) -> None:
        """Register only the routes still required by the current dashboard UI."""

        @app.get("/settings", include_in_schema=False)
        def workflow_settings_redirect(
            _principal: Principal = Depends(principal_dependency),
        ) -> RedirectResponse:
            return RedirectResponse(url="/#settings", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

        @app.get("/library", include_in_schema=False)
        @app.get("/discovery", include_in_schema=False)
        @app.get("/sections", include_in_schema=False)
        @app.get("/draft", include_in_schema=False)
        @app.get("/final", include_in_schema=False)
        async def workflow_page(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return self.page_response(request.url.path)

        @app.get("/planning", include_in_schema=False)
        @app.get("/images", include_in_schema=False)
        async def merged_workflow_page(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            workspace = request.url.path.strip("/")
            return self.workspace_page_response(workspace, request.query_params.get("tab", ""))

        @app.get("/matrix", include_in_schema=False)
        async def legacy_matrix_page(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return self.legacy_workspace_redirect(request, "planning", "matrix")

        @app.get("/blueprint", include_in_schema=False)
        async def legacy_blueprint_page(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return self.legacy_workspace_redirect(request, "planning", "blueprint")

        @app.get("/figure-review", include_in_schema=False)
        async def legacy_figure_review_page(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return self.legacy_workspace_redirect(request, "images", "review")

        @app.get("/figures", include_in_schema=False)
        async def legacy_figures_page(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return self.legacy_workspace_redirect(request, "images", "redraw")

        @app.api_route("/assets/{asset_path:path}", methods=["GET"], include_in_schema=False)
        async def workflow_asset(
            asset_path: str,
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return self.asset_response(asset_path)

        @app.api_route("/file", methods=["GET"], include_in_schema=False)
        async def workflow_file(
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return await self.dispatch(request, principal)

        @app.api_route(
            "/api/{workflow_path:path}",
            methods=["GET", "POST", "PUT", "DELETE"],
            include_in_schema=False,
        )
        async def workflow_api(
            workflow_path: str,
            request: Request,
            principal: Principal = Depends(principal_dependency),
        ) -> Response:
            return await self.dispatch(request, principal)
