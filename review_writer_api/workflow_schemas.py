"""Typed request contracts for the native workflow API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


class WorkflowRequest(BaseModel):
    """Preserve supported provider options while validating core workflow fields."""

    model_config = ConfigDict(extra="allow")


class LiteratureSearchRequest(WorkflowRequest):
    topic: StrictStr = Field(min_length=3, max_length=10_000)

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 3:
            raise ValueError("Enter a more specific literature topic.")
        return normalized


class LiteratureDownloadRequest(WorkflowRequest):
    candidates: list[dict[str, Any]] = Field(min_length=1, max_length=1_000)


class DiscoverySearchRequest(WorkflowRequest):
    topic: StrictStr = Field(min_length=3, max_length=10_000)
    keywords: StrictStr = Field(default="", max_length=10_000)
    web_search: StrictBool = False

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 3:
            raise ValueError("Enter a more specific review topic.")
        return normalized


class DiscoveryReviewSaveRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    results: list[dict[str, Any]]


class DiscoverySelectionRequest(BaseModel):
    selected: StrictBool


class DiscoveryTopSelectionRequest(BaseModel):
    count: StrictInt = Field(ge=1)


class DiscoveryConfirmRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
