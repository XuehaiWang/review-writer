"""Internal text-model gateway with task tokens, idempotency, and usage metering."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import random
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .config import ApiSettings
from review_writer_core.providers import DEFAULT_IMAGE_MODEL

from .database import (
    AIImageRequest,
    AIModelRequest,
    MinerUUsageEvent,
    Project,
    database_session,
    utc_now,
)
from .model_catalog import DEFAULT_MODEL_TIER, ModelTier, resolve_model_tier
from .server_providers import ServerProviderRuntime, ServerProviderSettingsService
from .workflow_models import WorkflowJob


class ModelGatewayError(RuntimeError):
    status_code = 502


class InvalidTaskToken(ModelGatewayError):
    status_code = 401


class GatewayConfigurationError(ModelGatewayError):
    status_code = 503


class GatewayRequestConflict(ModelGatewayError):
    status_code = 409


class GatewayProviderError(ModelGatewayError):
    status_code = 502


@dataclass(frozen=True)
class TaskClaims:
    job_id: str
    user_id: str
    project_id: str | None
    job_type: str
    model_tier: str
    capabilities: tuple[str, ...]
    expires_at: int


TEXT_GATEWAY_JOB_TYPES = frozenset(
    {
        "discovery.search",
        "sections.generate",
        "planning.reference-analyze",
        "draft.evaluate",
        "draft.optimize",
        "draft.rewrite",
        "draft.accept-rewrite",
        "final.conclusion",
    }
)
IMAGE_GATEWAY_JOB_TYPES = frozenset({"figures.redraw", "final.overview"})


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def calculate_provider_cost(
    tier: ModelTier,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> Decimal:
    cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
    uncached = max(0, int(input_tokens) - cached)
    cost = (
        Decimal(uncached) * tier.input_usd_per_million
        + Decimal(cached) * tier.cached_input_usd_per_million
        + Decimal(max(0, int(output_tokens))) * tier.output_usd_per_million
    ) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.00000001"))


class ModelGatewayService:
    TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        session_factory,
        settings: ApiSettings,
        *,
        provider_settings: ServerProviderSettingsService | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.provider_settings = provider_settings
        try:
            decoded = _b64decode(settings.credential_encryption_key)
        except Exception:
            decoded = settings.credential_encryption_key.encode("utf-8")
        self._signing_key = hmac.new(
            decoded, b"review-writer/internal-model-gateway/v1", hashlib.sha256
        ).digest()
        self._global_slots = asyncio.Semaphore(settings.model_gateway_max_concurrency)
        self._user_slots: dict[str, asyncio.Semaphore] = {}
        self._user_slots_lock = asyncio.Lock()
        self._image_global_slots = asyncio.Semaphore(settings.image_gateway_max_concurrency)
        self._image_user_slots: dict[str, asyncio.Semaphore] = {}
        self._image_user_slots_lock = asyncio.Lock()
        hosted_root = settings.hosted_workspace_root or (
            settings.review_root / ".review-writer" / "hosted-workspaces"
        )
        self._image_cache_root = (hosted_root / ".gateway-image-cache").resolve()
        self._image_cache_root.mkdir(parents=True, exist_ok=True)
        self._provider_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0)
        )

    def _text_runtime(self) -> ServerProviderRuntime:
        if self.provider_settings is not None:
            return self.provider_settings.runtime_config("text")
        secret = self.settings.text_provider_api_key
        return ServerProviderRuntime(
            "text", self.settings.text_provider_base_url,
            resolve_model_tier(DEFAULT_MODEL_TIER).model,
            self.settings.text_provider_wire_api, secret, bool(secret),
            "environment", "",
        )

    def _image_runtime(self) -> ServerProviderRuntime:
        if self.provider_settings is not None:
            return self.provider_settings.runtime_config("image")
        secret = self.settings.image_provider_api_key
        return ServerProviderRuntime(
            "image", self.settings.image_provider_base_url,
            self.settings.image_provider_model or DEFAULT_IMAGE_MODEL,
            self.settings.image_provider_wire_api, secret, bool(secret),
            "environment", "",
        )

    async def close(self) -> None:
        await self._provider_client.aclose()

    def issue_task_token(
        self,
        *,
        job_id: str,
        user_id: str,
        project_id: str | None,
        job_type: str,
        lifetime_seconds: int = 8 * 60 * 60,
    ) -> str:
        model_tier = DEFAULT_MODEL_TIER
        if project_id:
            with database_session(self.session_factory) as session:
                project = session.get(Project, uuid.UUID(project_id))
                if project is None or str(project.user_id) != user_id:
                    raise InvalidTaskToken("Task project does not belong to the task user.")
                model_tier = resolve_model_tier(project.model_tier).id
        capabilities: list[str] = []
        if job_type in TEXT_GATEWAY_JOB_TYPES:
            capabilities.append("text")
        if job_type in IMAGE_GATEWAY_JOB_TYPES:
            capabilities.append("image")
        payload = {
            "v": 2,
            "job_id": str(uuid.UUID(job_id)),
            "user_id": str(uuid.UUID(user_id)),
            "project_id": str(uuid.UUID(project_id)) if project_id else None,
            "job_type": str(job_type),
            "model_tier": model_tier,
            "capabilities": capabilities,
            "exp": int(time.time()) + max(60, int(lifetime_seconds)),
            "jti": uuid.uuid4().hex,
        }
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(hmac.new(self._signing_key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify_task_token(self, token: str) -> TaskClaims:
        try:
            encoded, supplied_signature = str(token or "").split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self._signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("signature")
            payload = json.loads(_b64decode(encoded).decode("utf-8"))
            if int(payload.get("v") or 0) != 2 or int(payload.get("exp") or 0) < int(time.time()):
                raise ValueError("expired")
            claims = TaskClaims(
                job_id=str(uuid.UUID(str(payload["job_id"]))),
                user_id=str(uuid.UUID(str(payload["user_id"]))),
                project_id=(
                    str(uuid.UUID(str(payload["project_id"])))
                    if payload.get("project_id")
                    else None
                ),
                job_type=str(payload["job_type"]),
                model_tier=resolve_model_tier(str(payload["model_tier"])).id,
                capabilities=tuple(
                    str(item)
                    for item in payload.get("capabilities") or []
                    if str(item) in {"text", "image"}
                ),
                expires_at=int(payload["exp"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
            raise InvalidTaskToken("The internal task token is invalid or expired.") from exc
        return claims

    @staticmethod
    def _require_capability(claims: TaskClaims, capability: str) -> None:
        if capability not in claims.capabilities:
            raise InvalidTaskToken(
                f"The task token is not authorized for {capability} model calls."
            )

    def environment_for_job(self, context) -> tuple[dict[str, str], dict[str, str]]:
        token = self.issue_task_token(
            job_id=context.job_id,
            user_id=context.user_id,
            project_id=context.project_id,
            job_type=context.job_type,
        )
        return (
            {
                "REVIEW_WRITER_MODEL_GATEWAY_URL": self.settings.internal_gateway_url,
                "REVIEW_WRITER_IMAGE_GATEWAY_URL": (
                    self.settings.internal_gateway_url.rsplit("/", 1)[0]
                    + "/image-generations"
                ),
            },
            {"REVIEW_WRITER_TASK_TOKEN": token},
        )

    def _validate_live_job(self, claims: TaskClaims) -> None:
        with database_session(self.session_factory) as session:
            job = session.get(WorkflowJob, uuid.UUID(claims.job_id))
            if (
                job is None
                or str(job.user_id) != claims.user_id
                or (str(job.project_id) if job.project_id else None) != claims.project_id
                or job.job_type != claims.job_type
                or job.status != "running"
            ):
                raise InvalidTaskToken("The task token is not bound to a running job.")

    async def _user_semaphore(self, user_id: str) -> asyncio.Semaphore:
        async with self._user_slots_lock:
            return self._user_slots.setdefault(
                user_id, asyncio.Semaphore(self.settings.model_gateway_user_concurrency)
            )

    async def _image_user_semaphore(self, user_id: str) -> asyncio.Semaphore:
        async with self._image_user_slots_lock:
            return self._image_user_slots.setdefault(
                user_id, asyncio.Semaphore(self.settings.image_gateway_user_concurrency)
            )

    @staticmethod
    def _request_sha256(stage: str, prompt: str, response_format: str) -> str:
        canonical = json.dumps(
            {"stage": stage, "prompt": prompt, "response_format": response_format},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _begin_request(
        self,
        claims: TaskClaims,
        *,
        request_key: str,
        stage: str,
        request_sha256: str,
        tier: ModelTier,
    ) -> tuple[str, dict[str, Any] | None]:
        with database_session(self.session_factory) as session:
            existing = session.scalar(
                select(AIModelRequest).where(
                    AIModelRequest.job_id == uuid.UUID(claims.job_id),
                    AIModelRequest.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise GatewayRequestConflict(
                        "The request key was reused with different content."
                    )
                if existing.status == "succeeded":
                    return str(existing.id), dict(existing.response_json or {})
                if existing.status == "running":
                    raise GatewayRequestConflict("The same model request is still running.")
                existing.status = "running"
                existing.attempt_count += 1
                existing.error_message = ""
                existing.updated_at = utc_now()
                return str(existing.id), None

            row = AIModelRequest(
                user_id=uuid.UUID(claims.user_id),
                project_id=uuid.UUID(claims.project_id) if claims.project_id else None,
                job_id=uuid.UUID(claims.job_id),
                request_key=request_key,
                stage=stage,
                model_tier=tier.id,
                model_name=tier.model,
                request_sha256=request_sha256,
                status="running",
                input_price_usd_per_million=tier.input_usd_per_million,
                cached_input_price_usd_per_million=tier.cached_input_usd_per_million,
                output_price_usd_per_million=tier.output_usd_per_million,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise GatewayRequestConflict("The same model request was submitted concurrently.") from exc
            return str(row.id), None

    @staticmethod
    def _usage(data: dict[str, Any]) -> dict[str, int]:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
        cached = int(input_details.get("cached_tokens") or 0) if isinstance(input_details, dict) else 0
        reasoning = int(output_details.get("reasoning_tokens") or 0) if isinstance(output_details, dict) else 0
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning,
            "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
        }

    @staticmethod
    def _output_text(data: dict[str, Any], wire_api: str) -> str:
        if wire_api in {"chat", "chat-completion", "chat-completions"}:
            choices = data.get("choices") if isinstance(data.get("choices"), list) else []
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
            content = message.get("content") if isinstance(message, dict) else ""
            if isinstance(content, list):
                return "\n".join(
                    str(item.get("text") or "") for item in content if isinstance(item, dict)
                )
            return str(content or "")
        text = str(data.get("output_text") or "")
        if text:
            return text
        return "\n".join(
            str(content.get("text") or "")
            for output in data.get("output") or []
            if isinstance(output, dict)
            for content in output.get("content") or []
            if isinstance(content, dict)
        )

    async def _provider_call(
        self,
        *,
        tier: ModelTier,
        prompt: str,
        idempotency_key: str,
        response_format: str = "json",
    ) -> dict[str, Any]:
        runtime = self._text_runtime()
        if not runtime.enabled:
            raise GatewayConfigurationError("The server text provider is not configured.")
        wire = runtime.wire_api
        if wire in {"chat", "chat-completion", "chat-completions"}:
            endpoint = f"{runtime.base_url}/chat/completions"
            payload = {
                "model": tier.model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            prompt + "\nReturn only one valid JSON object."
                            if response_format == "json"
                            else prompt
                        ),
                    }
                ],
            }
        else:
            endpoint = f"{runtime.base_url}/responses"
            payload = {"model": tier.model, "input": [{"role": "user", "content": prompt}]}
        headers = {
            "Authorization": f"Bearer {runtime.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        for attempt in range(1, 4):
            try:
                response = await self._provider_client.post(
                    endpoint, json=payload, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= 3:
                    raise GatewayProviderError(
                        f"Model provider transport failed after {attempt} attempts."
                    ) from exc
            else:
                if response.status_code < 400:
                    try:
                        result = response.json()
                    except ValueError as exc:
                        raise GatewayProviderError("Model provider returned invalid JSON.") from exc
                    if not isinstance(result, dict):
                        raise GatewayProviderError("Model provider returned an invalid payload.")
                    return result
                if response.status_code not in self.TRANSIENT_STATUSES or attempt >= 3:
                    detail = response.text[:500].replace("\n", " ")
                    raise GatewayProviderError(
                        f"Model provider returned HTTP {response.status_code}: {detail}"
                    )
            await asyncio.sleep(min(8.0, (2 ** (attempt - 1)) + random.random()))
        raise GatewayProviderError("Model provider request failed.")

    def _finish_request(
        self,
        request_id: str,
        *,
        response_payload: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        with database_session(self.session_factory) as session:
            row = session.get(AIModelRequest, uuid.UUID(request_id))
            if row is None:
                return
            if response_payload is None:
                row.status = "failed"
                row.error_message = error_message[:2000]
                row.finished_at = utc_now()
                return
            usage = dict(response_payload.get("usage") or {})
            row.status = "succeeded"
            row.provider_request_id = str(response_payload.get("provider_request_id") or "")
            row.input_tokens = int(usage.get("input_tokens") or 0)
            row.cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
            row.output_tokens = int(usage.get("output_tokens") or 0)
            row.reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
            row.total_tokens = int(usage.get("total_tokens") or 0)
            row.provider_cost_usd = Decimal(str(response_payload.get("cost_usd") or "0"))
            row.response_json = response_payload
            row.error_message = ""
            row.finished_at = utc_now()

    async def complete(
        self,
        token: str,
        *,
        request_key: str,
        stage: str,
        prompt: str,
        response_format: str = "json",
    ) -> dict[str, Any]:
        claims = self.verify_task_token(token)
        self._require_capability(claims, "text")
        self._validate_live_job(claims)
        key = str(request_key or "").strip()
        if not key or len(key) > 128:
            raise GatewayRequestConflict("A request key of at most 128 characters is required.")
        normalized_stage = str(stage or claims.job_type).strip()[:96]
        normalized_format = str(response_format or "json").strip().casefold()
        if normalized_format not in {"json", "text"}:
            raise GatewayRequestConflict("The response format must be json or text.")
        if not prompt or len(prompt) > 4_000_000:
            raise GatewayRequestConflict("The model prompt is empty or too large.")
        tier = resolve_model_tier(claims.model_tier)
        digest = self._request_sha256(normalized_stage, prompt, normalized_format)
        request_id, cached = self._begin_request(
            claims,
            request_key=key,
            stage=normalized_stage,
            request_sha256=digest,
            tier=tier,
        )
        if cached is not None:
            return {**cached, "cached": True}
        user_slot = await self._user_semaphore(claims.user_id)
        try:
            async with self._global_slots, user_slot:
                provider_data = await self._provider_call(
                    tier=tier,
                    prompt=prompt,
                    idempotency_key=f"{claims.job_id}:{key}",
                    response_format=normalized_format,
                )
            usage = self._usage(provider_data)
            cost = calculate_provider_cost(tier, **{
                "input_tokens": usage["input_tokens"],
                "cached_input_tokens": usage["cached_input_tokens"],
                "output_tokens": usage["output_tokens"],
            })
            response = {
                "request_id": request_id,
                "provider_request_id": str(provider_data.get("id") or ""),
                "model_tier": tier.id,
                "model": tier.model,
                "output_text": self._output_text(
                    provider_data, self._text_runtime().wire_api
                ),
                "usage": usage,
                "cost_usd": format(cost, "f"),
                "cached": False,
            }
            if not response["output_text"]:
                raise GatewayProviderError("Model provider returned an empty response.")
            self._finish_request(request_id, response_payload=response)
            return response
        except Exception as exc:
            self._finish_request(request_id, error_message=str(exc))
            raise

    async def complete_json(
        self,
        token: str,
        *,
        request_key: str,
        stage: str,
        prompt: str,
    ) -> dict[str, Any]:
        return await self.complete(
            token,
            request_key=request_key,
            stage=stage,
            prompt=prompt,
            response_format="json",
        )

    @staticmethod
    def _decode_image_inputs(images: list[dict[str, str]]) -> list[tuple[str, bytes]]:
        decoded: list[tuple[str, bytes]] = []
        total_bytes = 0
        for item in images:
            mime_type = str(item.get("mime_type") or "image/png").strip().casefold()
            if not mime_type.startswith("image/") or len(mime_type) > 100:
                raise GatewayRequestConflict("Image input has an invalid media type.")
            try:
                raw = base64.b64decode(str(item.get("data_base64") or ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise GatewayRequestConflict("Image input is not valid base64.") from exc
            if not raw:
                raise GatewayRequestConflict("Image input is empty.")
            total_bytes += len(raw)
            if total_bytes > 60 * 1024 * 1024:
                raise GatewayRequestConflict("Image inputs exceed the 60 MB task limit.")
            decoded.append((mime_type, raw))
        return decoded

    @staticmethod
    def _image_request_sha256(
        *,
        stage: str,
        operation: str,
        prompt: str,
        images: list[tuple[str, bytes]],
        quality: str,
        background: str,
        output_format: str,
        size: str,
    ) -> str:
        canonical = {
            "stage": stage,
            "operation": operation,
            "prompt": prompt,
            "images": [
                {"mime_type": mime, "sha256": hashlib.sha256(raw).hexdigest()}
                for mime, raw in images
            ],
            "quality": quality,
            "background": background,
            "output_format": output_format,
            "size": size,
        }
        return hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def _image_cache_path(self, request_id: str) -> Path:
        return self._image_cache_root / f"{uuid.UUID(request_id)}.bin"

    def _begin_image_request(
        self,
        claims: TaskClaims,
        *,
        request_key: str,
        stage: str,
        operation: str,
        request_sha256: str,
        model_name: str,
    ) -> tuple[str, dict[str, Any] | None]:
        with database_session(self.session_factory) as session:
            existing = session.scalar(
                select(AIImageRequest).where(
                    AIImageRequest.job_id == uuid.UUID(claims.job_id),
                    AIImageRequest.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise GatewayRequestConflict(
                        "The image request key was reused with different content."
                    )
                cached = dict(existing.response_json or {})
                if (
                    existing.status == "succeeded"
                    and self._image_cache_path(str(existing.id)).is_file()
                ):
                    cached.update(
                        {
                            "request_id": str(existing.id),
                            "provider_request_id": existing.provider_request_id,
                            "model": existing.model_name,
                            "image_count": existing.image_count,
                            "provider_attempt_count": existing.provider_attempt_count,
                            "cost_usd": format(existing.provider_cost_usd, "f"),
                        }
                    )
                    return str(existing.id), cached
                if existing.status == "running":
                    raise GatewayRequestConflict("The same image request is still running.")
                existing.status = "running"
                existing.attempt_count += 1
                existing.error_message = ""
                existing.updated_at = utc_now()
                return str(existing.id), None

            row = AIImageRequest(
                user_id=uuid.UUID(claims.user_id),
                project_id=uuid.UUID(claims.project_id) if claims.project_id else None,
                job_id=uuid.UUID(claims.job_id),
                request_key=request_key,
                stage=stage,
                operation=operation,
                model_name=model_name,
                request_sha256=request_sha256,
                status="running",
                unit_price_usd=self.settings.image_provider_price_usd_per_image,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise GatewayRequestConflict(
                    "The same image request was submitted concurrently."
                ) from exc
            return str(row.id), None

    @staticmethod
    def _image_reference(value: Any) -> tuple[str, str, str] | None:
        if isinstance(value, list):
            for item in value:
                found = ModelGatewayService._image_reference(item)
                if found:
                    return found
            return None
        if not isinstance(value, dict):
            return None
        for key in ("b64_json", "result", "image_base64"):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate.strip()) >= 32:
                return "base64", candidate.strip(), "image/png"
        for key in ("url", "image_url"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                candidate = candidate.get("url")
            if not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            if candidate.startswith("data:image/") and ";base64," in candidate:
                header, encoded = candidate.split(",", 1)
                mime_type = header[5:].split(";", 1)[0]
                return "base64", encoded, mime_type
            if candidate.startswith(("https://", "http://")):
                return "url", candidate, ""
        for key in (
            "data",
            "images",
            "image",
            "result",
            "output",
            "content",
            "choices",
            "message",
            "delta",
        ):
            if key in value:
                found = ModelGatewayService._image_reference(value[key])
                if found:
                    return found
        return None

    @staticmethod
    def _sse_events(raw: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    async def _resolve_provider_image(
        self, reference: tuple[str, str, str]
    ) -> tuple[bytes, str]:
        kind, value, mime_type = reference
        if kind == "base64":
            try:
                raw = base64.b64decode(value, validate=True)
            except ValueError as exc:
                raise GatewayProviderError("Image provider returned invalid base64.") from exc
            if not raw:
                raise GatewayProviderError("Image provider returned an empty image.")
            return raw, mime_type or "image/png"
        try:
            response = await self._provider_client.get(value, follow_redirects=True)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise GatewayProviderError("Generated image download failed.") from exc
        if response.status_code >= 400:
            raise GatewayProviderError(
                f"Generated image download returned HTTP {response.status_code}."
            )
        raw = bytes(response.content)
        if not raw:
            raise GatewayProviderError("Generated image download was empty.")
        content_type = str(response.headers.get("content-type") or "image/png")
        return raw, content_type.split(";", 1)[0]

    async def _provider_image_call(
        self,
        *,
        operation: str,
        prompt: str,
        images: list[tuple[str, bytes]],
        quality: str,
        background: str,
        output_format: str,
        size: str,
        idempotency_key: str,
        runtime: ServerProviderRuntime,
    ) -> tuple[bytes, str, str, int]:
        if not runtime.enabled:
            raise GatewayConfigurationError("The server image provider is not configured.")
        model = runtime.model_name or DEFAULT_IMAGE_MODEL
        wire = runtime.wire_api
        base_url = runtime.base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {runtime.api_key}",
            "Accept": "application/json, text/event-stream",
            "Idempotency-Key": idempotency_key,
        }
        for attempt in range(1, 4):
            try:
                if wire in {"chat", "chat-completion", "chat-completions"}:
                    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                    for mime_type, raw in images:
                        encoded = base64.b64encode(raw).decode("ascii")
                        content.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{encoded}",
                                    "detail": "high",
                                },
                            }
                        )
                    response = await self._provider_client.post(
                        f"{base_url}/chat/completions",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": content}],
                            "stream": attempt < 3,
                        },
                        headers={**headers, "Content-Type": "application/json"},
                    )
                elif wire == "responses":
                    content = [{"type": "input_text", "text": prompt}]
                    for mime_type, raw in images:
                        encoded = base64.b64encode(raw).decode("ascii")
                        content.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{mime_type};base64,{encoded}",
                            }
                        )
                    response = await self._provider_client.post(
                        f"{base_url}/responses",
                        json={
                            "model": model,
                            "input": [{"role": "user", "content": content}],
                            "tools": [
                                {
                                    "type": "image_generation",
                                    "quality": quality,
                                    "background": background,
                                    "output_format": output_format,
                                }
                            ],
                        },
                        headers={**headers, "Content-Type": "application/json"},
                    )
                else:
                    endpoint = "edits" if images and operation == "edit" else "generations"
                    data = {
                        "model": model,
                        "prompt": prompt,
                        "quality": quality,
                        "background": background,
                        "output_format": output_format,
                        "response_format": "b64_json",
                    }
                    if size:
                        data["size"] = size
                    files = None
                    if images and endpoint == "edits":
                        files = []
                        for index, (mime_type, raw) in enumerate(images):
                            field = "image" if index == 0 else "image[]"
                            files.append((field, (f"input-{index + 1}.png", raw, mime_type)))
                    response = await self._provider_client.post(
                        f"{base_url}/images/{endpoint}",
                        data=data,
                        files=files,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= 3:
                    raise GatewayProviderError(
                        f"Image provider transport failed after {attempt} attempts."
                    ) from exc
            else:
                if response.status_code < 400:
                    raw_text = response.text
                    content_type = str(response.headers.get("content-type") or "").casefold()
                    if "text/event-stream" in content_type or raw_text.lstrip().startswith("data:"):
                        data_value: Any = self._sse_events(raw_text)
                    else:
                        try:
                            data_value = response.json()
                        except ValueError as exc:
                            raise GatewayProviderError(
                                "Image provider returned invalid JSON."
                            ) from exc
                    reference = self._image_reference(data_value)
                    if reference:
                        image_bytes, mime_type = await self._resolve_provider_image(reference)
                        provider_id = ""
                        if isinstance(data_value, dict):
                            provider_id = str(data_value.get("id") or "")
                        elif isinstance(data_value, list):
                            provider_id = next(
                                (
                                    str(item.get("id") or "")
                                    for item in reversed(data_value)
                                    if isinstance(item, dict) and item.get("id")
                                ),
                                "",
                            )
                        return image_bytes, mime_type, provider_id, attempt
                    if attempt >= 3:
                        raise GatewayProviderError(
                            "Image provider response did not contain an image."
                        )
                elif response.status_code not in self.TRANSIENT_STATUSES or attempt >= 3:
                    detail = response.text[:500].replace("\n", " ")
                    raise GatewayProviderError(
                        f"Image provider returned HTTP {response.status_code}: {detail}"
                    )
            await asyncio.sleep(min(8.0, (2 ** (attempt - 1)) + random.random()))
        raise GatewayProviderError("Image provider request failed.")

    def _finish_image_request(
        self,
        request_id: str,
        *,
        provider_request_id: str = "",
        provider_attempt_count: int = 0,
        image_count: int = 0,
        mime_type: str = "",
        cost_usd: Decimal = Decimal("0"),
        error_message: str = "",
    ) -> None:
        with database_session(self.session_factory) as session:
            row = session.get(AIImageRequest, uuid.UUID(request_id))
            if row is None:
                return
            row.provider_attempt_count = int(provider_attempt_count)
            if error_message:
                row.status = "failed"
                row.error_message = error_message[:2000]
                row.finished_at = utc_now()
                return
            row.status = "succeeded"
            row.provider_request_id = provider_request_id
            row.image_count = image_count
            row.provider_cost_usd = cost_usd
            row.response_json = {"image_mime_type": mime_type}
            row.error_message = ""
            row.finished_at = utc_now()

    async def complete_image(
        self,
        token: str,
        *,
        request_key: str,
        stage: str,
        operation: str,
        prompt: str,
        images: list[dict[str, str]],
        quality: str = "high",
        background: str = "opaque",
        output_format: str = "png",
        size: str = "",
    ) -> dict[str, Any]:
        claims = self.verify_task_token(token)
        self._require_capability(claims, "image")
        self._validate_live_job(claims)
        key = str(request_key or "").strip()
        if not key or len(key) > 128:
            raise GatewayRequestConflict("An image request key of at most 128 characters is required.")
        normalized_stage = str(stage or claims.job_type).strip()[:96]
        normalized_operation = str(operation or "edit").strip().casefold()
        if normalized_operation not in {"edit", "generate"}:
            raise GatewayRequestConflict("Image operation must be edit or generate.")
        if not prompt or len(prompt) > 100_000:
            raise GatewayRequestConflict("The image prompt is empty or too large.")
        decoded_images = self._decode_image_inputs(images)
        if normalized_operation == "edit" and not decoded_images:
            raise GatewayRequestConflict("Image edit requires at least one input image.")
        provider_runtime = self._image_runtime()
        model = provider_runtime.model_name or DEFAULT_IMAGE_MODEL
        digest = self._image_request_sha256(
            stage=normalized_stage,
            operation=normalized_operation,
            prompt=prompt,
            images=decoded_images,
            quality=quality,
            background=background,
            output_format=output_format,
            size=size,
        )
        request_id, cached = self._begin_image_request(
            claims,
            request_key=key,
            stage=normalized_stage,
            operation=normalized_operation,
            request_sha256=digest,
            model_name=model,
        )
        if cached is not None:
            image_bytes = await asyncio.to_thread(self._image_cache_path(request_id).read_bytes)
            return {
                **cached,
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                "cached": True,
            }
        user_slot = await self._image_user_semaphore(claims.user_id)
        try:
            async with self._image_global_slots, user_slot:
                image_bytes, mime_type, provider_id, provider_attempts = (
                    await self._provider_image_call(
                        operation=normalized_operation,
                        prompt=prompt,
                        images=decoded_images,
                        quality=quality,
                        background=background,
                        output_format=output_format,
                        size=size,
                        idempotency_key=f"{claims.job_id}:{key}",
                        runtime=provider_runtime,
                    )
                )
            cache_path = self._image_cache_path(request_id)

            def write_cache() -> None:
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_bytes(image_bytes)
                temporary.replace(cache_path)

            await asyncio.to_thread(write_cache)
            cost = self.settings.image_provider_price_usd_per_image.quantize(
                Decimal("0.00000001")
            )
            self._finish_image_request(
                request_id,
                provider_request_id=provider_id,
                provider_attempt_count=provider_attempts,
                image_count=1,
                mime_type=mime_type,
                cost_usd=cost,
            )
            return {
                "request_id": request_id,
                "provider_request_id": provider_id,
                "model": model,
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                "image_mime_type": mime_type,
                "image_count": 1,
                "provider_attempt_count": provider_attempts,
                "cost_usd": format(cost, "f"),
                "cached": False,
            }
        except Exception as exc:
            self._finish_image_request(request_id, error_message=str(exc))
            raise

    def usage_summary(self, user_id: str, project_id: str | None = None) -> dict[str, Any]:
        filters = [
            AIModelRequest.user_id == uuid.UUID(user_id),
            AIModelRequest.status == "succeeded",
        ]
        if project_id:
            filters.append(AIModelRequest.project_id == uuid.UUID(project_id))
        with database_session(self.session_factory) as session:
            row = session.execute(
                select(
                    func.count(AIModelRequest.id),
                    func.coalesce(func.sum(AIModelRequest.input_tokens), 0),
                    func.coalesce(func.sum(AIModelRequest.cached_input_tokens), 0),
                    func.coalesce(func.sum(AIModelRequest.output_tokens), 0),
                    func.coalesce(func.sum(AIModelRequest.total_tokens), 0),
                    func.coalesce(func.sum(AIModelRequest.provider_cost_usd), 0),
                ).where(*filters)
            ).one()
            image_filters = [
                AIImageRequest.user_id == uuid.UUID(user_id),
                AIImageRequest.status == "succeeded",
            ]
            if project_id:
                image_filters.append(AIImageRequest.project_id == uuid.UUID(project_id))
            image_row = session.execute(
                select(
                    func.count(AIImageRequest.id),
                    func.coalesce(func.sum(AIImageRequest.image_count), 0),
                    func.coalesce(func.sum(AIImageRequest.provider_cost_usd), 0),
                ).where(*image_filters)
            ).one()
            mineru_filters = [MinerUUsageEvent.user_id == uuid.UUID(user_id)]
            if project_id:
                mineru_filters.append(
                    MinerUUsageEvent.project_id == uuid.UUID(project_id)
                )
            mineru_row = session.execute(
                select(
                    func.count(MinerUUsageEvent.id).filter(
                        MinerUUsageEvent.status.in_(
                            ["succeeded", "reconciliation_required"]
                        )
                    ),
                    func.coalesce(func.sum(MinerUUsageEvent.billable_pages), 0),
                    func.coalesce(func.sum(MinerUUsageEvent.cache_hit_count), 0),
                    func.coalesce(func.sum(MinerUUsageEvent.provider_cost_usd), 0),
                ).where(*mineru_filters)
            ).one()
        text_cost = Decimal(str(row[5]))
        image_cost = Decimal(str(image_row[2]))
        mineru_cost = Decimal(str(mineru_row[3]))
        return {
            "request_count": int(row[0]),
            "input_tokens": int(row[1]),
            "cached_input_tokens": int(row[2]),
            "output_tokens": int(row[3]),
            "total_tokens": int(row[4]),
            "image_request_count": int(image_row[0]),
            "image_count": int(image_row[1]),
            "estimated_text_cost_usd": format(text_cost, "f"),
            "estimated_image_cost_usd": format(image_cost, "f"),
            "mineru_request_count": int(mineru_row[0]),
            "mineru_billable_pages": int(mineru_row[1]),
            "mineru_cache_hit_count": int(mineru_row[2]),
            "estimated_mineru_cost_usd": format(mineru_cost, "f"),
            "estimated_cost_usd": format(text_cost + image_cost + mineru_cost, "f"),
            "billing_mode": "record_only",
        }

    def usage_timeline(
        self,
        user_id: str,
        project_id: str | None = None,
        *,
        days: int = 30,
    ) -> dict[str, Any]:
        """Return zero-filled daily usage buckets for the settings dashboard."""

        safe_days = max(7, min(int(days), 90))
        end_day = utc_now().date()
        start_day = end_day - timedelta(days=safe_days - 1)
        buckets: dict[str, dict[str, Any]] = {}
        for offset in range(safe_days):
            day = start_day + timedelta(days=offset)
            buckets[day.isoformat()] = {
                "date": day.isoformat(),
                "request_count": 0,
                "total_tokens": 0,
                "image_count": 0,
                "mineru_pages": 0,
                "estimated_cost_usd": Decimal("0"),
            }

        user_uuid = uuid.UUID(user_id)
        project_uuid = uuid.UUID(project_id) if project_id else None
        cutoff = utc_now() - timedelta(days=safe_days)
        with database_session(self.session_factory) as session:
            text_filters = [
                AIModelRequest.user_id == user_uuid,
                AIModelRequest.status == "succeeded",
                AIModelRequest.created_at >= cutoff,
            ]
            image_filters = [
                AIImageRequest.user_id == user_uuid,
                AIImageRequest.status == "succeeded",
                AIImageRequest.created_at >= cutoff,
            ]
            mineru_filters = [
                MinerUUsageEvent.user_id == user_uuid,
                MinerUUsageEvent.status.in_(["succeeded", "reconciliation_required"]),
                MinerUUsageEvent.created_at >= cutoff,
            ]
            if project_uuid:
                text_filters.append(AIModelRequest.project_id == project_uuid)
                image_filters.append(AIImageRequest.project_id == project_uuid)
                mineru_filters.append(MinerUUsageEvent.project_id == project_uuid)
            text_rows = session.execute(
                select(
                    AIModelRequest.created_at,
                    AIModelRequest.total_tokens,
                    AIModelRequest.provider_cost_usd,
                ).where(*text_filters)
            ).all()
            image_rows = session.execute(
                select(
                    AIImageRequest.created_at,
                    AIImageRequest.image_count,
                    AIImageRequest.provider_cost_usd,
                ).where(*image_filters)
            ).all()
            mineru_rows = session.execute(
                select(
                    MinerUUsageEvent.created_at,
                    MinerUUsageEvent.billable_pages,
                    MinerUUsageEvent.provider_cost_usd,
                ).where(*mineru_filters)
            ).all()

        for created_at, tokens, cost in text_rows:
            bucket = buckets.get(created_at.date().isoformat())
            if bucket is not None:
                bucket["request_count"] += 1
                bucket["total_tokens"] += int(tokens or 0)
                bucket["estimated_cost_usd"] += Decimal(str(cost or 0))
        for created_at, count, cost in image_rows:
            bucket = buckets.get(created_at.date().isoformat())
            if bucket is not None:
                bucket["image_count"] += int(count or 0)
                bucket["estimated_cost_usd"] += Decimal(str(cost or 0))
        for created_at, pages, cost in mineru_rows:
            bucket = buckets.get(created_at.date().isoformat())
            if bucket is not None:
                bucket["mineru_pages"] += int(pages or 0)
                bucket["estimated_cost_usd"] += Decimal(str(cost or 0))

        items = []
        for bucket in buckets.values():
            items.append(
                {
                    **bucket,
                    "estimated_cost_usd": format(
                        bucket["estimated_cost_usd"].quantize(Decimal("0.00000001")),
                        "f",
                    ),
                }
            )
        return {
            "days": safe_days,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "items": items,
        }
