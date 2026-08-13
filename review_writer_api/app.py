"""FastAPI application factory for local and hosted deployments."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import PASSWORD_MIN_LENGTH, AuthError, AuthService
from .config import ApiSettings
from .credentials import CredentialCipher, ProviderSettingsError, ProviderSettingsService
from .database import create_session_factory, utc_now
from .errors import WorkflowError
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
from .workflow_compat import WorkflowCompatibilityGateway
from .workflow_repository import WorkflowRepository
from .workspaces import HostedWorkspaceManager


API_VERSION = "v1"


def create_app(
    settings: ApiSettings | None = None,
    *,
    principal_provider: Callable[[], Principal] | None = None,
    session_factory_override: Any | None = None,
    project_repository: ProjectRepository | None = None,
    workflow_repository_override: WorkflowRepository | None = None,
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
        )
        if resolved.deployment_mode == "hosted"
        else None
    )
    auth_service = (
        AuthService(session_factory, session_days=resolved.session_days)
        if resolved.deployment_mode == "hosted"
        else None
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
    workflow_gateway = WorkflowCompatibilityGateway(
        review_root=resolved.review_root,
        project_service=project_service,
        workspace_manager=hosted_workspace_manager,
        provider_settings_service=provider_settings_service,
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
                workflow_repository.set_system_state(
                    "application_heartbeat",
                    {"status": "running", "observed_at": utc_now().isoformat()},
                )
                heartbeat_task = asyncio.create_task(heartbeat_loop())
            yield
        finally:
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
    app.state.session_factory = session_factory
    app.state.hosted_workspace_manager = hosted_workspace_manager
    app.state.workflow_compatibility_gateway = workflow_gateway
    app.state.workflow_repository = workflow_repository
    web_root = Path(__file__).resolve().parent / "web"

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
        same_origin_frame = request.url.path == "/file" or request.url.path.startswith(
            "/assets/ketcher/"
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
            "/file",
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
        def register(payload: RegisterRequest, response: Response) -> PrincipalResponse:
            try:
                authenticated = auth_service.register(
                    email=payload.email,
                    password=payload.password,
                    display_name=payload.display_name,
                )
            except AuthError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            set_session_cookie(response, authenticated.token)
            return principal_response(authenticated.principal)

        @app.post(
            "/api/v1/auth/login",
            response_model=PrincipalResponse,
            tags=["identity"],
        )
        def login(payload: LoginRequest, response: Response) -> PrincipalResponse:
            try:
                authenticated = auth_service.login(email=payload.email, password=payload.password)
            except AuthError as exc:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
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
        items = workflow_gateway.refresh_project_states(principal)
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
        if hosted_workspace_manager is not None:
            root = workflow_gateway.review_root(principal)
            workflow_gateway.sync_project_state(
                principal,
                record.project_id,
                record.slug,
                root,
            )
            record = project_service.get_project(principal, project_id)
            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return ProjectResponse.model_validate(record)

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

    workflow_gateway.register_routes(app, current_principal)

    return app
