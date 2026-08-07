"""Shared figure-type routing for Stage 7 chemistry redraws."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from PIL import Image


FIGURE_TYPE_AUTO = "auto"
FIGURE_TYPE_MECHANISM = "mechanism-cycle"
FIGURE_TYPE_SIMPLE = "simple-scheme"
FIGURE_TYPE_SCOPE = "reaction-scope"
FIGURE_TYPE_MULTIPANEL = "complex-multipanel"
FIGURE_TYPE_LOW_RESOLUTION = "low-resolution"
FIGURE_TYPE_COLORED = "colored-chemistry"
FIGURE_TYPE_TABLE = "data-table"
FIGURE_TYPE_PLOT = "scientific-plot"
FIGURE_TYPE_GENERAL = "general-scientific"

FIGURE_TYPE_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "value": FIGURE_TYPE_AUTO,
        "label": "自动识别",
        "description": "综合候选说明、源图尺寸、颜色和闭环布局自动选择处理方式。",
    },
    {
        "value": FIGURE_TYPE_MECHANISM,
        "label": "化学机理 / 催化循环",
        "description": "只改变流程箭头形状，锁定分子、化学键、文字和流程连接关系。",
    },
    {
        "value": FIGURE_TYPE_SIMPLE,
        "label": "简单反应式",
        "description": "统一线条、背景和清晰度，不改变反应物、产物、条件或箭头。",
    },
    {
        "value": FIGURE_TYPE_SCOPE,
        "label": "底物范围 / 反应范围",
        "description": "逐格锁定结构、编号和收率，仅做颜色、背景和清晰度增强。",
    },
    {
        "value": FIGURE_TYPE_MULTIPANEL,
        "label": "复杂多面板化学图",
        "description": "锁定面板顺序、边界和跨面板流程，只做受约束的视觉增强。",
    },
    {
        "value": FIGURE_TYPE_LOW_RESOLUTION,
        "label": "低清 / 细线化学图",
        "description": "放大并强化已有线条，禁止推断或补造无法辨认的结构。",
    },
    {
        "value": FIGURE_TYPE_COLORED,
        "label": "彩色化学结构图",
        "description": "去除非语义填充、保留轮廓和符号，并增强化学线条。",
    },
    {
        "value": FIGURE_TYPE_TABLE,
        "label": "数据表格",
        "description": "锁定全部单元格、数值、单位和脚注，只增强排版清晰度。",
    },
    {
        "value": FIGURE_TYPE_PLOT,
        "label": "曲线 / 科学图表",
        "description": "锁定数据曲线、坐标、刻度和图例，只增强可读性。",
    },
    {
        "value": FIGURE_TYPE_GENERAL,
        "label": "其他科学图",
        "description": "保留全部科学内容和布局，仅做保守的颜色与清晰度调整。",
    },
)

FIGURE_TYPE_VALUES = {item["value"] for item in FIGURE_TYPE_OPTIONS}
HIGH_RISK_FIGURE_TYPES = {
    FIGURE_TYPE_MECHANISM,
    FIGURE_TYPE_SCOPE,
    FIGURE_TYPE_MULTIPANEL,
    FIGURE_TYPE_LOW_RESOLUTION,
    FIGURE_TYPE_COLORED,
    FIGURE_TYPE_TABLE,
    FIGURE_TYPE_PLOT,
    FIGURE_TYPE_GENERAL,
}

_MECHANISM_PATTERN = re.compile(
    r"\b(?:mechanism|mechanistic|plausible\s+mechanism|proposed\s+mechanisms?|"
    r"reaction\s+mechanisms?|catalytic\s+cycles?|photocatalytic\s+cycles?)\b|"
    r"反应机理|机理图|催化循环|光催化循环",
    re.IGNORECASE,
)
_SCOPE_PATTERN = re.compile(
    r"\b(?:reaction\s+scope|substrate\s+scope|scope\s+(?:summary|for|of)|examples?)\b|"
    r"反应范围|底物范围|反应底物",
    re.IGNORECASE,
)
_MULTIPANEL_PATTERN = re.compile(
    r"\b(?:strateg(?:y|ies)|background|overview|comparison|rearrangement|applications?|"
    r"protocols?|routes?|methodology|kinetic\s+investigations?|total\s+synthesis|"
    r"multi[- ]?panel)\b|策略|背景|概览|对比|重排|应用|路线|多面板",
    re.IGNORECASE,
)
_TABLE_PATTERN = re.compile(r"\btable\s*\d*\b|表\s*\d*", re.IGNORECASE)
_PLOT_PATTERN = re.compile(
    r"\b(?:plot|spectrum|spectra|chromatogram|kinetic\s+profile|time\s+course|"
    r"stern[- ]volmer|voltammetr|xrd|nmr|uv[- ]?vis)\b|曲线|光谱|色谱|动力学",
    re.IGNORECASE,
)
_SCHEME_PATTERN = re.compile(r"\b(?:scheme|reaction|synthesis|transformation)\b|反应式|合成", re.IGNORECASE)


def normalize_figure_type(value: str | None, *, allow_auto: bool = True) -> str:
    normalized = str(value or FIGURE_TYPE_AUTO).strip().lower().replace("_", "-")
    aliases = {
        "mechanism": FIGURE_TYPE_MECHANISM,
        "catalytic-cycle": FIGURE_TYPE_MECHANISM,
        "scope": FIGURE_TYPE_SCOPE,
        "multipanel": FIGURE_TYPE_MULTIPANEL,
        "lowres": FIGURE_TYPE_LOW_RESOLUTION,
        "low-resolution-scheme": FIGURE_TYPE_LOW_RESOLUTION,
        "color": FIGURE_TYPE_COLORED,
        "colored": FIGURE_TYPE_COLORED,
        "table": FIGURE_TYPE_TABLE,
        "plot": FIGURE_TYPE_PLOT,
        "general": FIGURE_TYPE_GENERAL,
        "standard": FIGURE_TYPE_SIMPLE,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in FIGURE_TYPE_VALUES or (normalized == FIGURE_TYPE_AUTO and not allow_auto):
        raise ValueError(f"Unsupported figure type: {value}")
    return normalized


def _candidate_text(candidate: dict[str, Any], fields: tuple[str, ...]) -> str:
    return " ".join(str(candidate.get(field) or "") for field in fields)


def _image_features(source_path: Path | None) -> dict[str, Any]:
    features: dict[str, Any] = {
        "available": False,
        "width": 0,
        "height": 0,
        "min_side": 0,
        "aspect_ratio": 0.0,
        "chromatic_pixel_ratio": 0.0,
        "cycle_layout_score": 0.0,
        "cycle_layout_detected": False,
    }
    if source_path is None or not Path(source_path).is_file():
        return features
    try:
        with Image.open(source_path) as source:
            rgb = source.convert("RGB")
            width, height = rgb.size
            features.update(
                available=True,
                width=width,
                height=height,
                min_side=min(width, height),
                aspect_ratio=width / max(height, 1),
            )
            sampled = rgb.copy()
            sampled.thumbnail((160, 160), Image.Resampling.BILINEAR)
            pixel_reader = getattr(sampled, "get_flattened_data", sampled.getdata)
            pixels = list(pixel_reader())
            chromatic = sum(
                1
                for red, green, blue in pixels
                if max(red, green, blue) - min(red, green, blue) >= 45
                and min(red, green, blue) <= 225
            )
            features["chromatic_pixel_ratio"] = chromatic / max(len(pixels), 1)

            # Catalytic cycles have source ink distributed around a relatively
            # empty centre. Preserve aspect ratio while sampling; stretching a
            # wide simple scheme to a square creates false circular layouts.
            canvas = Image.new("L", (160, 160), 255)
            gray = rgb.convert("L")
            gray.thumbnail((152, 152), Image.Resampling.BILINEAR)
            offset = ((160 - gray.width) // 2, (160 - gray.height) // 2)
            canvas.paste(gray, offset)
            center_ink = center_total = annulus_ink = annulus_total = 0
            sector_ink = [0, 0, 0, 0]
            sector_total = [0, 0, 0, 0]
            for y in range(160):
                for x in range(160):
                    dx = (x - 79.5) / 80.0
                    dy = (y - 79.5) / 80.0
                    radius = math.sqrt(dx * dx + dy * dy)
                    is_ink = canvas.getpixel((x, y)) < 190
                    if radius < 0.23:
                        center_total += 1
                        center_ink += int(is_ink)
                    if 0.25 < radius < 0.70:
                        annulus_total += 1
                        annulus_ink += int(is_ink)
                        angle = math.atan2(dy, dx)
                        sector = (
                            0
                            if -math.pi / 4 <= angle < math.pi / 4
                            else 1
                            if math.pi / 4 <= angle < 3 * math.pi / 4
                            else 2
                            if angle >= 3 * math.pi / 4 or angle < -3 * math.pi / 4
                            else 3
                        )
                        sector_total[sector] += 1
                        sector_ink[sector] += int(is_ink)
            center_density = center_ink / max(center_total, 1)
            annulus_density = annulus_ink / max(annulus_total, 1)
            sector_density = [
                sector_ink[index] / max(sector_total[index], 1)
                for index in range(4)
            ]
            occupied_sectors = sum(density >= 0.028 for density in sector_density)
            centre_hole = center_density <= min(0.03, annulus_density * 0.55 + 0.003)
            balanced_ring = occupied_sectors == 4 and min(sector_density) >= 0.028
            aspect_ok = 0.70 <= features["aspect_ratio"] <= 1.85
            score = 0.0
            score += 0.35 if centre_hole else 0.0
            score += 0.35 if balanced_ring else occupied_sectors * 0.06
            score += 0.20 if annulus_density >= 0.04 else 0.0
            score += 0.10 if aspect_ok else 0.0
            features.update(
                center_ink_density=round(center_density, 5),
                annulus_ink_density=round(annulus_density, 5),
                annulus_sector_densities=[round(value, 5) for value in sector_density],
                cycle_layout_score=round(score, 3),
                cycle_layout_detected=bool(
                    centre_hole and balanced_ring and annulus_density >= 0.04 and aspect_ok
                ),
            )
    except (OSError, ValueError):
        return features
    return features


def classify_chemical_figure(
    candidate: dict[str, Any],
    source_path: Path | None = None,
    *,
    requested_type: str = FIGURE_TYPE_AUTO,
) -> dict[str, Any]:
    """Classify a figure once for both dashboard routing and the worker script."""
    requested = normalize_figure_type(requested_type)
    features = _image_features(source_path)
    if requested != FIGURE_TYPE_AUTO:
        return {
            "figure_type": requested,
            "selection": "manual",
            "reasons": ["reviewer_selected_figure_type"],
            "features": features,
            "requires_human_approval": requested in HIGH_RISK_FIGURE_TYPES,
        }

    explicit = str(candidate.get("figure_type") or candidate.get("redraw_figure_type") or "").strip()
    if explicit:
        try:
            selected = normalize_figure_type(explicit, allow_auto=False)
            return {
                "figure_type": selected,
                "selection": "candidate-explicit",
                "reasons": ["candidate_explicit_figure_type"],
                "features": features,
                "requires_human_approval": selected in HIGH_RISK_FIGURE_TYPES,
            }
        except ValueError:
            pass
    edit_profile = str(candidate.get("edit_profile") or candidate.get("redraw_profile") or "").strip()
    primary_text = _candidate_text(candidate, ("source_label", "source_caption_text"))
    source_type = str(candidate.get("source_type") or "")
    if edit_profile == "mechanism-arrow-straighten" or _MECHANISM_PATTERN.search(primary_text):
        selected, reasons = FIGURE_TYPE_MECHANISM, ["mechanism_source_caption_or_profile"]
    elif features.get("cycle_layout_detected"):
        selected, reasons = FIGURE_TYPE_MECHANISM, ["visual_closed_cycle_layout"]
    elif source_type.casefold() == "table" or _TABLE_PATTERN.search(primary_text):
        selected, reasons = FIGURE_TYPE_TABLE, ["table_label_or_source_type"]
    elif _SCOPE_PATTERN.search(_candidate_text(candidate, ("source_label", "source_caption_text", "title"))):
        selected, reasons = FIGURE_TYPE_SCOPE, ["scope_caption"]
    elif _PLOT_PATTERN.search(_candidate_text(candidate, ("source_label", "source_caption_text"))):
        selected, reasons = FIGURE_TYPE_PLOT, ["scientific_plot_caption"]
    elif _MULTIPANEL_PATTERN.search(
        _candidate_text(candidate, ("source_label", "source_caption_text", "title"))
    ):
        selected, reasons = FIGURE_TYPE_MULTIPANEL, ["multipanel_caption"]
    elif float(features.get("chromatic_pixel_ratio") or 0.0) >= 0.015:
        selected, reasons = FIGURE_TYPE_COLORED, ["significant_scientific_color"]
    elif features.get("available") and int(features.get("min_side") or 0) < 420:
        selected, reasons = FIGURE_TYPE_LOW_RESOLUTION, ["small_source_canvas"]
    elif _SCHEME_PATTERN.search(primary_text):
        selected, reasons = FIGURE_TYPE_SIMPLE, ["simple_scheme_label"]
    else:
        selected, reasons = FIGURE_TYPE_GENERAL, ["no_specialized_type_indicator"]
    return {
        "figure_type": selected,
        "selection": "automatic",
        "reasons": reasons,
        "features": features,
        "requires_human_approval": selected in HIGH_RISK_FIGURE_TYPES,
    }


def figure_type_options() -> list[dict[str, str]]:
    return [dict(item) for item in FIGURE_TYPE_OPTIONS]
