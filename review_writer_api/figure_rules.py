"""Pure validation and vectorization rules for manuscript figure artifacts."""

from __future__ import annotations

import html
import io
import math
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from PIL import Image


MAX_EDITOR_BYTES = 25 * 1024 * 1024
MAX_VECTOR_DIMENSION = 1600
_SVG_OPENING = re.compile(r"\s*<svg\b([^>]*)>", re.IGNORECASE | re.DOTALL)


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.load()
        return image.size


def png_size(content: bytes) -> tuple[int, int]:
    if not content or len(content) > MAX_EDITOR_BYTES:
        raise ValueError("PNG is empty or larger than the 25 MB editor limit")
    with Image.open(io.BytesIO(content)) as image:
        if image.format != "PNG":
            raise ValueError("Manual figure edits must be encoded as PNG")
        image.load()
        return image.size


def aspect_ratio_integrity(
    source_size: tuple[int, int],
    output_size: tuple[int, int],
    *,
    tolerance: float = 0.015,
) -> dict[str, Any]:
    source_ratio = source_size[0] / max(1, source_size[1])
    output_ratio = output_size[0] / max(1, output_size[1])
    delta = abs(output_ratio - source_ratio) / max(source_ratio, 1e-9)
    return {
        "status": "pass" if delta <= tolerance else "failed",
        "source_size": list(source_size),
        "output_size": list(output_size),
        "source_aspect_ratio": source_ratio,
        "output_aspect_ratio": output_ratio,
        "relative_difference": delta,
        "tolerance": tolerance,
    }


