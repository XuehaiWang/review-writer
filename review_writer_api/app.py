"""FastAPI application factory for local and hosted deployments."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager, suppress
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    PASSWORD_MIN_LENGTH,
    AuthAttemptThrottle,
    AuthError,
    AuthRateLimited,
    AuthService,
)
from .artifact_service import ArtifactService
from .config import ApiSettings
from .container import ApplicationContainer
from .credentials import CredentialCipher, ProviderSettingsError, ProviderSettingsService
from .database import create_session_factory, utc_now
from .errors import ProjectArchiveFailed, WorkflowError
from .job_service import JobService
from .native_handlers import NativeWorkflowHandlers
from .domain_services.library import LibraryService
from .domain_services.discovery import DiscoveryService
from .domain_services.drafts import DraftsService
from .domain_services.final import FinalService
from .domain_services.figures import FiguresService
from .domain_services.planning import PlanningService
from .domain_services.sections import SectionsService
from .repositories import (
    HostedProjectRepository,
    LocalProjectRepository,
    ProjectOperationError,
    ProjectRepository,
)
from .schemas import (
    BrowserAuthConfigResponse,
    HealthResponse,
    LoginRequest,
    PrincipalResponse,
    ProviderSettingsListResponse,
    ProviderSettingsResponse,
    ProviderSettingsUpdateRequest,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectResponse,
    RegisterRequest,
)
from .security import AuthorizationError, Principal, local_owner_principal
from .services import ProjectService
from .routers.files import build_file_router
from .routers.jobs import build_job_router
from .routers.library import build_library_router
from .routers.discovery import build_discovery_router
from .routers.drafts import build_drafts_router
from .routers.final import build_final_router
from .routers.planning import build_planning_router
from .routers.sections import build_sections_router
from .routers.figures import build_figures_router
from .scientific_runner import ScientificRunner
from .workflow_repository import WorkflowRepository
from .workspaces import HostedWorkspaceManager
from review_writer_core.dashboard_assets import dashboard_page_paths


API_VERSION = "v1"


def create_app(
    settings: ApiSettings | None = None,
    *,
    principal_provider: Callable[[], Principal] | None = None,
    session_factory_override: Any | None = None,
    project_repository: ProjectRepository | None = None,
    workflow_repository_override: WorkflowRepository | None = None,
    job_service_override: JobService | None = None,
    native_workflow_overrides: Mapping[str, Callable] | None = None,
) -> FastAPI:
    resolved = settings or ApiSettings.from_env()
    engine = None
    session_factory = session_factory_override
    if resolved.deployment_mode == "hosted":
        if session_factory is None:
            session_factory, engine = create_session_factory(resolved.database_url)
        repository = project_repository or HostedProjectRepository(session_factory)
    else:
        repository = project_repository or LocalProjectRepository(
            resolved.review_root,
            user_id=local_owner_principal().user_id,
        )
    project_service = ProjectService(repository)
    provider_settings_service = (
        ProviderSettingsService(
            session_factory,
            CredentialCipher(resolved.credential_encryption_key),
            allow_private_urls=resolved.allow_private_provider_urls,
            allowed_hosts=resolved.allowed_provider_hosts,
            trusted_proxy_networks=resolved.trusted_proxy_networks,
        )
        if resolved.deployment_mode == "hosted"
        else None
    )
    auth_service = (
        AuthService(session_factory, session_days=resolved.session_days)
        if resolved.deployment_mode == "hosted"
        else None
    )
    auth_throttle = AuthAttemptThrottle(
        max_attempts=resolved.auth_rate_limit_attempts,
        window_seconds=resolved.auth_rate_limit_window_seconds,
    )
    auth_ip_throttle = AuthAttemptThrottle(
        max_attempts=resolved.auth_rate_limit_attempts * 10,
        window_seconds=resolved.auth_rate_limit_window_seconds,
    )
    workflow_repository = (
        workflow_repository_override or WorkflowRepository(session_factory)
        if resolved.deployment_mode == "hosted"
        else None
    )
    hosted_workspace_manager = (
        HostedWorkspaceManager(
            resolved.hosted_workspace_root
            or (resolved.review_root / ".review-writer" / "hosted-workspaces")
        )
        if resolved.deployment_mode == "hosted"
        else None
    )
    artifact_service = (
        ArtifactService(workflow_repository, hosted_workspace_manager)
        if workflow_repository is not None and hosted_workspace_manager is not None
        else None
    )
    job_service = (
        job_service_override
        or JobService(workflow_repository, max_workers=resolved.job_worker_count)
        if workflow_repository is not None
        else None
    )
    scientific_runner = (
        ScientificRunner(
            allow_private_networks=resolved.allow_private_provider_urls,
            trusted_proxy_networks=resolved.trusted_proxy_networks,
        )
        if workflow_repository is not None
        else None
    )
    native_overrides = dict(native_workflow_overrides or {})
    library_service = (
        LibraryService(
            session_factory,
            hosted_workspace_manager,
            precise_ingest=native_overrides.get("library.precise_ingest"),
            runtime_environment=(
                provider_settings_service.mineru_environment
                if provider_settings_service is not None
                else None
            ),
            scientific_runner=scientific_runner,
        )
        if session_factory is not None and hosted_workspace_manager is not None
        else None
    )
    discovery_service = (
        DiscoveryService(workflow_repository, artifact_service)
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    planning_service = (
        PlanningService(workflow_repository, artifact_service)
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    sections_service = (
        SectionsService(workflow_repository, artifact_service)
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    figures_service = (
        FiguresService(workflow_repository, artifact_service)
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    drafts_service = (
        DraftsService(workflow_repository, artifact_service)
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    final_service = (
        FinalService(workflow_repository, artifact_service, drafts_service)
        if workflow_repository is not None
        and artifact_service is not None
        and drafts_service is not None
        else None
    )
    native_handlers = (
        NativeWorkflowHandlers(
            scientific_runner,
            hosted_workspace_manager,
            provider_settings_service,
        ).mapping()
        if scientific_runner is not None and hosted_workspace_manager is not None
        else {}
    )
    native_handlers.update(native_overrides)
    container = (
        ApplicationContainer(
            workflow_repository=workflow_repository,
            artifact_service=artifact_service,
            job_service=job_service,
            scientific_runner=scientific_runner,
            library_service=library_service,
            discovery_service=discovery_service,
            planning_service=planning_service,
            sections_service=sections_service,
            figures_service=figures_service,
            drafts_service=drafts_service,
            final_service=final_service,
        )
        if (
            workflow_repository is not None
            and artifact_service is not None
            and job_service is not None
            and scientific_runner is not None
        )
        else None
    )
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        heartbeat_task = None

        async def heartbeat_loop() -> None:
            while True:
                workflow_repository.set_system_state(
                    "application_heartbeat",
                    {"status": "running", "observed_at": utc_now().isoformat()},
                )
                await asyncio.sleep(10)

        try:
            if workflow_repository is not None:
                job_service.start()
                workflow_repository.set_system_state(
                    "application_heartbeat",
                    {"status": "running", "observed_at": utc_now().isoformat()},
                )
                heartbeat_task = asyncio.create_task(heartbeat_loop())
            yield
        finally:
            if job_service is not None:
                job_service.shutdown(wait=True)
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
                workflow_repository.set_system_state(
                    "application_heartbeat",
                    {"status": "stopped", "observed_at": utc_now().isoformat()},
                )
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="Review Writer API",
        version="0.1.0",
        description="Review Writer application API.",
        docs_url="/api/docs" if resolved.expose_api_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if resolved.expose_api_docs else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.project_service = project_service
    app.state.provider_settings_service = provider_settings_service
    app.state.auth_service = auth_service
    app.state.auth_throttle = auth_throttle
    app.state.auth_ip_throttle = auth_ip_throttle
    app.state.session_factory = session_factory
    app.state.hosted_workspace_manager = hosted_workspace_manager
    app.state.workflow_repository = workflow_repository
    app.state.artifact_service = artifact_service
    app.state.job_service = job_service
    app.state.scientific_runner = scientific_runner
    app.state.library_service = library_service
    app.state.discovery_service = discovery_service
    app.state.planning_service = planning_service
    app.state.sections_service = sections_service
    app.state.figures_service = figures_service
    app.state.drafts_service = drafts_service
    app.state.final_service = final_service
    app.state.container = container
    web_root = Path(__file__).resolve().parent / "web"
    view_root = Path(__file__).resolve().parents[1] / "view"
    dashboard_pages = dashboard_page_paths(view_root)

    def portal_csp() -> str:
        return "; ".join(
            (
                "default-src 'self'",
                "base-uri 'none'",
                "connect-src 'self'",
                "font-src 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
                "img-src 'self' data:",
                "object-src 'none'",
                "script-src 'self'",
                "style-src 'self'",
            )
        )

    @app.middleware("http")
    async def production_headers(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        if (
            resolved.deployment_mode == "hosted"
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path.startswith("/api/")
        ):
            origin = str(request.headers.get("Origin") or "").rstrip("/")
            if origin and origin != resolved.public_origin:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Request origin is not allowed."},
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Library previews and the bundled Ketcher editor are intentionally
        # rendered by the authenticated dashboard in same-origin iframes.
        # Keep every other route non-embeddable and never allow cross-origin
        # framing of these two narrowly scoped surfaces.
        same_origin_frame = (
            request.url.path.startswith("/assets/ketcher/")
            or (
                request.url.path.startswith("/api/v1/library/papers/")
                and request.url.path.endswith("/pdf")
            )
        )
        if same_origin_frame:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        else:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = portal_csp()
            response.headers["Cache-Control"] = "no-store"
        return response

    def workflow_error_response(exc: WorkflowError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    def is_workflow_surface(path: str) -> bool:
        if path in {
            "/library",
            "/discovery",
            "/planning",
            "/matrix",
            "/blueprint",
            "/sections",
            "/images",
            "/figure-review",
            "/figures",
            "/draft",
            "/final",
        }:
            return True
        if not path.startswith("/api/"):
            return False
        allowed_exact = {"/api/v1/health", "/api/v1/me", "/api/openapi.json"}
        allowed_prefixes = ("/api/v1/auth", "/api/v1/provider-settings", "/api/docs")
        return path not in allowed_exact and not any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in allowed_prefixes
        )

    @app.middleware("http")
    async def workflow_readiness(request: Request, call_next):
        if workflow_repository is not None and is_workflow_surface(request.url.path):
            try:
                workflow_repository.require_workflow_ready()
            except WorkflowError as exc:
                return workflow_error_response(exc)
        return await call_next(request)

    if principal_provider is not None:
        def current_principal() -> Principal:
            return principal_provider()
    elif resolved.deployment_mode == "local":
        def current_principal() -> Principal:
            return local_owner_principal()
    else:
        def current_principal(request: Request) -> Principal:
            raw_token = request.cookies.get(resolved.session_cookie_name, "")
            principal = auth_service.resolve(raw_token) if raw_token else None
            if principal is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="请先登录。",
                )
            return principal

    @app.exception_handler(AuthorizationError)
    async def authorization_error(_request: Request, exc: AuthorizationError):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(exc)},
        )

    @app.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, exc: WorkflowError):
        return workflow_error_response(exc)

    @app.exception_handler(ProjectOperationError)
    async def project_operation_error(_request: Request, exc: ProjectOperationError):
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(ProviderSettingsError)
    async def provider_settings_error(_request: Request, exc: ProviderSettingsError):
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            api_version=API_VERSION,
            deployment_mode=resolved.deployment_mode,
        )

    @app.get(
        "/api/v1/auth/config",
        response_model=BrowserAuthConfigResponse,
        tags=["identity"],
    )
    def browser_auth_config() -> BrowserAuthConfigResponse:
        hosted = resolved.deployment_mode == "hosted"
        return BrowserAuthConfigResponse(
            enabled=hosted,
            registration_enabled=hosted,
            password_min_length=PASSWORD_MIN_LENGTH,
        )

    def principal_response(principal: Principal) -> PrincipalResponse:
        return PrincipalResponse(
            user_id=principal.user_id,
            email=principal.email,
            display_name=principal.display_name,
            roles=sorted(role.value for role in principal.roles),
            permissions=sorted(permission.value for permission in principal.permissions),
        )

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=resolved.session_cookie_name,
            value=token,
            max_age=resolved.session_days * 24 * 60 * 60,
            path="/",
            secure=resolved.session_cookie_secure,
            httponly=True,
            samesite="lax",
        )

    if auth_service is not None:

        @app.post(
            "/api/v1/auth/register",
            response_model=PrincipalResponse,
            status_code=status.HTTP_201_CREATED,
            tags=["identity"],
        )
        def register(
            payload: RegisterRequest, request: Request, response: Response
        ) -> PrincipalResponse:
            throttle_key = f"register:{request.client.host if request.client else 'unknown'}"
            try:
                auth_throttle.consume(throttle_key)
                authenticated = auth_service.register(
                    email=payload.email,
                    password=payload.password,
                    display_name=payload.display_name,
                )
            except AuthRateLimited as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=str(exc),
                    headers={"Retry-After": str(resolved.auth_rate_limit_window_seconds)},
                ) from exc
            except AuthError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            set_session_cookie(response, authenticated.token)
            return principal_response(authenticated.principal)

        @app.post(
            "/api/v1/auth/login",
            response_model=PrincipalResponse,
            tags=["identity"],
        )
        def login(
            payload: LoginRequest, request: Request, response: Response
        ) -> PrincipalResponse:
            client_host = request.client.host if request.client else "unknown"
            identity_hash = hashlib.sha256(
                str(payload.email or "").strip().casefold().encode("utf-8")
            ).hexdigest()
            throttle_key = f"login:{client_host}:{identity_hash}"
            ip_throttle_key = f"login-ip:{client_host}"
            try:
                auth_ip_throttle.consume(ip_throttle_key)
                auth_throttle.consume(throttle_key)
                authenticated = auth_service.login(email=payload.email, password=payload.password)
            except AuthRateLimited as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=str(exc),
                    headers={"Retry-After": str(resolved.auth_rate_limit_window_seconds)},
                ) from exc
            except AuthError as exc:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
            auth_throttle.clear(throttle_key)
            set_session_cookie(response, authenticated.token)
            return principal_response(authenticated.principal)

        @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["identity"])
        def logout(
            request: Request,
            response: Response,
            _principal: Principal = Depends(current_principal),
        ) -> None:
            raw_token = request.cookies.get(resolved.session_cookie_name, "")
            if raw_token:
                auth_service.logout(raw_token)
            response.delete_cookie(
                key=resolved.session_cookie_name,
                path="/",
                secure=resolved.session_cookie_secure,
                httponly=True,
                samesite="lax",
            )

    @app.get("/api/v1/me", response_model=PrincipalResponse, tags=["identity"])
    def me(principal: Principal = Depends(current_principal)) -> PrincipalResponse:
        return principal_response(principal)

    @app.get("/api/v1/projects", response_model=ProjectListResponse, tags=["projects"])
    def projects(principal: Principal = Depends(current_principal)) -> ProjectListResponse:
        items = project_service.list_projects(principal)
        return ProjectListResponse(items=items, count=len(items))

    @app.post(
        "/api/v1/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["projects"],
    )
    def create_project(
        payload: ProjectCreateRequest,
        principal: Principal = Depends(current_principal),
    ) -> ProjectResponse:
        record = project_service.create_project(
            principal,
            slug=payload.slug,
            topic=payload.topic,
            taxonomy_profile=payload.taxonomy_profile,
        )
        return ProjectResponse.model_validate(record)

    @app.get("/api/v1/projects/{project_id}", response_model=ProjectResponse, tags=["projects"])
    def project(project_id: str, principal: Principal = Depends(current_principal)) -> ProjectResponse:
        record = project_service.get_project(principal, project_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return ProjectResponse.model_validate(record)

    @app.delete(
        "/api/v1/projects/{project_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["projects"],
    )
    def delete_project(
        project_id: str,
        principal: Principal = Depends(current_principal),
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

    if provider_settings_service is not None:

        @app.get(
            "/api/v1/provider-settings",
            response_model=ProviderSettingsListResponse,
            tags=["provider-settings"],
        )
        def provider_settings(
            principal: Principal = Depends(current_principal),
        ) -> ProviderSettingsListResponse:
            records = provider_settings_service.list_settings(principal)
            return ProviderSettingsListResponse(
                items=[ProviderSettingsResponse.model_validate(record, from_attributes=True) for record in records]
            )

        @app.put(
            "/api/v1/provider-settings/{provider_kind}",
            response_model=ProviderSettingsResponse,
            tags=["provider-settings"],
        )
        def save_provider_settings(
            provider_kind: str,
            payload: ProviderSettingsUpdateRequest,
            principal: Principal = Depends(current_principal),
        ) -> ProviderSettingsResponse:
            record = provider_settings_service.save_settings(
                principal,
                provider_kind,
                base_url=payload.base_url,
                model_name=payload.model_name,
                wire_api=payload.wire_api,
                api_key=payload.api_key,
                enabled=payload.enabled,
            )
            return ProviderSettingsResponse.model_validate(record, from_attributes=True)

        @app.delete(
            "/api/v1/provider-settings/{provider_kind}",
            status_code=status.HTTP_204_NO_CONTENT,
            tags=["provider-settings"],
        )
        def delete_provider_settings(
            provider_kind: str,
            principal: Principal = Depends(current_principal),
        ) -> None:
            provider_settings_service.delete_settings(principal, provider_kind)

    @app.get("/", include_in_schema=False)
    def hosted_portal() -> FileResponse:
        return FileResponse(web_root / "index.html", media_type="text/html")

    app.mount("/assets/app", StaticFiles(directory=web_root), name="hosted-portal-assets")
    app.mount(
        "/assets",
        StaticFiles(directory=view_root / "assets"),
        name="workflow-assets",
    )

    def dashboard_response(route: str) -> FileResponse:
        return FileResponse(
            dashboard_pages[route],
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
        )

    def require_native_workflow() -> None:
        if workflow_repository is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    @app.get("/settings", include_in_schema=False)
    def workflow_settings_redirect(
        _principal: Principal = Depends(current_principal),
    ) -> RedirectResponse:
        return RedirectResponse(url="/#settings", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/library", include_in_schema=False)
    @app.get("/discovery", include_in_schema=False)
    @app.get("/sections", include_in_schema=False)
    @app.get("/draft", include_in_schema=False)
    @app.get("/final", include_in_schema=False)
    def workflow_page(
        request: Request,
        _principal: Principal = Depends(current_principal),
        _workflow: None = Depends(require_native_workflow),
    ) -> FileResponse:
        return dashboard_response(request.url.path)

    @app.get("/planning", include_in_schema=False)
    def planning_page(
        request: Request,
        _principal: Principal = Depends(current_principal),
        _workflow: None = Depends(require_native_workflow),
    ) -> FileResponse:
        route = "/blueprint" if request.query_params.get("tab") == "blueprint" else "/matrix"
        return dashboard_response(route)

    @app.get("/images", include_in_schema=False)
    def image_page(
        request: Request,
        _principal: Principal = Depends(current_principal),
        _workflow: None = Depends(require_native_workflow),
    ) -> FileResponse:
        route = "/figures" if request.query_params.get("tab") == "redraw" else "/figure-review"
        return dashboard_response(route)

    @app.get("/matrix", include_in_schema=False)
    def matrix_redirect(
        request: Request, _workflow: None = Depends(require_native_workflow)
    ) -> RedirectResponse:
        query = str(request.url.query or "")
        suffix = f"&{query}" if query else ""
        return RedirectResponse(f"/planning?tab=matrix{suffix}", status_code=307)

    @app.get("/blueprint", include_in_schema=False)
    def blueprint_redirect(
        request: Request, _workflow: None = Depends(require_native_workflow)
    ) -> RedirectResponse:
        query = str(request.url.query or "")
        suffix = f"&{query}" if query else ""
        return RedirectResponse(f"/planning?tab=blueprint{suffix}", status_code=307)

    @app.get("/figure-review", include_in_schema=False)
    def figure_review_redirect(
        request: Request, _workflow: None = Depends(require_native_workflow)
    ) -> RedirectResponse:
        query = str(request.url.query or "")
        suffix = f"&{query}" if query else ""
        return RedirectResponse(f"/images?tab=review{suffix}", status_code=307)

    @app.get("/figures", include_in_schema=False)
    def figures_redirect(
        request: Request, _workflow: None = Depends(require_native_workflow)
    ) -> RedirectResponse:
        query = str(request.url.query or "")
        suffix = f"&{query}" if query else ""
        return RedirectResponse(f"/images?tab=redraw{suffix}", status_code=307)

    if artifact_service is not None:
        app.include_router(build_file_router(current_principal, artifact_service))
    if job_service is not None:
        app.include_router(build_job_router(current_principal, job_service))
    if library_service is not None and job_service is not None:
        app.include_router(
            build_library_router(
                current_principal,
                library_service,
                job_service,
                native_handlers,
            )
        )
    if discovery_service is not None and job_service is not None:
        app.include_router(
            build_discovery_router(
                current_principal,
                discovery_service,
                job_service,
                native_handlers,
            )
        )
    if planning_service is not None:
        app.include_router(build_planning_router(current_principal, planning_service))
    if sections_service is not None and job_service is not None:
        app.include_router(
            build_sections_router(
                current_principal,
                sections_service,
                job_service,
                native_handlers,
            )
        )
    if figures_service is not None and job_service is not None:
        app.include_router(
            build_figures_router(
                current_principal,
                figures_service,
                job_service,
                native_handlers,
            )
        )
    if drafts_service is not None and job_service is not None:
        app.include_router(
            build_drafts_router(
                current_principal,
                drafts_service,
                job_service,
                native_handlers,
            )
        )
    if final_service is not None and job_service is not None:
        app.include_router(
            build_final_router(
                current_principal,
                final_service,
                job_service,
                native_handlers,
            )
        )
    return app
