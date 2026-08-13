"""Versioned public API schemas."""

from __future__ import annotations

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


class ProviderSettingsResponse(BaseModel):
    provider_kind: str
    base_url: str
    model_name: str
    wire_api: str
    api_key_configured: bool
    api_key_hint: str
    enabled: bool


class ProviderSettingsListResponse(BaseModel):
    items: list[ProviderSettingsResponse]


class ProviderSettingsUpdateRequest(BaseModel):
    base_url: str = Field(default="", max_length=1024)
    model_name: str = Field(default="", max_length=255)
    wire_api: str = Field(default="", max_length=64)
    api_key: str | None = Field(default=None, max_length=16_384)
    enabled: bool = True