def _svg_attribute(opening: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*(['\"])(.*?)\1",
        opening,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(2).strip() if match else ""


def validate_svg_markup(markup: str, *, require_full_trace: bool = False) -> None:
    encoded = markup.encode("utf-8")
    if not markup.lstrip().startswith("<svg"):
        raise ValueError("Figure editor content must be SVG markup")
    if len(encoded) > MAX_EDITOR_BYTES:
        raise ValueError("SVG is larger than the 25 MB editor limit")
    try:
        root = ElementTree.fromstring(markup)
    except ElementTree.ParseError as exc:
        raise ValueError("Figure editor SVG is not well formed") from exc
    if root.tag.rsplit("}", 1)[-1].casefold() != "svg":
        raise ValueError("Figure editor content must have an SVG root")
    forbidden = {"script", "foreignobject", "iframe", "object", "embed"}
    unsafe_style = re.compile(
        r"(?:javascript\s*:|expression\s*\(|@import\b|url\s*\(\s*['\"]?\s*(?:https?:|//|data:))",
        re.IGNORECASE,
    )
    for node in root.iter():
        local_name = node.tag.rsplit("}", 1)[-1].casefold()
        if local_name in forbidden:
            raise ValueError("Figure editor SVG contains an unsafe element")
        if local_name == "style" and unsafe_style.search(str(node.text or "")):
            raise ValueError("Figure editor SVG contains an unsafe style")
        for raw_name, raw_value in node.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].casefold()
            value = str(raw_value or "").strip().casefold()
            if name.startswith("on"):
                raise ValueError("Figure editor SVG contains an event handler")
            if name in {"href", "src"} and value and not value.startswith("#"):
                raise ValueError("Figure editor SVG contains an external resource")
            if name == "style" and unsafe_style.search(value):
                raise ValueError("Figure editor SVG contains an unsafe style")
    if require_full_trace:
        if "full-image-vector-trace" not in markup:
            raise ValueError("Full SVG must contain the complete vector trace")
        if re.search(r"<image\b", markup, re.IGNORECASE):
            raise ValueError("Full SVG cannot contain an embedded raster image")


def svg_workspace_size(markup: str) -> tuple[int, int]:
    """Return the source-pixel canvas represented by a saved editor SVG."""

    try:
        root = ElementTree.fromstring(markup)
    except ElementTree.ParseError as exc:
        raise ValueError("Figure editor SVG is not well formed") from exc
    if root.tag.rsplit("}", 1)[-1].casefold() != "svg":
        raise ValueError("Figure editor content must have an SVG root")

    def dimension(name: str, fallback: str) -> int:
        raw = str(root.attrib.get(name) or root.attrib.get(fallback) or "").strip()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:px)?", raw, re.IGNORECASE)
        if not match:
            raise ValueError(f"SVG workspace dimension {name} is missing or invalid")
        value = round(float(match.group(1)))
        if value < 1:
            raise ValueError(f"SVG workspace dimension {name} is missing or invalid")
        return value

    return (
        dimension("data-source-width", "data-original-width"),
        dimension("data-source-height", "data-original-height"),
    )


def validated_content_crop(
    svg_markup: str,
    source_size: tuple[int, int],
    submitted_size: tuple[int, int],
) -> dict[str, Any] | None:
    opening = _SVG_OPENING.match(svg_markup)
    if not opening:
        return None
    attributes = opening.group(1)
    if _svg_attribute(attributes, "data-content-crop").casefold() != "true":
        return None
    if _svg_attribute(attributes, "data-crop-unit") != "source-px":
        raise ValueError("SVG content crop uses an unsupported coordinate unit")
    values: dict[str, int] = {}
    for name in (
        "data-source-width",
        "data-source-height",
        "data-crop-x",
        "data-crop-y",
        "data-crop-width",
        "data-crop-height",
        "data-original-width",
        "data-original-height",
    ):
        raw = _svg_attribute(attributes, name)
        if not re.fullmatch(r"\d+", raw):
            raise ValueError(f"SVG content crop attribute {name} is missing or invalid")
        values[name] = int(raw)
    recorded_source = (
        values["data-source-width"],
        values["data-source-height"],
    )
    crop_size = (values["data-crop-width"], values["data-crop-height"])
    if recorded_source != source_size:
        raise ValueError("SVG content crop does not match the selected base image")
    if min(crop_size) < 1 or crop_size != submitted_size:
        raise ValueError("SVG content crop does not match the submitted PNG")
    if (
        values["data-crop-x"] + crop_size[0] > source_size[0]
        or values["data-crop-y"] + crop_size[1] > source_size[1]
    ):
        raise ValueError("SVG content crop extends outside the selected base image")
    if (
        values["data-original-width"],
        values["data-original-height"],
    ) != submitted_size:
        raise ValueError("SVG output dimensions do not match the submitted PNG")
    return {
        "status": "verified",
        "unit": "source-px",
        "x": values["data-crop-x"],
        "y": values["data-crop-y"],
        "width": crop_size[0],
        "height": crop_size[1],
        "source_width": source_size[0],
        "source_height": source_size[1],
    }


def _color(value: tuple[int, int, int]) -> str:
    return f"#{value[0]:02x}{value[1]:02x}{value[2]:02x}"


def build_full_vector_svg(source_path: Path) -> str:
    """Trace visible raster runs into editable SVG paths without chemical OCR."""

    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        rgb = background.convert("RGB")
        original_width, original_height = rgb.size
        scale = min(1.0, MAX_VECTOR_DIMENSION / max(rgb.size))
        if scale < 1:
            rgb = rgb.resize(
                (
                    max(1, round(original_width * scale)),
                    max(1, round(original_height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        width, height = rgb.size
        pixels = rgb.load()
        runs: list[tuple[int, int, int, tuple[int, int, int]]] = []
        parents: list[int] = []

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            if left_root < right_root:
                parents[right_root] = left_root
            else:
                parents[left_root] = right_root

        previous_row: list[int] = []
        for y in range(height):
            x = 0
            current_row: list[int] = []
            while x < width:
                red, green, blue = pixels[x, y]
                if min(red, green, blue) >= 248:
                    x += 1
                    continue
                # Eight levels per channel preserve chemistry colors while keeping
                # the browser DOM small enough for interactive hit testing.
                quantized = (red // 32 * 32, green // 32 * 32, blue // 32 * 32)
                start = x
                x += 1
                while x < width:
                    next_pixel = pixels[x, y]
                    next_quantized = tuple(channel // 32 * 32 for channel in next_pixel)
                    if min(next_pixel) >= 248 or next_quantized != quantized:
                        break
                    x += 1
                index = len(runs)
                runs.append((y, start, x, quantized))
                parents.append(index)
                if current_row and runs[current_row[-1]][2] >= start - 1:
                    union(current_row[-1], index)
                current_row.append(index)
            previous_cursor = 0
            for index in current_row:
                _run_y, start, end, _run_color = runs[index]
                while (
                    previous_cursor < len(previous_row)
                    and runs[previous_row[previous_cursor]][2] < start - 1
                ):
                    previous_cursor += 1
                candidate_cursor = previous_cursor
                while candidate_cursor < len(previous_row):
                    previous_index = previous_row[candidate_cursor]
                    _previous_y, previous_start, _previous_end, _previous_color = runs[
                        previous_index
                    ]
                    if previous_start > end + 1:
                        break
                    union(index, previous_index)
                    candidate_cursor += 1
            previous_row = current_row
        components: dict[int, dict[tuple[int, int, int], list[str]]] = {}
        component_order: list[int] = []
        for index, (y, start, end, quantized) in enumerate(runs):
            root = find(index)
            if root not in components:
                components[root] = {}
                component_order.append(root)
            components[root].setdefault(quantized, []).append(
                f"M{start} {y}h{end - start}v1h-{end - start}z"
            )
        trace_objects: list[str] = []
        for object_index, root in enumerate(component_order):
            paths = "".join(
                f'<path d="{"".join(commands)}" fill="{_color(color)}"/>'
                for color, commands in components[root].items()
            )
            trace_objects.append(
                f'<g data-trace-object-id="trace-{object_index}" '
                f'data-vector-kind="base-trace-object">{paths}</g>'
            )
        title = html.escape("Full-image chemistry figure vector trace", quote=False)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" data-original-width="{original_width}" '
            f'data-original-height="{original_height}" data-source-width="{original_width}" '
            f'data-source-height="{original_height}" data-content-crop="false">'
            f"<title>{title}</title><rect width=\"{width}\" height=\"{height}\" fill=\"#fff\"/>"
            f'<g id="full-image-vector-trace">{"".join(trace_objects)}</g>'
            '<g id="editable-arrow-overlays"></g></svg>'
        )
    if len(svg.encode("utf-8")) > MAX_EDITOR_BYTES:
        raise ValueError("Full-image vector SVG is larger than the 25 MB editor limit")
    return svg


def append_operation_overlays(
    svg_markup: str, operations: list[dict[str, Any]]
) -> str:
    overlays: list[str] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        kind = str(operation.get("type") or "")
        points = operation.get("points") or []
        if kind not in {"erase", "arrow", "line"} or not isinstance(points, list):
            continue
        parsed: list[tuple[float, float]] = []
        for point in points:
            if not isinstance(point, dict):
                parsed = []
                break
            parsed.append((finite_number(point.get("x") or 0), finite_number(point.get("y") or 0)))
        if len(parsed) < 2:
            continue
        coordinates = " ".join(f"{x:g},{y:g}" for x, y in parsed)
        width = max(1.0, min(40.0, finite_number(operation.get("width") or 2)))
        color = str(operation.get("color") or "#111111")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#111111"
        stroke = "#ffffff" if kind == "erase" else color
        marker = ' marker-end="url(#manualArrowHead)"' if kind == "arrow" else ""
        overlays.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{stroke}" '
            f'stroke-width="{width:g}" stroke-linecap="round" stroke-linejoin="round"{marker}/>'
        )
    if not overlays:
        return svg_markup
    definition = (
        '<defs><marker id="manualArrowHead" markerWidth="8" markerHeight="8" '
        'refX="7" refY="4" orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L8,4 L0,8 z" fill="#111111"/></marker></defs>'
    )
    return svg_markup.replace(
        '<g id="editable-arrow-overlays"></g>',
        f'{definition}<g id="editable-arrow-overlays">{"".join(overlays)}</g>',
        1,
    )


def canvas_policy_matches(row: dict[str, Any], integrity: dict[str, Any]) -> bool:
    if integrity.get("status") == "pass":
        return True
    crop = (row.get("manual_edit") or {}).get("canvas_crop") or {}
    if (
        crop.get("status") == "verified"
        and list(integrity.get("output_size") or [])
        == [int(crop.get("width") or 0), int(crop.get("height") or 0)]
    ):
        return True
    approval = row.get("human_approval") or {}
    return bool(
        row.get("render_mode") == "manual-arrow-edit"
        and approval.get("status") == "approved"
        and approval.get("manual_canvas_override") is True
        and approval.get("source_canvas_size") == integrity.get("source_size")
        and approval.get("output_canvas_size") == integrity.get("output_size")
    )


def finite_number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1_000_000:
        raise ValueError("SVG coordinate is invalid")
    return number
