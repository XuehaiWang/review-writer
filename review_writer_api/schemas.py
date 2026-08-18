"""Versioned public API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .auth import PASSWORD_MIN_LENGTH

class HealthResponse(BaseModel):
    status: str
    api_version: str
    deployment_mode: str


class BrowserAuthConfigResponse(BaseModel):
    enabled: bool
    registration_enabled: bool
    password_min_length: int


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=256)
    display_name: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class PrincipalResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    roles: list[str]
    permissions: list[str]


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: str
    slug: str
    owner_user_id: str
    topic: str
    taxonomy_profile: str
    model_tier: str
    discovery_status: str
    current_stage: str
    completed_stages: tuple[str, ...]


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    count: int


class ProjectCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=96)
    topic: str = Field(default="", max_length=10_000)
    taxonomy_profile: str = Field(default="chemistry_general", min_length=1, max_length=96)
    model_tier: str = Field(default="terra", min_length=1, max_length=32)


class ProjectModelTierUpdateRequest(BaseModel):
    model_tier: str = Field(min_length=1, max_length=32)


class ModelTierResponse(BaseModel):
    id: str
    model: str
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    input_usd_per_million: str
    cached_input_usd_per_million: str
    output_usd_per_million: str


class ModelCatalogResponse(BaseModel):
    items: list[ModelTierResponse]
    default_tier: str


class ModelGatewayRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=128)
    stage: str = Field(default="", max_length=96)
    prompt: str = Field(min_length=1, max_length=4_000_000)
    response_format: str = Field(default="json", pattern="^(json|text)$")


class ModelGatewayResponse(BaseModel):
    request_id: str
    provider_request_id: str
    model_tier: str
    model: str
    output_text: str
    usage: dict[str, int]
    cost_usd: str
    cached: bool


class ImageGatewayInput(BaseModel):
    mime_type: str = Field(default="image/png", min_length=1, max_length=100)
    data_base64: str = Field(min_length=1, max_length=40_000_000)


class ImageGatewayRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=128)
    stage: str = Field(default="", max_length=96)
    operation: str = Field(default="edit", pattern="^(edit|generate)$")
    prompt: str = Field(min_length=1, max_length=100_000)
    images: list[ImageGatewayInput] = Field(default_factory=list, max_length=8)
    quality: str = Field(default="high", max_length=32)
    background: str = Field(default="opaque", max_length=32)
    output_format: str = Field(default="png", max_length=16)
    size: str = Field(default="", max_length=32)


class ImageGatewayResponse(BaseModel):
    request_id: str
    provider_request_id: str
    model: str
    image_base64: str
    image_mime_type: str
    image_count: int
    provider_attempt_count: int
    cost_usd: str
    cached: bool


class UsageSummaryResponse(BaseModel):
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    image_request_count: int
    image_count: int
    estimated_text_cost_usd: str
    estimated_image_cost_usd: str
    mineru_request_count: int
    mineru_billable_pages: int
    mineru_cache_hit_count: int
    estimated_mineru_cost_usd: str
    estimated_cost_usd: str
    billing_mode: str


class UsageTimelineItemResponse(BaseModel):
    date: str
    request_count: int
    total_tokens: int
    image_count: int
    mineru_pages: int
    estimated_cost_usd: str


class UsageTimelineResponse(BaseModel):
    days: int
    start_date: str
    end_date: str
    items: list[UsageTimelineItemResponse]


class ProviderSettingsResponse(BaseModel):
    provider_kind: str
    base_url: str
    model_name: str
    wire_api: str
    api_key_configured: bool
    api_key_hint: str
    enabled: bool
    source: str = "server"
    updated_at: datetime | None = None


class ProviderSettingsListResponse(BaseModel):
    items: list[ProviderSettingsResponse]


class AdminProviderSettingsUpdateRequest(BaseModel):
    base_url: str = Field(default="", max_length=1024)
    model_name: str = Field(default="", max_length=255)
    wire_api: str = Field(default="", max_length=64)
    api_key: str | None = Field(default=None, max_length=10_000)
    enabled: bool = True


class AdminProviderTestResponse(BaseModel):
    provider_kind: str
    ok: bool
    status_code: int
    latency_ms: int
    message: str


class AdminProviderAuditResponse(BaseModel):
    id: str
    actor_email: str
    provider_kind: str
    action: str
    summary: str
    created_at: datetime


class AdminProviderAuditListResponse(BaseModel):
    items: list[AdminProviderAuditResponse]


class JobResponse(BaseModel):
    id: str
    project_id: str | None
    scope: str
    job_type: str
    status: str
    result: dict
    progress_current: int
    progress_total: int
    cancellation_requested: bool
    error_code: str
    error_message: str
    retry_of_job_id: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    available_actions: list[str]
