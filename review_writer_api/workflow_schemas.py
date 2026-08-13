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


class MatrixRowUpdateRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    main_content: StrictStr | None = Field(default=None, max_length=2_000_000)
    most_relevant_figure: dict[str, Any] | None = None
    mark_complete: StrictBool = False


class OutlineSaveRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    outline_style: StrictStr = Field(min_length=1, max_length=160)
    outline_md: StrictStr | None = Field(default=None, max_length=250_000)


class ReferenceOutlineUploadRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    filename: StrictStr = Field(min_length=1, max_length=255)
    content_base64: StrictStr = Field(min_length=1, max_length=42_000_000)


class BlueprintGenerateRequest(BaseModel):
    revision: StrictInt = Field(ge=0)


class BlueprintConfirmRequest(BaseModel):
    revision: StrictInt = Field(ge=0)


class SectionsGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionsConfirmRequest(BaseModel):
    revision: StrictInt = Field(ge=0)


class FigureReviewSelectionRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    candidate_index: StrictInt = Field(ge=0)
    review_note: StrictStr = Field(default="", max_length=10_000)


class FigureReviewConfirmRequest(BaseModel):
    revision: StrictInt = Field(ge=0)


class FigureRedrawRequest(BaseModel):
    figure_ids: list[StrictStr] = Field(default_factory=list, max_length=500)
    figure_type: StrictStr = Field(default="auto", max_length=64)
    retry_of_job_id: StrictStr | None = Field(default=None, max_length=64)


class FigureConfirmRequest(BaseModel):
    revision: StrictInt = Field(ge=0)


class FigureFullSvgRequest(BaseModel):
    base_mode: StrictStr = Field(default="source", pattern="^(source|redrawn)$")


class FigureManualEditRequest(BaseModel):
    image_png_data_url: StrictStr = Field(min_length=32, max_length=36_000_000)
    operations: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    base_mode: StrictStr = Field(default="source", pattern="^(source|redrawn)$")
    editable_svg: StrictStr = Field(default="", max_length=26_500_000)
    full_vector_svg: StrictStr = Field(default="", max_length=26_500_000)
