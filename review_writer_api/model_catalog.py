"""Server-controlled text model tiers and immutable price snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelTier:
    id: str
    model: str
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal


MODEL_TIERS: tuple[ModelTier, ...] = (
    ModelTier(
        id="sol",
        model="gpt-5.6-sol",
        label_zh="Sol · 高质量",
        label_en="Sol · Highest quality",
        description_zh="适合最终定稿和高难度科学推理。",
        description_en="For final drafting and difficult scientific reasoning.",
        input_usd_per_million=Decimal("5.00"),
        cached_input_usd_per_million=Decimal("0.50"),
        output_usd_per_million=Decimal("30.00"),
    ),
    ModelTier(
        id="terra",
        model="gpt-5.6-terra",
        label_zh="Terra · 均衡",
        label_en="Terra · Balanced",
        description_zh="质量、速度和成本均衡，默认推荐。",
        description_en="Balanced quality, speed, and cost; recommended by default.",
        input_usd_per_million=Decimal("2.00"),
        cached_input_usd_per_million=Decimal("0.20"),
        output_usd_per_million=Decimal("12.00"),
    ),
    ModelTier(
        id="luna",
        model="gpt-5.6-luna",
        label_zh="Luna · 经济",
        label_en="Luna · Economy",
        description_zh="适合批量初筛和成本敏感任务。",
        description_en="For high-volume screening and cost-sensitive work.",
        input_usd_per_million=Decimal("0.20"),
        cached_input_usd_per_million=Decimal("0.02"),
        output_usd_per_million=Decimal("1.20"),
    ),
)

DEFAULT_MODEL_TIER = "terra"
MODEL_TIERS_BY_ID = {tier.id: tier for tier in MODEL_TIERS}


def resolve_model_tier(value: str | None) -> ModelTier:
    normalized = str(value or DEFAULT_MODEL_TIER).strip().casefold()
    try:
        return MODEL_TIERS_BY_ID[normalized]
    except KeyError as exc:
        allowed = ", ".join(MODEL_TIERS_BY_ID)
        raise ValueError(f"Unsupported model tier. Choose one of: {allowed}.") from exc
