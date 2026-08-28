"""FastAPI application factory for local and hosted deployments."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    PASSWORD_MIN_LENGTH,
    AuthAttemptThrottle,
    AuthError,
    AuthRateLimited,
    AuthService,
    PasswordResetCredential,
)
from .artifact_service import ArtifactService
from .billing import BillingService
from .config import ApiSettings
from .container import ApplicationContainer
from .credentials import ProviderSettingsError
from .database import create_session_factory, utc_now
from .errors import ProjectArchiveFailed, WorkflowError
from .job_service import JobService
from .gateway_client import test_provider_through_gateway
from .model_catalog import DEFAULT_MODEL_TIER, MODEL_TIERS
from .model_gateway import ModelGatewayError, ModelGatewayService
from .native_handlers import NativeWorkflowHandlers
from .domain_services.library import LibraryService
from .domain_services.library_index import LibraryIndexService
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
    AdminProviderAuditListResponse,
    AdminProviderAuditResponse,
    AdminProviderSettingsUpdateRequest,
    AdminProviderTestResponse,
    AdminCreditAdjustmentRequest,
    AdminUsageSummaryResponse,
    AdminUserListResponse,
    AdminUserResponse,
    AdminUserUpdateRequest,
    BalanceResponse,
    BrowserAuthConfigResponse,
    AuthMessageResponse,
    HealthResponse,
    LoginRequest,
    PasswordResetCompleteRequest,
    PasswordResetRequest,
    ModelCatalogResponse,
    ModelGatewayRequest,
    ModelGatewayResponse,
    EmbeddingGatewayRequest,
    EmbeddingGatewayResponse,
    ImageGatewayRequest,
    ImageGatewayResponse,
    ModelTierResponse,
    PrincipalResponse,
    ProviderSettingsListResponse,
    ProviderSettingsResponse,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectModelTierUpdateRequest,
    ProjectResponse,
    ProjectTaxonomyProfileUpdateRequest,
    ProjectTaxonomyProfileUpdateResponse,
    TaxonomyProfileCatalogResponse,
    RegisterRequest,
    CreditTransactionListResponse,
    CreditTransactionResponse,
    UsageSummaryResponse,
    UsageTimelineResponse,
)
from review_writer_core.taxonomy import (
    DEFAULT_TAXONOMY_PROFILE,
    taxonomy_profile_catalog,
)
from .server_providers import ServerProviderSettingsService
from .password_reset_mailer import SmtpPasswordResetMailer
from .security import AuthorizationError, Permission, Principal, local_owner_principal
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
API_VERSION = "v1"
LOGGER = logging.getLogger(__name__)


def create_app(
    settings: ApiSettings | None = None,
    *,
    principal_provider: Callable[[], Principal] | None = None,
    session_factory_override: Any | None = None,
    project_repository: ProjectRepository | None = None,
    workflow_repository_override: WorkflowRepository | None = None,
    job_service_override: JobService | None = None,
    model_gateway_override: Any | None = None,
    native_workflow_overrides: Mapping[str, Callable] | None = None,
    password_reset_mailer_override: Any | None = None,
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
        ServerProviderSettingsService(resolved, session_factory)
        if resolved.deployment_mode == "hosted"
        else None
    )
    auth_service = (
        AuthService(
            session_factory,
            session_days=resolved.session_days,
            password_reset_minutes=resolved.password_reset_minutes,
            admin_emails=resolved.admin_emails,
        )
        if resolved.deployment_mode == "hosted"
        else None
    )
    password_reset_mailer = password_reset_mailer_override
    if (
        password_reset_mailer is None
        and resolved.smtp_host
        and resolved.smtp_from_email
    ):
        password_reset_mailer = SmtpPasswordResetMailer(
            host=resolved.smtp_host,
            port=resolved.smtp_port,
            username=resolved.smtp_username,
            password=resolved.smtp_password,
            from_email=resolved.smtp_from_email,
            security=resolved.smtp_security,
        )
    billing_service = (
        BillingService(session_factory)
        if resolved.deployment_mode == "hosted" and session_factory is not None
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
        or JobService(
            workflow_repository,
            max_workers=resolved.job_worker_count,
            execution_enabled=resolved.job_execution_enabled,
        )
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
            mineru_price_usd_per_page=resolved.mineru_price_usd_per_page,
            mineru_max_concurrency=resolved.mineru_max_concurrency,
            billing_service=billing_service,
        )
        if session_factory is not None and hosted_workspace_manager is not None
        else None
    )
    discovery_service = (
        DiscoveryService(workflow_repository, artifact_service)
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    library_index_service = (
        LibraryIndexService(
            session_factory,
            hosted_workspace_manager,
            enabled=resolved.document_retrieval_enabled,
            vector_enabled=resolved.vector_retrieval_enabled,
            tuning=resolved.retrieval_tuning,
        )
        if session_factory is not None and hosted_workspace_manager is not None
        else None
    )
    model_gateway = model_gateway_override or (
        ModelGatewayService(
            session_factory,
            resolved,
            provider_settings=provider_settings_service,
            billing_service=billing_service,
        )
        if session_factory is not None and resolved.deployment_mode == "hosted"
        else None
    )
    if library_index_service is not None and hasattr(
        model_gateway, "embedding_profile"
    ):
        library_index_service.embedding_profile_provider = model_gateway
    if library_index_service is not None and hasattr(
        model_gateway, "embed_for_active_job"
    ):
        library_index_service.embedding_gateway = model_gateway
    if discovery_service is not None:
        discovery_service.library_index = library_index_service
    planning_service = (
        PlanningService(
            workflow_repository,
            artifact_service,
            scientific_runner=scientific_runner,
            provider_settings=provider_settings_service,
            model_gateway=model_gateway,
            library_index=library_index_service,
        )
        if workflow_repository is not None and artifact_service is not None
        else None
    )
    sections_service = (
        SectionsService(workflow_repository, artifact_service, library_index_service)
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
            model_gateway,
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
            library_index_service=library_index_service,
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
            if isinstance(model_gateway, ModelGatewayService):
                await model_gateway.close()
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
    app.state.model_gateway = model_gateway
    app.state.auth_service = auth_service
    app.state.billing_service = billing_service
    app.state.auth_throttle = auth_throttle
    app.state.auth_ip_throttle = auth_ip_throttle
    app.state.session_factory = session_factory
    app.state.hosted_workspace_manager = hosted_workspace_manager
    app.state.workflow_repository = workflow_repository
    app.state.artifact_service = artifact_service
    app.state.job_service = job_service
    app.state.scientific_runner = scientific_runner
    app.state.library_service = library_service
    app.state.library_index_service = library_index_service
    app.state.discovery_service = discovery_service
    app.state.planning_service = planning_service
    app.state.sections_service = sections_service
    app.state.figures_service = figures_service
    app.state.drafts_service = drafts_service
    app.state.final_service = final_service
    app.state.container = container
    view_root = Path(__file__).resolve().parents[1] / "view"
    react_spa_root = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    react_spa_index = react_spa_root / "index.html"
    react_spa_available = react_spa_index.is_file()

    def portal_csp() -> str:
        return "; ".join(
            (
                "default-src 'self'",
                "base-uri 'none'",
                "connect-src 'self'",
                "font-src 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
                "img-src 'self' data: blob:",
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
        # rendered by the authenticated React workspace in same-origin iframes.
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
        if request.url.path in {
            "/",
            "/admin",
            "/settings",
            "/library",
            "/discovery",
            "/planning",
            "/sections",
            "/images",
            "/draft",
            "/final",
        }:
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
        allowed_prefixes = (
            "/api/v1/auth",
            "/api/v1/provider-settings",
            "/api/v1/admin",
            "/api/docs",
        )
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
        if workflow_repository is not None:
            # A real query makes the container health check sensitive to a
            # broken PostgreSQL connection instead of reporting a false OK.
            workflow_repository.workflow_is_ready()
        return HealthResponse(
            status="ok",
            api_version=API_VERSION,
            deployment_mode=resolved.deployment_mode,
            components={
                "postgresql": "ok" if workflow_repository is not None else "not-configured",
                "job_executor": (
                    "api-compatibility"
                    if resolved.job_execution_enabled
                    else "external-worker"
                ),
                "model_gateway": (
                    "embedded"
                    if resolved.embedded_gateway_routes_enabled
                    else "external"
                ),
            },
        )

    @app.get("/api/v1/model-catalog", response_model=ModelCatalogResponse, tags=["models"])
    def model_catalog(_principal: Principal = Depends(current_principal)) -> ModelCatalogResponse:
        return ModelCatalogResponse(
            default_tier=DEFAULT_MODEL_TIER,
            items=[
                ModelTierResponse(
                    id=tier.id,
                    model=tier.model,
                    label_zh=tier.label_zh,
                    label_en=tier.label_en,
                    description_zh=tier.description_zh,
                    description_en=tier.description_en,
                    input_usd_per_million=format(tier.input_usd_per_million, "f"),
                    cached_input_usd_per_million=format(
                        tier.cached_input_usd_per_million, "f"
                    ),
                    output_usd_per_million=format(tier.output_usd_per_million, "f"),
                )
                for tier in MODEL_TIERS
            ],
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
            password_reset_enabled=hosted and password_reset_mailer is not None,
            password_reset_expiry_minutes=resolved.password_reset_minutes,
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

        password_reset_throttle = AuthAttemptThrottle(
            max_attempts=max(2, resolved.auth_rate_limit_attempts // 2),
            window_seconds=resolved.auth_rate_limit_window_seconds,
        )
        password_reset_ip_throttle = AuthAttemptThrottle(
            max_attempts=resolved.auth_rate_limit_attempts * 5,
            window_seconds=resolved.auth_rate_limit_window_seconds,
        )

        def deliver_password_reset(credential: PasswordResetCredential) -> None:
            reset_url = (
                f"{resolved.public_origin}/login?reset_token={credential.token}"
            )
            try:
                password_reset_mailer.send(
                    credential.recipient,
                    reset_url,
                    resolved.password_reset_minutes,
                )
            except Exception:
                auth_service.invalidate_password_reset(credential.token)
                recipient_hash = hashlib.sha256(
                    credential.recipient.encode("utf-8")
                ).hexdigest()[:12]
                LOGGER.exception(
                    "Password reset email delivery failed (recipient_hash=%s)",
                    recipient_hash,
                )

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

        @app.post(
            "/api/v1/auth/password-reset/request",
            response_model=AuthMessageResponse,
            status_code=status.HTTP_202_ACCEPTED,
            tags=["identity"],
        )
        def request_password_reset(
            payload: PasswordResetRequest,
            request: Request,
            background_tasks: BackgroundTasks,
        ) -> AuthMessageResponse:
            if password_reset_mailer is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="服务器尚未配置密码重置邮件，请联系管理员。",
                )
            client_host = request.client.host if request.client else "unknown"
            identity_hash = hashlib.sha256(
                str(payload.email or "").strip().casefold().encode("utf-8")
            ).hexdigest()
            try:
                password_reset_ip_throttle.consume(f"reset-request-ip:{client_host}")
                password_reset_throttle.consume(
                    f"reset-request:{client_host}:{identity_hash}"
                )
                credential = auth_service.issue_password_reset(email=payload.email)
            except AuthRateLimited as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=str(exc),
                    headers={"Retry-After": str(resolved.auth_rate_limit_window_seconds)},
                ) from exc
            except AuthError:
                credential = None
            if credential is not None:
                background_tasks.add_task(deliver_password_reset, credential)
            return AuthMessageResponse(
                message="如果该邮箱对应有效账户，密码重置邮件将很快送达。"
            )

        @app.post(
            "/api/v1/auth/password-reset/complete",
            response_model=AuthMessageResponse,
            tags=["identity"],
        )
        def complete_password_reset(
            payload: PasswordResetCompleteRequest,
            request: Request,
        ) -> AuthMessageResponse:
            client_host = request.client.host if request.client else "unknown"
            token_identity = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
            try:
                password_reset_ip_throttle.consume(f"reset-complete-ip:{client_host}")
                password_reset_throttle.consume(
                    f"reset-complete:{client_host}:{token_identity}"
                )
                auth_service.reset_password(
                    token=payload.token,
                    new_password=payload.new_password,
                )
            except AuthRateLimited as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=str(exc),
                    headers={"Retry-After": str(resolved.auth_rate_limit_window_seconds)},
                ) from exc
            except AuthError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc
            return AuthMessageResponse(message="密码已经修改，请使用新密码登录。")

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

    if billing_service is not None:

        @app.get("/api/v1/balance", response_model=BalanceResponse, tags=["billing"])
        def balance(
            principal: Principal = Depends(current_principal),
        ) -> BalanceResponse:
            return BalanceResponse.model_validate(
                billing_service.account_summary(principal.user_id)
            )

        @app.get(
            "/api/v1/balance/transactions",
            response_model=CreditTransactionListResponse,
            tags=["billing"],
        )
        def balance_transactions(
            limit: int = 100,
            principal: Principal = Depends(current_principal),
        ) -> CreditTransactionListResponse:
            items = billing_service.transactions(principal.user_id, limit=limit)
            return CreditTransactionListResponse(
                items=[CreditTransactionResponse.model_validate(item) for item in items],
                count=len(items),
            )

        @app.get(
            "/api/v1/admin/users",
            response_model=AdminUserListResponse,
            tags=["admin"],
        )
        def admin_users(
            q: str = "",
            limit: int = 200,
            principal: Principal = Depends(current_principal),
        ) -> AdminUserListResponse:
            principal.require(Permission.PROVIDER_MANAGE)
            items = billing_service.admin_users(query=q, limit=limit)
            return AdminUserListResponse(
                items=[AdminUserResponse.model_validate(item) for item in items],
                count=len(items),
            )

        @app.patch(
            "/api/v1/admin/users/{user_id}",
            response_model=AdminUserResponse,
            tags=["admin"],
        )
        def update_admin_user(
            user_id: str,
            payload: AdminUserUpdateRequest,
            principal: Principal = Depends(current_principal),
        ) -> AdminUserResponse:
            principal.require(Permission.PROVIDER_MANAGE)
            result = billing_service.update_user(
                actor_user_id=principal.user_id,
                target_user_id=user_id,
                role=payload.role,
                status=payload.status,
            )
            return AdminUserResponse.model_validate(result)

        @app.post(
            "/api/v1/admin/credits/adjustments",
            response_model=CreditTransactionResponse,
            tags=["admin"],
        )
        def create_admin_credit_adjustment(
            payload: AdminCreditAdjustmentRequest,
            idempotency_key: str = Header(default="", alias="Idempotency-Key"),
            principal: Principal = Depends(current_principal),
        ) -> CreditTransactionResponse:
            principal.require(Permission.PROVIDER_MANAGE)
            transaction = billing_service.adjust(
                actor_user_id=principal.user_id,
                target_user_id=payload.target_user_id,
                amount_usd=payload.amount_usd,
                reason=payload.reason,
                idempotency_key=idempotency_key,
            )
            return CreditTransactionResponse.model_validate(
                billing_service._transaction_dict(transaction)
            )

        @app.get(
            "/api/v1/admin/usage",
            response_model=AdminUsageSummaryResponse,
            tags=["admin"],
        )
        def admin_usage(
            principal: Principal = Depends(current_principal),
        ) -> AdminUsageSummaryResponse:
            principal.require(Permission.PROVIDER_MANAGE)
            return AdminUsageSummaryResponse.model_validate(
                billing_service.admin_usage_summary()
            )

    @app.get("/api/v1/projects", response_model=ProjectListResponse, tags=["projects"])
    def projects(principal: Principal = Depends(current_principal)) -> ProjectListResponse:
        items = project_service.list_projects(principal)
        return ProjectListResponse(items=items, count=len(items))

    @app.get(
        "/api/v1/taxonomy-profiles",
        response_model=TaxonomyProfileCatalogResponse,
        tags=["projects"],
    )
    def taxonomy_profiles(
        _principal: Principal = Depends(current_principal),
    ) -> TaxonomyProfileCatalogResponse:
        return TaxonomyProfileCatalogResponse(
            items=taxonomy_profile_catalog(),
            default_profile=DEFAULT_TAXONOMY_PROFILE,
        )

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
            model_tier=payload.model_tier,
        )
        return ProjectResponse.model_validate(record)

    @app.patch(
        "/api/v1/projects/{project_id}/model-tier",
        response_model=ProjectResponse,
        tags=["projects"],
    )
    def update_project_model_tier(
        project_id: str,
        payload: ProjectModelTierUpdateRequest,
        principal: Principal = Depends(current_principal),
    ) -> ProjectResponse:
        record = project_service.update_project_model_tier(
            principal, project_id, model_tier=payload.model_tier
        )
        return ProjectResponse.model_validate(record)

    @app.patch(
        "/api/v1/projects/{project_id}/taxonomy-profile",
        response_model=ProjectTaxonomyProfileUpdateResponse,
        tags=["projects"],
    )
    def update_project_taxonomy_profile(
        project_id: str,
        payload: ProjectTaxonomyProfileUpdateRequest,
        principal: Principal = Depends(current_principal),
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

        @app.get(
            "/api/v1/admin/provider-settings",
            response_model=ProviderSettingsListResponse,
            tags=["admin"],
        )
        def admin_provider_settings(
            principal: Principal = Depends(current_principal),
        ) -> ProviderSettingsListResponse:
            principal.require(Permission.PROVIDER_MANAGE)
            records = provider_settings_service.list_settings(principal)
            return ProviderSettingsListResponse(
                items=[
                    ProviderSettingsResponse.model_validate(record, from_attributes=True)
                    for record in records
                ]
            )

        @app.put(
            "/api/v1/admin/provider-settings/{provider_kind}",
            response_model=ProviderSettingsResponse,
            tags=["admin"],
        )
        def update_admin_provider_settings(
            provider_kind: str,
            payload: AdminProviderSettingsUpdateRequest,
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
            "/api/v1/admin/provider-settings/{provider_kind}",
            response_model=ProviderSettingsResponse,
            tags=["admin"],
        )
        def reset_admin_provider_settings(
            provider_kind: str,
            principal: Principal = Depends(current_principal),
        ) -> ProviderSettingsResponse:
            record = provider_settings_service.reset_settings(principal, provider_kind)
            return ProviderSettingsResponse.model_validate(record, from_attributes=True)

        @app.post(
            "/api/v1/admin/provider-settings/{provider_kind}/test",
            response_model=AdminProviderTestResponse,
            tags=["admin"],
        )
        async def test_admin_provider_settings(
            provider_kind: str,
            principal: Principal = Depends(current_principal),
        ) -> AdminProviderTestResponse:
            if (
                not resolved.embedded_gateway_routes_enabled
                and resolved.internal_worker_token
            ):
                result = await asyncio.to_thread(
                    test_provider_through_gateway,
                    resolved.internal_gateway_url,
                    resolved.internal_worker_token,
                    provider_kind=provider_kind,
                    actor_user_id=principal.user_id,
                )
            else:
                result = await provider_settings_service.test_connection(
                    principal, provider_kind
                )
            return AdminProviderTestResponse.model_validate(result, from_attributes=True)

        @app.get(
            "/api/v1/admin/provider-audit",
            response_model=AdminProviderAuditListResponse,
            tags=["admin"],
        )
        def admin_provider_audit(
            limit: int = 50,
            principal: Principal = Depends(current_principal),
        ) -> AdminProviderAuditListResponse:
            records = provider_settings_service.audit_log(principal, limit=limit)
            return AdminProviderAuditListResponse(
                items=[
                    AdminProviderAuditResponse.model_validate(record, from_attributes=True)
                    for record in records
                ]
            )

    if isinstance(model_gateway, ModelGatewayService):

        @app.post(
            "/api/internal/v1/model-responses",
            response_model=ModelGatewayResponse,
            include_in_schema=False,
        )
        async def internal_model_response(
            payload: ModelGatewayRequest,
            request: Request,
        ) -> ModelGatewayResponse:
            if not resolved.embedded_gateway_routes_enabled:
                raise HTTPException(status_code=404, detail="Not Found")
            authorization = str(request.headers.get("Authorization") or "")
            token = (
                authorization[7:].strip()
                if authorization.casefold().startswith("bearer ")
                else ""
            )
            try:
                result = await model_gateway.complete(
                    token,
                    request_key=payload.request_key,
                    stage=payload.stage,
                    prompt=payload.prompt,
                    response_format=payload.response_format,
                )
            except ModelGatewayError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            return ModelGatewayResponse.model_validate(result)

        @app.post(
            "/api/internal/v1/embeddings",
            response_model=EmbeddingGatewayResponse,
            include_in_schema=False,
        )
        async def internal_embeddings(
            payload: EmbeddingGatewayRequest,
            request: Request,
        ) -> EmbeddingGatewayResponse:
            if not resolved.embedded_gateway_routes_enabled:
                raise HTTPException(status_code=404, detail="Not Found")
            authorization = str(request.headers.get("Authorization") or "")
            token = (
                authorization[7:].strip()
                if authorization.casefold().startswith("bearer ")
                else ""
            )
            try:
                result = await model_gateway.complete_embeddings(
                    token,
                    request_key=payload.request_key,
                    stage=payload.stage,
                    inputs=payload.inputs,
                )
            except ModelGatewayError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            return EmbeddingGatewayResponse.model_validate(result)

        @app.post(
            "/api/internal/v1/image-generations",
            response_model=ImageGatewayResponse,
            include_in_schema=False,
        )
        async def internal_image_generation(
            payload: ImageGatewayRequest,
            request: Request,
        ) -> ImageGatewayResponse:
            if not resolved.embedded_gateway_routes_enabled:
                raise HTTPException(status_code=404, detail="Not Found")
            authorization = str(request.headers.get("Authorization") or "")
            token = (
                authorization[7:].strip()
                if authorization.casefold().startswith("bearer ")
                else ""
            )
            try:
                result = await model_gateway.complete_image(
                    token,
                    request_key=payload.request_key,
                    stage=payload.stage,
                    operation=payload.operation,
                    prompt=payload.prompt,
                    images=[item.model_dump() for item in payload.images],
                    quality=payload.quality,
                    background=payload.background,
                    output_format=payload.output_format,
                    size=payload.size,
                )
            except ModelGatewayError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            return ImageGatewayResponse.model_validate(result)

        @app.get(
            "/api/v1/usage/summary",
            response_model=UsageSummaryResponse,
            tags=["usage"],
        )
        def usage_summary(
            project_id: str = "",
            principal: Principal = Depends(current_principal),
        ) -> UsageSummaryResponse:
            resolved_project_id = ""
            if project_id:
                project_record = project_service.get_project(principal, project_id)
                if project_record is None:
                    raise HTTPException(status_code=404, detail="Project not found.")
                resolved_project_id = project_record.project_id
            return UsageSummaryResponse.model_validate(
                model_gateway.usage_summary(principal.user_id, resolved_project_id or None)
            )

        @app.get(
            "/api/v1/usage/timeline",
            response_model=UsageTimelineResponse,
            tags=["usage"],
        )
        def usage_timeline(
            project_id: str = "",
            days: int = 30,
            principal: Principal = Depends(current_principal),
        ) -> UsageTimelineResponse:
            resolved_project_id = ""
            if project_id:
                project_record = project_service.get_project(principal, project_id)
                if project_record is None:
                    raise HTTPException(status_code=404, detail="Project not found.")
                resolved_project_id = project_record.project_id
            return UsageTimelineResponse.model_validate(
                model_gateway.usage_timeline(
                    principal.user_id,
                    resolved_project_id or None,
                    days=days,
                )
            )

    def portal_response() -> Response:
        if not react_spa_available:
            return Response(
                content=(
                    "The React frontend build is unavailable. "
                    "Run the frontend build or use the production container."
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(
            react_spa_index,
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
        )

    @app.get("/", include_in_schema=False)
    def hosted_portal() -> FileResponse:
        return portal_response()

    if react_spa_available:
        app.mount(
            "/assets/react",
            StaticFiles(directory=react_spa_root),
            name="react-spa-assets",
        )
    app.mount(
        "/assets/ketcher",
        StaticFiles(directory=view_root / "assets" / "ketcher"),
        name="ketcher-assets",
    )

    def require_native_workflow() -> None:
        if workflow_repository is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    @app.get("/login", include_in_schema=False)
    @app.get("/workspace", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    @app.get("/library", include_in_schema=False)
    @app.get("/discovery", include_in_schema=False)
    @app.get("/planning", include_in_schema=False)
    @app.get("/sections", include_in_schema=False)
    @app.get("/images", include_in_schema=False)
    @app.get("/draft", include_in_schema=False)
    @app.get("/final", include_in_schema=False)
    def workflow_page(
        _workflow: None = Depends(require_native_workflow),
    ) -> Response:
        # BrowserRouter owns these paths.  Always return the SPA shell on a
        # top-level page refresh; React obtains the current identity from the
        # authenticated API and redirects signed-out/unauthorised users.  The
        # HTML shell contains no protected project data.
        return portal_response()

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
    if (
        library_service is not None
        and library_index_service is not None
        and job_service is not None
    ):
        app.include_router(
            build_library_router(
                current_principal,
                library_service,
                library_index_service,
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
                planning_service,
                native_handlers,
            )
        )
    if planning_service is not None and job_service is not None:
        app.include_router(
            build_planning_router(
                current_principal,
                planning_service,
                job_service,
                native_handlers,
            )
        )
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
