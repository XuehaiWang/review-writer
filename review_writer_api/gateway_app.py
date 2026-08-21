"""Private FastAPI application that owns all text and image provider calls."""

from __future__ import annotations

import hmac
from dataclasses import asdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from review_writer_api.billing import BillingService
from review_writer_api.config import ApiSettings
from review_writer_api.database import create_session_factory, database_session
from review_writer_api.model_gateway import ModelGatewayError, ModelGatewayService
from review_writer_api.schemas import (
    ImageGatewayRequest,
    ImageGatewayResponse,
    ModelGatewayRequest,
    ModelGatewayResponse,
)
from review_writer_api.server_providers import ServerProviderSettingsService
from review_writer_api.security import Principal, Role


class LeaseTokenRequest(BaseModel):
    job_id: str
    lease_token: str
    lease_generation: int = Field(ge=1)


class LeaseTokenResponse(BaseModel):
    task_token: str


class ProviderTestRequest(BaseModel):
    provider_kind: str
    actor_user_id: str


def _bearer_token(request: Request) -> str:
    authorization = str(request.headers.get("Authorization") or "")
    return (
        authorization[7:].strip()
        if authorization.casefold().startswith("bearer ")
        else ""
    )


def create_gateway_app(settings: ApiSettings | None = None) -> FastAPI:
    resolved = settings or ApiSettings.from_env()
    sessions, engine = create_session_factory(resolved.database_url)
    providers = ServerProviderSettingsService(resolved, sessions)
    billing = BillingService(sessions)
    gateway = ModelGatewayService(
        sessions,
        resolved,
        provider_settings=providers,
        billing_service=billing,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await gateway.close()
            engine.dispose()

    app = FastAPI(
        title="Review Writer Internal Model Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    metrics = {"requests": 0, "active": 0, "failures": 0}

    @app.middleware("http")
    async def gateway_metrics(request: Request, call_next):
        is_model_call = request.url.path in {
            "/api/internal/v1/model-responses",
            "/api/internal/v1/image-generations",
        }
        if is_model_call:
            metrics["requests"] += 1
            metrics["active"] += 1
        try:
            response = await call_next(request)
            if is_model_call and response.status_code >= 400:
                metrics["failures"] += 1
            return response
        except Exception:
            if is_model_call:
                metrics["failures"] += 1
            raise
        finally:
            if is_model_call:
                metrics["active"] = max(0, metrics["active"] - 1)

    @app.get("/health")
    def health() -> dict[str, object]:
        try:
            with database_session(sessions) as session:
                session.scalar(select(1))
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="The model gateway database is unavailable."
            ) from exc
        return {
            "status": "ok",
            "service": "model-gateway",
            "postgresql": "ok",
            "requests": metrics["requests"],
            "active": metrics["active"],
            "failures": metrics["failures"],
        }

    @app.post(
        "/api/internal/v1/task-token",
        response_model=LeaseTokenResponse,
        include_in_schema=False,
    )
    def task_token(
        payload: LeaseTokenRequest,
        worker_token: str = Header(default="", alias="X-Review-Writer-Worker-Token"),
    ) -> LeaseTokenResponse:
        expected = resolved.internal_worker_token
        if not expected or not hmac.compare_digest(worker_token, expected):
            raise HTTPException(status_code=401, detail="Worker authentication failed.")
        try:
            token = gateway.issue_leased_task_token(
                job_id=payload.job_id,
                lease_token=payload.lease_token,
                lease_generation=payload.lease_generation,
                lifetime_seconds=8 * 60 * 60,
            )
        except ModelGatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return LeaseTokenResponse(task_token=token)

    @app.post("/api/internal/v1/provider-test", include_in_schema=False)
    async def provider_test(
        payload: ProviderTestRequest,
        worker_token: str = Header(default="", alias="X-Review-Writer-Worker-Token"),
    ) -> dict[str, object]:
        expected = resolved.internal_worker_token
        if not expected or not hmac.compare_digest(worker_token, expected):
            raise HTTPException(status_code=401, detail="Service authentication failed.")
        try:
            principal = Principal(
                payload.actor_user_id,
                frozenset({Role.ADMIN}),
            )
            return asdict(
                await providers.test_connection(principal, payload.provider_kind)
            )
        except Exception as exc:
            # ProviderSettingsError is intentionally kept behind the private
            # boundary; public API returns its stable validation response.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/internal/v1/model-responses",
        response_model=ModelGatewayResponse,
        include_in_schema=False,
    )
    async def model_response(
        payload: ModelGatewayRequest, request: Request
    ) -> ModelGatewayResponse:
        try:
            result = await gateway.complete(
                _bearer_token(request),
                request_key=payload.request_key,
                stage=payload.stage,
                prompt=payload.prompt,
                response_format=payload.response_format,
            )
        except ModelGatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return ModelGatewayResponse.model_validate(result)

    @app.post(
        "/api/internal/v1/image-generations",
        response_model=ImageGatewayResponse,
        include_in_schema=False,
    )
    async def image_generation(
        payload: ImageGatewayRequest, request: Request
    ) -> ImageGatewayResponse:
        try:
            result = await gateway.complete_image(
                _bearer_token(request),
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

    app.state.model_gateway = gateway
    return app
