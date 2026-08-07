#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from collections import deque
import io
import json
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


_BOOTSTRAP_ROOT = next(
    (p for p in Path(__file__).resolve().parents if (p / "review_writer_core").is_dir()),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.providers import (  # noqa: E402
    DEFAULT_IMAGE_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    normalize_wire_api as _shared_normalize_wire_api,
    openai_endpoint as _shared_openai_endpoint,
    resolve_api_key as _shared_resolve_api_key,
)
from review_writer_core.figure_redraw_routing import (  # noqa: E402
    FIGURE_TYPE_AUTO,
    FIGURE_TYPE_COLORED,
    FIGURE_TYPE_LOW_RESOLUTION,
    FIGURE_TYPE_MECHANISM,
    FIGURE_TYPE_MULTIPANEL,
    FIGURE_TYPE_PLOT,
    FIGURE_TYPE_SCOPE,
    FIGURE_TYPE_TABLE,
    classify_chemical_figure,
    normalize_figure_type,
)


SOURCE_FAITHFUL_SCALE_FACTOR = 4
SOURCE_FAITHFUL_RENDER_MODES = {
    "source-faithful-bw",
    "source-faithful-color",
    "source-faithful-outline-color",
}
SOURCE_FAITHFUL_DARK_INK_THRESHOLD = 180
BRIGHT_COLOR_FILL_SPREAD = 50
BRIGHT_COLOR_FILL_BRIGHTNESS = 135
# Only pixels with a solid 3x3 bright-colour neighbourhood are fill interiors.
# Chemical outlines, aromatic bonds, labels, and substituent strokes are
# usually thinner than this and must survive even when they touch a filled
# ring.  Reconstructed bright fill pixels must be whitened, never converted
# into synthetic black boundaries around every internal bond or glyph.
BRIGHT_COLOR_FILL_MIN_REGION_SIZE = 3
BRIGHT_COLOR_FILL_MAX_LOCAL_CHANNEL_RANGE = 80
MAX_ISOLATED_CHROMATIC_NOISE_PIXELS = 5
CONTENT_INK_THRESHOLD = 192
CONTENT_MATCH_RADIUS = 3
MAX_RESTORED_COMPONENT_PIXELS = 1024
STRUCTURE_MATCH_RADIUS = 4
MAX_UNMATCHED_OUTPUT_INK_RATIO = 0.04
MIN_UNMATCHED_OUTPUT_INK_PIXELS = 64
MAX_UNMATCHED_SOURCE_INK_RATIO = 0.03
MIN_UNMATCHED_SOURCE_INK_PIXELS = 64
DEFAULT_STYLE_NAME = "organic-review-structure-locked-v2"
MECHANISM_ARROW_STRAIGHTEN_PROFILE = "mechanism-arrow-straighten"
OCR_PAGE_SEGMENTATION_MODE = "3"
OCR_ASSUMED_DPI = "300"
MECHANISM_MAX_UNMATCHED_SOURCE_INK_RATIO = 0.28
MECHANISM_MAX_UNMATCHED_OUTPUT_INK_RATIO = 0.14
MECHANISM_MIN_OUTPUT_TO_SOURCE_INK_RATIO = 0.75
MECHANISM_MIN_CHANGED_INK_RATIO = 0.002
MECHANISM_MAX_EDIT_ATTEMPTS = 2
COLORED_CLEANUP_MAX_EDIT_ATTEMPTS = 2
COLORED_MAX_REMAINING_CHROMATIC_RATIO = 0.012
COLORED_MAX_SOURCE_CHROMATIC_RETENTION = 0.45
CHAT_COMPLETION_NO_IMAGE_ATTEMPTS = 3
AI_EDIT_MAX_INPUT_SIDE = 2048
ASPECT_PADDING_GUARD_RATIO = 0.004
ASPECT_PADDING_SAFE_MARGIN_RATIO = 0.012
ASPECT_PADDING_MIN_INK_PIXELS = 24
ASPECT_PADDING_MIN_INK_RATIO = 0.002
_DENSE_SCOPE_FIGURE_PATTERN = re.compile(
    r"\b(?:reaction\s+scope|substrate\s+scope|scope\s+(?:summary|for|of)|examples?)\b|"
    r"反应范围|底物范围|反应底物",
    re.IGNORECASE,
)
_COMPLEX_MULTIPANEL_FIGURE_PATTERN = re.compile(
    r"\b(?:strateg(?:y|ies)|background|overview|comparison|rearrangement|applications?|"
    r"protocols?|routes?|methodology|stud(?:y|ies)|kinetic\s+investigations?|"
    r"(?:proposed\s+)?catalytic\s+cycles?|total\s+synthesis)\b|"
    r"策略|背景|概览|对比|重排|应用|路线",
    re.IGNORECASE,
)
_HOLLOW_COLOR_FILL_PATTERN = re.compile(
    r"\b(?:strateg(?:y|ies)|traditional\s+protocols?|research\s+background)\b",
    re.IGNORECASE,
)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_dotenv(review_root: Path) -> None:
    """Load project-local API settings without overriding the process environment."""
    path = review_root / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("'\""))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_space(text).lower()).strip()


def is_dense_scope_figure(figure: dict[str, Any]) -> bool:
    """Return true for multi-product scope figures that cannot be safely regenerated.

    This guard deliberately lives in the renderer, rather than only in the
    dashboard, so stale web workers, batch jobs, and direct CLI calls cannot
    accidentally send a reaction-scope grid through a generative edit model.
    """
    fields = (
        "source_label",
        "source_caption_text",
        "title",
        "section_heading",
        "recommended_action",
    )
    text = " ".join(str(figure.get(field) or "") for field in fields)
    return bool(_DENSE_SCOPE_FIGURE_PATTERN.search(text))


def is_complex_multipanel_figure(figure: dict[str, Any]) -> bool:
    """Identify multi-panel overview figures whose source pixels must be retained.

    These figures combine multiple independent reaction schemes, annotations,
    and often color-coded scientific meaning.  A model edit can make a small
    local chemistry error while still passing a global ink-geometry threshold.
    """
    fields = (
        "source_label",
        "source_caption_text",
        "title",
        "what_it_shows",
        "section_heading",
    )
    text = " ".join(str(figure.get(field) or "") for field in fields)
    return bool(_COMPLEX_MULTIPANEL_FIGURE_PATTERN.search(text))


def needs_hollow_color_fills(figure: dict[str, Any]) -> bool:
    """Keep colored outlines but remove non-semantic pastel fills in strategy figures."""
    fields = ("source_label", "source_caption_text", "title", "what_it_shows")
    return bool(_HOLLOW_COLOR_FILL_PATTERN.search(" ".join(str(figure.get(field) or "") for field in fields)))


def is_low_resolution_or_thin_scheme(source_path: Path) -> bool:
    """Avoid generative reconstruction when small source strokes cannot be recovered safely."""
    with Image.open(source_path) as source:
        width, height = source.size
    return min(width, height) < 360


def is_tall_multistep_source(source_path: Path) -> bool:
    """Protect portrait multi-panel reaction summaries from generative canvas cropping."""
    with Image.open(source_path) as source:
        width, height = source.size
    return height >= 700 and height / max(width, 1) >= 1.35


def resolve_tesseract_command(review_root: Path, configured_path: str) -> str:
    """Find an explicitly configured, system, or project-local Tesseract executable."""
    if configured_path.strip():
        return configured_path.strip()
    environment_path = os.environ.get("TESSERACT_CMD", "").strip()
    if environment_path:
        return environment_path
    system_path = shutil.which("tesseract")
    if system_path:
        return system_path
    project_runtime = review_root / ".tmp" / "tesseract" / "runtime" / "tesseract.exe"
    if project_runtime.exists():
        return str(project_runtime)
    return "tesseract"


def extract_ocr_text(
    image_path: Path,
    language: str,
    runner: Any = subprocess.run,
    command: str = "tesseract",
) -> dict[str, str]:
    """Read visible text with an optional local Tesseract executable."""
    command_args = [
        command,
        str(image_path),
        "stdout",
        "-l",
        language,
        "--psm",
        OCR_PAGE_SEGMENTATION_MODE,
        "-c",
        f"user_defined_dpi={OCR_ASSUMED_DPI}",
    ]
    try:
        result = runner(command_args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    except FileNotFoundError:
        return {"status": "unavailable", "text": ""}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "text": ""}
    except OSError as exc:
        return {"status": f"failed: {type(exc).__name__}", "text": ""}
    if result.returncode != 0:
        return {"status": "failed", "text": ""}
    return {"status": "ok", "text": str(result.stdout or "").strip()}


def ocr_tokens(text: str) -> set[str]:
    return {
        match.group(0).upper()
        for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9().+/%-]*", str(text or ""))
    }


def compare_ocr_text(source_text: str, output_text: str) -> dict[str, Any]:
    """Report source OCR tokens absent from an AI-edited output image."""
    source_tokens = ocr_tokens(source_text)
    output_tokens = ocr_tokens(output_text)
    missing_tokens = sorted(source_tokens - output_tokens)
    if not source_tokens:
        status = "not_available"
    elif missing_tokens:
        status = "needs_human_check"
    else:
        status = "pass"
    return {
        "status": status,
        "source_tokens": sorted(source_tokens),
        "output_tokens": sorted(output_tokens),
        "missing_tokens": missing_tokens,
    }


def ocr_region_boxes(image_path: Path, language: str, runner: Any = subprocess.run, command: str = "tesseract") -> dict[str, Any]:
    """Read OCR text and bounding boxes so source glyphs can be protected."""
    try:
        result = runner(
            [
                command,
                str(image_path),
                "stdout",
                "-l",
                language,
                "--psm",
                OCR_PAGE_SEGMENTATION_MODE,
                "-c",
                f"user_defined_dpi={OCR_ASSUMED_DPI}",
                "tsv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "unavailable" if isinstance(exc, FileNotFoundError) else "failed", "text": "", "boxes": []}
    if result.returncode != 0:
        return {"status": "failed", "text": "", "boxes": []}
    lines = str(result.stdout or "").splitlines()
    if not lines:
        return {"status": "ok", "text": "", "boxes": []}
    header = lines[0].split("\t")
    boxes: list[dict[str, Any]] = []
    for line in lines[1:]:
        values = line.split("\t")
        if len(values) != len(header):
            continue
        row = dict(zip(header, values))
        text = row.get("text", "").strip()
        try:
            left, top, width, height = (int(row[key]) for key in ("left", "top", "width", "height"))
        except (KeyError, ValueError):
            continue
        if text and width > 0 and height > 0:
            boxes.append({"left": left, "top": top, "width": width, "height": height, "text": text})
    return {"status": "ok", "text": " ".join(box["text"] for box in boxes), "boxes": boxes}


def expanded_box(box: dict[str, Any], image_size: tuple[int, int], padding: int = 3) -> tuple[int, int, int, int]:
    width, height = image_size
    left = max(0, int(box["left"]) - padding)
    top = max(0, int(box["top"]) - padding)
    right = min(width, int(box["left"]) + int(box["width"]) + padding)
    bottom = min(height, int(box["top"]) + int(box["height"]) + padding)
    return left, top, right, bottom


def mask_ocr_regions(source_path: Path, output_path: Path, boxes: list[dict[str, Any]]) -> None:
    """Erase source text before image generation so the model cannot redraw it."""
    with Image.open(source_path) as source:
        image = source.convert("RGB")
        draw = ImageDraw.Draw(image)
        for box in boxes:
            draw.rectangle(expanded_box(box, image.size), fill="white")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, format="PNG")


def restore_source_text_regions(source_path: Path, output_path: Path, boxes: list[dict[str, Any]]) -> None:
    """Clear generated text areas and paste source glyph pixels once, preventing overlap."""
    with Image.open(source_path) as source, Image.open(output_path) as generated:
        source_image = source.convert("RGB")
        output_image = generated.convert("RGB")
        if output_image.size != source_image.size:
            output_image = output_image.resize(source_image.size, Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(output_image)
        for box in boxes:
            region = expanded_box(box, source_image.size)
            draw.rectangle(region, fill="white")
            output_image.paste(source_image.crop(region), region)
        output_image.save(output_path, format="PNG")


def select_requested_figures(figures: list[dict[str, Any]], figure_id: str = "", paper_id: str = "") -> list[dict[str, Any]]:
    """Select an exact figure first; paper selection remains for legacy batch runs."""
    if figure_id:
        return [figure for figure in figures if str(figure.get("figure_id") or "") == figure_id]
    if paper_id:
        return [figure for figure in figures if str(figure.get("paper_id") or "") == paper_id]
    return figures


def ink_mask(image: Image.Image) -> Image.Image:
    """Return an L mask whose white pixels represent source/output ink."""
    return image.convert("L").point(lambda value: 255 if value < CONTENT_INK_THRESHOLD else 0, mode="L")


def enclosed_white_mask(ink: Image.Image) -> bytes:
    """Return white output areas enclosed by ink; these are valid hollow-fill interiors."""
    width, height = ink.size
    raw = ink.tobytes()
    exterior = bytearray(width * height)
    queue: deque[int] = deque()

    def add(index: int) -> None:
        if raw[index] == 0 and not exterior[index]:
            exterior[index] = 1
            queue.append(index)

    for x in range(width):
        add(x)
        add((height - 1) * width + x)
    for y in range(height):
        add(y * width)
        add(y * width + width - 1)
    while queue:
        index = queue.popleft()
        x, y = index % width, index // width
        if x:
            add(index - 1)
        if x + 1 < width:
            add(index + 1)
        if y:
            add(index - width)
        if y + 1 < height:
            add(index + width)
    return bytes(255 if raw[index] == 0 and not exterior[index] else 0 for index in range(width * height))


def missing_ink_components(mask_bytes: bytes, size: tuple[int, int]) -> list[list[int]]:
    """Return connected missing-ink components so small lost symbols can be restored exactly."""
    width, height = size
    seen = bytearray(width * height)
    components: list[list[int]] = []
    for start, value in enumerate(mask_bytes):
        if value == 0 or seen[start]:
            continue
        component: list[int] = []
        queue: deque[int] = deque([start])
        seen[start] = 1
        while queue:
            index = queue.popleft()
            component.append(index)
            x, y = index % width, index // width
            neighbors = []
            if x:
                neighbors.append(index - 1)
            if x + 1 < width:
                neighbors.append(index + 1)
            if y:
                neighbors.append(index - width)
            if y + 1 < height:
                neighbors.append(index + width)
            for neighbor in neighbors:
                if mask_bytes[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    queue.append(neighbor)
        components.append(component)
    return components


def restore_missing_source_ink(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Restore missed small source marks and reject large content loss outside hollow interiors."""
    with Image.open(source_path) as source, Image.open(output_path) as output:
        source_image = source.convert("RGB")
        output_image = output.convert("RGB")
        if output_image.size != source_image.size:
            output_image = output_image.resize(source_image.size, Image.Resampling.LANCZOS)
        source_ink = ink_mask(source_image).tobytes()
        output_ink_image = ink_mask(output_image)
        output_ink = output_ink_image.tobytes()
        expanded_output = output_ink_image.filter(ImageFilter.MaxFilter(CONTENT_MATCH_RADIUS * 2 + 1)).tobytes()
        hollow_interiors = enclosed_white_mask(output_ink_image)
        missing = bytes(
            255 if source_ink[index] and not expanded_output[index] and not hollow_interiors[index] else 0
            for index in range(len(source_ink))
        )
        components = missing_ink_components(missing, source_image.size)
        restored = [component for component in components if len(component) <= MAX_RESTORED_COMPONENT_PIXELS]
        unrecoverable = [component for component in components if len(component) > MAX_RESTORED_COMPONENT_PIXELS]
        pixels = output_image.load()
        width, _ = source_image.size
        for component in restored:
            for index in component:
                pixels[index % width, index // width] = (0, 0, 0)
        output_image.save(output_path, format="PNG")
    return {
        "status": "failed" if unrecoverable else "pass",
        "match_radius_px": CONTENT_MATCH_RADIUS,
        "max_restored_component_pixels": MAX_RESTORED_COMPONENT_PIXELS,
        "restored_component_count": len(restored),
        "restored_pixel_count": sum(len(component) for component in restored),
        "unrecoverable_component_count": len(unrecoverable),
        "unrecoverable_pixel_count": sum(len(component) for component in unrecoverable),
    }


def structural_fidelity_check(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Reject added/displaced ink and missing source geometry in either direction."""
    with Image.open(source_path) as source, Image.open(output_path) as output:
        source_image = source.convert("RGB")
        output_image = output.convert("RGB")
        if output_image.size != source_image.size:
            output_image = output_image.resize(source_image.size, Image.Resampling.LANCZOS)
        source_ink = ink_mask(source_image)
        output_ink = ink_mask(output_image)
        expanded_source = source_ink.filter(ImageFilter.MaxFilter(STRUCTURE_MATCH_RADIUS * 2 + 1)).tobytes()
        expanded_output = output_ink.filter(ImageFilter.MaxFilter(STRUCTURE_MATCH_RADIUS * 2 + 1)).tobytes()
        source_bytes = source_ink.tobytes()
        output_bytes = output_ink.tobytes()
    unmatched_output = sum(1 for index, value in enumerate(output_bytes) if value and not expanded_source[index])
    unmatched_source = sum(1 for index, value in enumerate(source_bytes) if value and not expanded_output[index])
    source_ink_pixels = sum(1 for value in source_bytes if value)
    output_ink_pixels = sum(1 for value in output_bytes if value)
    max_unmatched_output = max(
        MIN_UNMATCHED_OUTPUT_INK_PIXELS,
        round(source_ink_pixels * MAX_UNMATCHED_OUTPUT_INK_RATIO),
    )
    max_unmatched_source = max(
        MIN_UNMATCHED_SOURCE_INK_PIXELS,
        round(source_ink_pixels * MAX_UNMATCHED_SOURCE_INK_RATIO),
    )
    failed = unmatched_output > max_unmatched_output or unmatched_source > max_unmatched_source
    return {
        "status": "failed" if failed else "pass",
        "match_radius_px": STRUCTURE_MATCH_RADIUS,
        "source_ink_pixels": source_ink_pixels,
        "output_ink_pixels": output_ink_pixels,
        "unmatched_output_ink_pixels": unmatched_output,
        "unmatched_source_ink_pixels": unmatched_source,
        "max_unmatched_output_ink_pixels": max_unmatched_output,
        "max_unmatched_source_ink_pixels": max_unmatched_source,
    }


def mechanism_source_fidelity_check(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Allow arrow rerouting while rejecting broad loss or regeneration of source content."""
    structure = structural_fidelity_check(source_path, output_path)
    source_ink = max(1, int(structure["source_ink_pixels"]))
    output_ink = int(structure["output_ink_pixels"])
    unmatched_source_ratio = structure["unmatched_source_ink_pixels"] / source_ink
    unmatched_output_ratio = structure["unmatched_output_ink_pixels"] / source_ink
    output_to_source_ratio = output_ink / source_ink
    changed_ink_ratio = (
        structure["unmatched_source_ink_pixels"] + structure["unmatched_output_ink_pixels"]
    ) / source_ink
    failures: list[str] = []
    if unmatched_source_ratio > MECHANISM_MAX_UNMATCHED_SOURCE_INK_RATIO:
        failures.append("too much source ink disappeared outside a credible arrow-only edit")
    if unmatched_output_ratio > MECHANISM_MAX_UNMATCHED_OUTPUT_INK_RATIO:
        failures.append("too much new or displaced ink appeared")
    if output_to_source_ratio < MECHANISM_MIN_OUTPUT_TO_SOURCE_INK_RATIO:
        failures.append("output ink density indicates missing structures, labels, or process steps")
    if changed_ink_ratio < MECHANISM_MIN_CHANGED_INK_RATIO:
        failures.append("no detectable process-arrow geometry change was produced")
    return {
        "status": "failed" if failures else "pass",
        "failures": failures,
        "match_radius_px": structure["match_radius_px"],
        "source_ink_pixels": source_ink,
        "output_ink_pixels": output_ink,
        "unmatched_source_ink_pixels": structure["unmatched_source_ink_pixels"],
        "unmatched_output_ink_pixels": structure["unmatched_output_ink_pixels"],
        "unmatched_source_ink_ratio": round(unmatched_source_ratio, 6),
        "unmatched_output_ink_ratio": round(unmatched_output_ratio, 6),
        "output_to_source_ink_ratio": round(output_to_source_ratio, 6),
        "changed_ink_ratio": round(changed_ink_ratio, 6),
        "max_unmatched_source_ink_ratio": MECHANISM_MAX_UNMATCHED_SOURCE_INK_RATIO,
        "max_unmatched_output_ink_ratio": MECHANISM_MAX_UNMATCHED_OUTPUT_INK_RATIO,
        "min_output_to_source_ink_ratio": MECHANISM_MIN_OUTPUT_TO_SOURCE_INK_RATIO,
        "min_changed_ink_ratio": MECHANISM_MIN_CHANGED_INK_RATIO,
    }


def build_mechanism_edit_mask(
    source_path: Path,
    mask_path: Path,
    protected_boxes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create an RGBA mask that exposes large curved-arrow corridors but protects source ink."""
    with Image.open(source_path) as source:
        source_image = source.convert("RGB")
        source_ink_image = ink_mask(source_image)
        width, height = source_image.size
        components = missing_ink_components(source_ink_image.tobytes(), source_image.size)
        candidates: list[dict[str, Any]] = []
        selected_pixels: set[int] = set()
        min_width = max(80, round(width * 0.14))
        min_height = max(60, round(height * 0.14))
        for component in components:
            if len(component) < 120:
                continue
            xs = [index % width for index in component]
            ys = [index // width for index in component]
            left, top, right, bottom = min(xs), min(ys), max(xs) + 1, max(ys) + 1
            box_width, box_height = right - left, bottom - top
            density = len(component) / max(1, box_width * box_height)
            if box_width < min_width or box_height < min_height or density > 0.09:
                continue
            candidates.append(
                {
                    "pixel_count": len(component),
                    "bbox": [left, top, right, bottom],
                    "density": round(density, 6),
                }
            )
            selected_pixels.update(component)

        alpha = Image.new("L", source_image.size, 255)
        alpha_draw = ImageDraw.Draw(alpha)
        padding = 6
        for candidate in candidates:
            left, top, right, bottom = candidate["bbox"]
            alpha_draw.rectangle(
                (
                    max(0, left - padding),
                    max(0, top - padding),
                    min(width, right + padding),
                    min(height, bottom + padding),
                ),
                fill=0,
            )

        # Re-protect every source-ink pixel that is not part of a selected arrow component.
        protected_ink = bytearray(source_ink_image.tobytes())
        for index in selected_pixels:
            protected_ink[index] = 0
        protected = Image.frombytes("L", source_image.size, bytes(protected_ink)).filter(ImageFilter.MaxFilter(9))
        # Colored labels often touch cycle arrows in raster figures; always protect them even if
        # connected-component analysis grouped them with an arrow.
        saturated = source_image.convert("HSV").getchannel("S").point(lambda value: 255 if value >= 40 else 0)
        protected = ImageChops.lighter(protected, saturated.filter(ImageFilter.MaxFilter(9)))
        protected_draw = ImageDraw.Draw(protected)
        for box in protected_boxes or []:
            protected_draw.rectangle(expanded_box(box, source_image.size, padding=5), fill=255)
        alpha = Image.composite(Image.new("L", source_image.size, 255), alpha, protected)
        mask = Image.merge("RGBA", (Image.new("L", source_image.size, 255),) * 3 + (alpha,))
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        mask.save(mask_path, format="PNG")
    return {
        "status": "ready" if candidates else "no_arrow_corridors_detected",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "mask_path": str(mask_path),
    }


def composite_mechanism_masked_edit(source_path: Path, generated_path: Path, mask_path: Path) -> None:
    """Enforce pixel preservation outside the transparent mechanism-edit mask."""
    with Image.open(source_path) as source, Image.open(generated_path) as generated, Image.open(mask_path) as mask:
        source_image = source.convert("RGB")
        generated_image = generated.convert("RGB")
        if generated_image.size != source_image.size:
            generated_image = generated_image.resize(source_image.size, Image.Resampling.LANCZOS)
        editable = ImageOps.invert(mask.convert("RGBA").getchannel("A"))
        composite = Image.composite(generated_image, source_image, editable)
        composite.save(generated_path, format="PNG")


def ensure_project_dir(review_root: Path, project_id: str) -> Path:
    project = review_root / "review-projects" / project_id
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    return project


def load_candidate_file(review_root: Path, project_id: str, path_arg: str) -> Path:
    if path_arg:
        path = Path(path_arg).resolve()
    else:
        path = review_root / "review-projects" / project_id / "02_section_drafting" / "figure_candidates.json"
    if not path.exists():
        raise SystemExit(f"figure_candidates.json not found: {path}")
    return path


def load_metadata(review_root: Path, paper_id: str) -> dict[str, Any] | None:
    path = review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def human_selected_figure(project: Path, figure: dict[str, Any]) -> dict[str, Any]:
    """Use the Figure Review choice as the source for the next redraw run."""
    paper_id = str(figure.get("paper_id") or "")
    if not paper_id:
        return figure
    candidates_data = read_json(project / "02_section_drafting" / "paper_figure_candidates.json") if (project / "02_section_drafting" / "paper_figure_candidates.json").exists() else {}
    reviews_data = read_json(project / "02_section_drafting" / "human_figure_review.json") if (project / "02_section_drafting" / "human_figure_review.json").exists() else {}
    reviewed = (reviews_data.get("papers") or {}).get(paper_id, {}) if isinstance(reviews_data, dict) else {}
    paper_rows = candidates_data.get("papers", []) if isinstance(candidates_data, dict) else candidates_data
    paper = next((row for row in paper_rows or [] if isinstance(row, dict) and str(row.get("paper_id")) == paper_id), None)
    selected_index = reviewed.get("selected_candidate_index") if isinstance(reviewed, dict) else None
    if selected_index is None and isinstance(paper, dict):
        selected_index = paper.get("selected_candidate_index")
    candidate = next(
        (row for row in (paper or {}).get("candidates", []) if isinstance(row, dict) and row.get("candidate_index") == selected_index),
        None,
    )
    if not isinstance(candidate, dict) or not candidate.get("source_image_path"):
        return figure
    selected = dict(figure)
    selected["source_image_path"] = candidate["source_image_path"]
    selected["source_label"] = candidate.get("source_label") or figure.get("source_label")
    selected["source_caption_text"] = candidate.get("source_label") or figure.get("source_caption_text")
    selected["human_selected_candidate_index"] = selected_index
    selected["human_selected_source"] = True
    return selected


def whiten_large_bright_color_fills(image: Image.Image) -> int:
    """Whiten only proven broad colour interiors, preserving chemical strokes.

    The eroded seed is the safe interior of a coloured region.  Only that seed
    is whitened.  Its one-pixel native boundary remains available to the
    colour-aware black-ink conversion, while thin chemical bonds, ring lines,
    atom labels, and substituent strokes never contain a 3x3 fill seed and are
    therefore retained.
    """
    # Cyan scientific fills are often highly saturated but only medium-bright
    # (for example RGB 1,255,255 has mean brightness 170).  Brightness must not
    # be raised merely because paler antialias pixels also occur in the image,
    # otherwise the cyan interior survives and becomes a black block.
    brightness_cutoff = BRIGHT_COLOR_FILL_BRIGHTNESS
    pixels = image.load()
    mask = Image.new("L", image.size, 0)
    mask_pixels = mask.load()
    for y in range(image.height):
        for x in range(image.width):
            red, green, blue = pixels[x, y]
            spread = max(red, green, blue) - min(red, green, blue)
            brightness = (red + green + blue) / 3
            if spread >= BRIGHT_COLOR_FILL_SPREAD and brightness >= brightness_cutoff:
                mask_pixels[x, y] = 255
    # Erosion keeps only pixels safely inside a broad solid colour region.
    # Do not reconstruct or flood the component: pale magenta/cyan chemical
    # strokes can be connected to the fill and must remain source pixels.
    radius = BRIGHT_COLOR_FILL_MIN_REGION_SIZE // 2
    seeds = Image.new("L", image.size, 0)
    seed_pixels = seeds.load()
    for y in range(radius, image.height - radius):
        for x in range(radius, image.width - radius):
            neighbourhood = [
                pixels[nx, ny]
                for ny in range(y - radius, y + radius + 1)
                for nx in range(x - radius, x + radius + 1)
                if mask_pixels[nx, ny]
            ]
            if len(neighbourhood) != BRIGHT_COLOR_FILL_MIN_REGION_SIZE**2:
                continue
            if all(
                max(color[channel] for color in neighbourhood)
                - min(color[channel] for color in neighbourhood)
                <= BRIGHT_COLOR_FILL_MAX_LOCAL_CHANNEL_RANGE
                for channel in range(3)
            ):
                seed_pixels[x, y] = 255
    replaced = 0
    for y in range(image.height):
        for x in range(image.width):
            if not seed_pixels[x, y]:
                continue
            pixels[x, y] = (255, 255, 255)
            replaced += 1
    return replaced


def chromatic_content_metrics(image_path: Path) -> dict[str, Any]:
    """Measure saturated colour inside the non-white scientific content bounds."""
    with Image.open(image_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        rgb = background.convert("RGB")
    difference = ImageChops.difference(rgb, Image.new("RGB", rgb.size, "white"))
    content_mask = ImageOps.grayscale(difference).point(lambda value: 255 if value >= 12 else 0, mode="L")
    content_box = content_mask.getbbox() or (0, 0, rgb.width, rgb.height)
    content = rgb.crop(content_box)
    pixel_reader = getattr(content, "get_flattened_data", content.getdata)
    pixels = list(pixel_reader())
    chromatic_pixels = sum(
        1
        for red, green, blue in pixels
        if max(red, green, blue) - min(red, green, blue) >= 45
        and min(red, green, blue) <= 225
    )
    return {
        "image_size": list(rgb.size),
        "content_box": list(content_box),
        "content_size": [content.width, content.height],
        "chromatic_pixels": chromatic_pixels,
        "chromatic_ratio": chromatic_pixels / max(len(pixels), 1),
    }


def colored_fill_cleanup_check(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Reject coloured-chemistry outputs that leave most broad source fill intact."""
    source = chromatic_content_metrics(source_path)
    output = chromatic_content_metrics(output_path)
    maximum = max(
        COLORED_MAX_REMAINING_CHROMATIC_RATIO,
        source["chromatic_ratio"] * COLORED_MAX_SOURCE_CHROMATIC_RETENTION,
    )
    failures: list[str] = []
    if output["chromatic_ratio"] > maximum:
        failures.append(
            "non-semantic coloured fill remains above the allowed content-area ratio "
            f"({output['chromatic_ratio']:.4f} > {maximum:.4f})"
        )
    return {
        "status": "failed" if failures else "pass",
        "failures": failures,
        "source": source,
        "output": output,
        "maximum_output_chromatic_ratio": maximum,
    }


def edit_profile_for_figure_type(figure_type: str, render_mode: str) -> str:
    """Derive the edit profile from the final classification, ignoring stale caller flags."""
    if figure_type == FIGURE_TYPE_MECHANISM and render_mode == "ai-edit":
        return MECHANISM_ARROW_STRAIGHTEN_PROFILE
    return "standard"


def render_source_faithful_bw(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Create a high-resolution pure black-and-white copy without regenerating typography."""
    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        target_size = (
            source.width * SOURCE_FAITHFUL_SCALE_FACTOR,
            source.height * SOURCE_FAITHFUL_SCALE_FACTOR,
        )
        rgb = background.convert("RGB")
        hollowed_color_pixels = whiten_large_bright_color_fills(rgb)
        # Work at native resolution.  Luminance-only conversion makes pale
        # magenta/cyan bonds (for example P091 RGB≈254,185,250) disappear even
        # though they are strongly chromatic.  Treat any sufficiently saturated
        # non-white source pixel as ink, then enlarge the binary geometry with
        # neutral antialiasing.
        grayscale_bytes = ImageOps.grayscale(rgb).tobytes()
        native_binary = Image.new("L", rgb.size, 255)
        binary_pixels = native_binary.load()
        chromatic_mask = Image.new("L", rgb.size, 255)
        chromatic_pixels = chromatic_mask.load()
        rgb_pixels = rgb.load()
        for y in range(rgb.height):
            for x in range(rgb.width):
                red, green, blue = rgb_pixels[x, y]
                spread = max(red, green, blue) - min(red, green, blue)
                grayscale_value = grayscale_bytes[y * rgb.width + x]
                chromatic_ink = spread >= 35 and min(red, green, blue) < 248
                if chromatic_ink:
                    chromatic_pixels[x, y] = 0
                if grayscale_value < SOURCE_FAITHFUL_DARK_INK_THRESHOLD or chromatic_ink:
                    binary_pixels[x, y] = 0
        # JPEG colour noise often appears as tiny isolated saturated islands
        # inside a formerly filled ring.  Remove only those small islands
        # chromatic-only components.  Anything also dark in grayscale remains,
        # preserving radical dots, punctuation, stereochemical marks, and true
        # atom/bond strokes.
        chromatic_ink_image = ImageOps.invert(chromatic_mask)
        removed_chromatic_noise_pixels = 0
        for component in missing_ink_components(chromatic_ink_image.tobytes(), rgb.size):
            if len(component) > MAX_ISOLATED_CHROMATIC_NOISE_PIXELS:
                continue
            for index in component:
                x, y = index % rgb.width, index // rgb.width
                chromatic_pixels[x, y] = 255
                if grayscale_bytes[index] >= SOURCE_FAITHFUL_DARK_INK_THRESHOLD:
                    binary_pixels[x, y] = 255
                removed_chromatic_noise_pixels += 1
        if min(source.size) < 360:
            black_and_white = native_binary.resize(target_size, Image.Resampling.LANCZOS)
            if not hollowed_color_pixels:
                black_and_white = black_and_white.filter(ImageFilter.MinFilter(3))
            black_and_white = black_and_white.filter(
                ImageFilter.UnsharpMask(radius=1.2, percent=175, threshold=3)
            )
        else:
            # Preserve the former clean grayscale geometry for ordinary black
            # structures, then add only the chromatic scientific strokes that
            # luminance thresholding would otherwise lose.  This avoids making
            # every black bond four-pixel-native and excessively bold.
            upscaled = rgb.resize(target_size, Image.Resampling.LANCZOS)
            grayscale = ImageOps.autocontrast(ImageOps.grayscale(upscaled))
            grayscale_binary = grayscale.point(
                lambda value: 255 if value >= SOURCE_FAITHFUL_DARK_INK_THRESHOLD else 0,
                mode="L",
            )
            chromatic_upscaled = chromatic_mask.resize(target_size, Image.Resampling.LANCZOS)
            chromatic_upscaled = chromatic_upscaled.filter(
                ImageFilter.UnsharpMask(radius=0.8, percent=120, threshold=3)
            )
            black_and_white = ImageChops.darker(grayscale_binary, chromatic_upscaled)
        black_and_white = black_and_white.point(
            lambda value: 255 if value >= 250 else 0 if value <= 90 else value,
            mode="L",
        )
        conversion_note = "native color-aware ink mask with sharpened neutral antialiased enlargement"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        black_and_white.save(output_path, format="PNG")
        return {
            "width": target_size[0],
            "height": target_size[1],
            "scale_factor": SOURCE_FAITHFUL_SCALE_FACTOR,
            "color_mode": "black and white with neutral antialiasing; only proven bright-fill interiors hollowed",
            "conversion": conversion_note,
            "removed_isolated_chromatic_noise_pixels": removed_chromatic_noise_pixels,
        }


def render_source_faithful_color(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Create a high-resolution source-pixel-preserving copy, retaining color meaning."""
    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        target_size = (
            source.width * SOURCE_FAITHFUL_SCALE_FACTOR,
            source.height * SOURCE_FAITHFUL_SCALE_FACTOR,
        )
        # Upscale only.  Do not threshold, recolor, reconstruct glyphs, or
        # simplify filled scientific markers such as the cyan P137 products.
        upscaled = background.convert("RGB").resize(target_size, Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        upscaled.save(output_path, format="PNG")
        return {
            "width": target_size[0],
            "height": target_size[1],
            "scale_factor": SOURCE_FAITHFUL_SCALE_FACTOR,
            "color_mode": "source colors preserved",
        }


def render_source_faithful_outline_color(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Preserve colored outlines and labels while whitening bright colored interior fills.

    This is a local pixel operation for overview/strategy figures such as P137.
    It never regenerates structures, labels, circles, or bonds.  Dark coloured
    strokes remain; only bright, saturated fill pixels are changed to white.
    """
    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        target_size = (
            source.width * SOURCE_FAITHFUL_SCALE_FACTOR,
            source.height * SOURCE_FAITHFUL_SCALE_FACTOR,
        )
        rgb = background.convert("RGB")
        whiten_large_bright_color_fills(rgb)
        upscaled = rgb.resize(target_size, Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        upscaled.save(output_path, format="PNG")
        return {
            "width": target_size[0],
            "height": target_size[1],
            "scale_factor": SOURCE_FAITHFUL_SCALE_FACTOR,
            "color_mode": "source outlines preserved; bright color fills hollowed",
        }


def resolve_source_image(review_root: Path, figure: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    notes: dict[str, Any] = {
        "resolution_method": None,
        "matched_block_page_idx": None,
        "matched_caption": None,
        "matched_img_relpath": None,
    }
    image_path = figure.get("source_image_path")
    if image_path:
        path = Path(str(image_path)).resolve()
        if path.exists():
            notes["resolution_method"] = "candidate_source_image_path"
            return path, notes
    paper_id = figure.get("paper_id")
    meta = load_metadata(review_root, str(paper_id)) if paper_id else None
    content_list_path = figure.get("source_content_list") or (((meta or {}).get("source_paths") or {}).get("content_list"))
    extracted_dir = (((meta or {}).get("source_paths") or {}).get("extracted_dir"))
    if not content_list_path or not extracted_dir:
        return None, notes
    cpath = Path(str(content_list_path)).resolve()
    edir = Path(str(extracted_dir)).resolve()
    if not cpath.exists() or not edir.exists():
        return None, notes
    try:
        blocks = read_json(cpath)
    except Exception:
        return None, notes
    if not isinstance(blocks, list):
        return None, notes
    wanted_label = normalize_label(figure.get("source_label"))
    wanted_caption = normalize_label(figure.get("source_caption_text"))
    wanted_page = normalize_label(figure.get("source_page_hint"))
    best: tuple[int, dict[str, Any]] | None = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"image", "chart", "table"}:
            continue
        img_rel = block.get("img_path") or block.get("image_path") or block.get("path")
        if not img_rel:
            continue
        captions = []
        for key in ["image_caption", "table_caption", "caption"]:
            value = block.get(key)
            if isinstance(value, list):
                captions.extend(str(x) for x in value if str(x).strip())
            elif isinstance(value, str) and value.strip():
                captions.append(value)
        norm_text = normalize_label(" ".join(captions))
        score = 0
        if wanted_label and wanted_label in norm_text:
            score += 8
        if wanted_caption and wanted_caption[:48] and wanted_caption[:48] in norm_text:
            score += 6
        if wanted_page:
            page_idx = block.get("page_idx")
            if page_idx is not None and str(page_idx + 1) in wanted_page:
                score += 2
        if figure.get("source_type") == block.get("type"):
            score += 1
        if best is None or score > best[0]:
            best = (score, block)
    if best is None or best[0] <= 0:
        return None, notes
    block = best[1]
    img_rel = str(block.get("img_path") or block.get("image_path") or block.get("path"))
    resolved = (edir / img_rel).resolve()
    if not resolved.exists():
        return None, notes
    captions = []
    for key in ["image_caption", "table_caption", "caption"]:
        value = block.get(key)
        if isinstance(value, list):
            captions.extend(str(x) for x in value if str(x).strip())
        elif isinstance(value, str) and value.strip():
            captions.append(value)
    notes["resolution_method"] = "content_list_caption_match"
    notes["matched_block_page_idx"] = block.get("page_idx")
    notes["matched_caption"] = " ".join(captions)
    notes["matched_img_relpath"] = img_rel
    return resolved, notes


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "image/png"


def resolve_api_key(cli_value: str, base_url: str = "") -> str:
    """Prefer the dedicated image credential over text-provider credentials."""
    del base_url
    return _shared_resolve_api_key(
        cli_value,
        env_names=(
            "IMAGE_OPENAI_API_KEY",
            "IMAGE_FALLBACK_API_KEY",
            "OPENAI_API_KEY",
        ),
    )


def resolve_image_field(configured: str, base_url: str) -> str:
    """Choose the multipart image field required by the selected OpenAI-compatible relay."""
    del base_url
    return configured.strip() or os.environ.get("IMAGE_OPENAI_FIELD", "").strip() or "image[]"


def default_base_url() -> str:
    return os.environ.get("IMAGE_OPENAI_BASE_URL", os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL))


def default_wire_api() -> str:
    configured = os.environ.get("IMAGE_OPENAI_WIRE_API", "images")
    if configured.strip().lower().replace("_", "-") == "responses":
        return "responses"
    try:
        return _shared_normalize_wire_api(configured, image=True, default="images")
    except ValueError:
        return "images"


def image_fallback_config() -> dict[str, str]:
    """Return an explicitly enabled secondary image provider configuration."""
    base_url = os.environ.get("IMAGE_FALLBACK_BASE_URL", "").strip()
    wire_api = os.environ.get("IMAGE_FALLBACK_WIRE_API", "").strip().lower().replace("_", "-")
    if not base_url or wire_api not in {"chat", "chat-completion", "chat-completions"}:
        return {}
    return {
        "base_url": base_url,
        "wire_api": "chat-completions",
        "model": os.environ.get("IMAGE_FALLBACK_MODEL", DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL,
        "api_key": (
            os.environ.get("IMAGE_FALLBACK_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        ),
    }


def openai_api_url(base_url: str, endpoint: str) -> str:
    """Build an OpenAI-compatible v1 endpoint without duplicating a configured /v1 suffix."""
    return _shared_openai_endpoint(base_url, endpoint)


def http_error_details(error: urllib.error.HTTPError) -> str:
    """Expose a provider's response body so incompatible image-edit payloads can be diagnosed."""
    try:
        body = error.read().decode("utf-8", "replace").strip()
    except OSError:
        body = ""
    if body:
        try:
            parsed = json.loads(body)
            message = (parsed.get("error") or {}).get("message") if isinstance(parsed, dict) else None
            if message:
                body = str(message)
        except json.JSONDecodeError:
            pass
    return f"HTTP {error.code}: {body or error.reason}"


def build_headers(api_key: str, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "review-writer-figure-redraw/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
GENERIC_TRANSIENT_RETRY_DELAYS = (1, 2)
CHANNEL_FAILURE_RETRY_DELAYS = (15, 45)
SAFETY_MODERATION_ERROR_MARKERS = (
    "内容被安全审核拦截",
    "疑似成人内容",
    "安全审核",
    "内容审核",
    "adult content",
    "sexual content",
    "nsfw",
    "content_filter",
    "content filter",
    "safety policy",
    "safety review",
    "moderation",
    "policy violation",
)


def transient_retry_delay(details: str, attempt: int) -> int:
    """Give an exhausted provider channel enough time to recover before retrying."""
    delays = (
        CHANNEL_FAILURE_RETRY_DELAYS
        if "ALL_CHANNELS_FAILED" in details.upper()
        else GENERIC_TRANSIENT_RETRY_DELAYS
    )
    return delays[min(max(attempt, 0), len(delays) - 1)]


def is_safety_moderation_rejection(error: BaseException | str) -> bool:
    """Recognize an explicit provider moderation rejection, not a generic 400."""
    details = str(error or "").casefold()
    return any(marker.casefold() in details for marker in SAFETY_MODERATION_ERROR_MARKERS)


def call_with_academic_safety_retry(
    requester: Any,
    prompt: str,
    safety_prompt: str,
    audit_log: list[dict[str, Any]],
    generation_attempt: int = 1,
) -> Any:
    """Retry one false-positive moderation rejection with a concise academic prompt."""
    try:
        return requester(prompt)
    except RuntimeError as exc:
        if not is_safety_moderation_rejection(exc):
            raise
        audit = {
            "generation_attempt": generation_attempt,
            "status": "retrying",
            "prompt_variant": "academic-chemistry-safety-context",
            "trigger_error": str(exc)[-2000:],
        }
        audit_log.append(audit)
        try:
            result = requester(safety_prompt)
        except RuntimeError as retry_error:
            audit["status"] = "failed"
            audit["retry_error"] = str(retry_error)[-2000:]
            raise RuntimeError(
                "Image edit request was blocked by provider safety review and the concise "
                f"academic-chemistry prompt retry also failed: {retry_error}"
            ) from retry_error
        audit["status"] = "succeeded"
        return result


def decode_json_object(raw: bytes, label: str) -> dict[str, Any]:
    if not raw.strip():
        raise RuntimeError(f"{label} returned an empty response body")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw.decode("utf-8", "replace")[:300].replace("\r", " ").replace("\n", " ")
        raise RuntimeError(f"{label} returned non-JSON content: {preview or '<empty>'}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} returned JSON {type(data).__name__}, expected an object")
    return data


def open_json_request(request: urllib.request.Request, label: str, timeout: int = 300) -> dict[str, Any]:
    for attempt in range(3):
        retry_delay = GENERIC_TRANSIENT_RETRY_DELAYS[
            min(attempt, len(GENERIC_TRANSIENT_RETRY_DELAYS) - 1)
        ]
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=timeout) as response:
                return decode_json_object(response.read(), label)
        except urllib.error.HTTPError as exc:
            details = http_error_details(exc)
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise RuntimeError(f"{label} rejected: {details}") from exc
            retry_delay = transient_retry_delay(details, attempt)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{label} transport failed: {exc}") from exc
        time.sleep(retry_delay)
    raise RuntimeError(f"{label} failed after retries")


def build_multipart_form(fields: dict[str, Any], file_fields: list[tuple[str, Path]]) -> tuple[str, bytes]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, path in file_fields:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {guess_mime(path)}\r\n\r\n".encode("utf-8"))
        body.extend(path.read_bytes())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def image_edit_file_fields(image_path: Path, image_field: str = "image[]") -> list[tuple[str, Path]]:
    """Build the image-upload field required by the selected OpenAI-compatible relay."""
    return [(image_field.strip() or "image[]", image_path)]


def figure_type_prompt(figure_type: str) -> str:
    """Return type-specific constraints without weakening the shared chemistry lock."""
    prompts = {
        FIGURE_TYPE_SCOPE: (
            " This is a reaction/substrate-scope grid. Preserve every cell, molecule, substituent, example number, "
            "yield, selectivity value, footnote, condition, and failed-example marker one-for-one. Do not consolidate "
            "examples or replace repeated structures with generic placeholders. Improve colour consistency, white "
            "background, edge continuity, and legibility only."
        ),
        FIGURE_TYPE_MULTIPANEL: (
            " This is a complex multi-panel chemistry figure. Preserve every panel boundary, panel letter, reading "
            "order, cross-panel arrow, inset, legend, structure, and annotation. Do not merge, split, reorder, or "
            "summarize panels. Improve colour balance and edge clarity only."
        ),
        FIGURE_TYPE_LOW_RESOLUTION: (
            " This is a low-resolution or thin-stroke chemistry source. Upscale and strengthen only lines and glyphs "
            "that are visibly present in the upload. Do not guess, autocomplete, infer, or reconstruct an ambiguous "
            "bond, atom, digit, substituent, stereochemical mark, or arrow. Prefer faithful soft antialiasing over "
            "inventing missing detail."
        ),
        FIGURE_TYPE_COLORED: (
            " This is a coloured chemistry figure. Remove every broad non-semantic cyan, teal, blue, green, magenta, "
            "orange, or other solid fill completely and make the affected aromatic-ring, fused-ring, circle, node, or "
            "marker interior pure white. In particular, a solid colour behind a benzene or fused heteroaromatic ring "
            "is decorative fill, not a chemical bond: delete the fill but retain the exact ring perimeter, every inner "
            "alternating/double-bond stroke, heteroatom, substituent bond, R-group label, and symbol as crisp line art. "
            "Retain colours only when they encode arrow classes, data series, atom identity, or scientific meaning. "
            "Never leave the source fill unchanged and never convert a filled region into a black block."
        ),
        FIGURE_TYPE_TABLE: (
            " This is a scientific data table. Preserve the exact row and column count, cell boundaries, headings, "
            "numbers, decimal places, signs, units, superscripts, subscripts, footnotes, and blank cells. Never "
            "recalculate, translate, normalize, or invent a value. Improve text and rule sharpness only."
        ),
        FIGURE_TYPE_PLOT: (
            " This is a scientific plot or spectrum. Preserve every data curve and point at its exact coordinate, "
            "together with axes, scales, ticks, legends, colours, error bars, labels, and annotations. Do not smooth, "
            "fit, move, remove, or invent data. Improve visual clarity only."
        ),
    }
    return prompts.get(figure_type, "")


def build_prompt(
    style_name: str,
    figure: dict[str, Any],
    ocr_text: str = "",
    figure_type: str = "simple-scheme",
) -> str:
    ocr_constraint = ""
    if ocr_text.strip():
        ocr_constraint = (
            " OCR transcription (advisory integrity constraint): preserve every legible token below exactly; "
            "do not add, omit, or substitute it. OCR transcription: "
            f"{ocr_text.strip()[:2000]}"
        )
    return (
        "Apply one consistent publication style to the supplied figure: crisp medium-weight black line art, "
        "pure white background, even contrast, clean antialiasing, consistent line caps and arrowheads, and no "
        "decorative gradients, shadows, textures, or grayscale fills. "
        "Do not introduce cyan, teal, green, orange, or any other filled colour regions: molecule-ring interiors "
        "and non-semantic substituent-marker circles must remain white and hollow, with only their source outlines "
        "and symbols retained. "
        "Preserve every coloured word, glyph, letter, number, and label as a solid, continuous, crisp stroke in its "
        "original colour; never hollow, fade, perforate, blur, or break coloured text while removing a large shape fill. "
        "Treat the scientific layer as immutable. Trace every molecule and reaction network from the source exactly. "
        "Preserve the number, identity, position, orientation, and connectivity of every atom, substituent, ring, "
        "single bond, double bond, triple bond, aromatic bond, allene, wedge, dash, charge, radical, lone-pair mark, "
        "and stereochemical descriptor. Preserve every reaction/process arrow, including its count, source object, "
        "target object, direction, branch relationship, and sequence. Preserve every reagent, catalyst, solvent, "
        "stoichiometry, temperature, time, yield, condition, label, panel, table value, and numbering token. "
        "Never add, remove, merge, split, move, reconnect, rotate, rename, infer, or simplify scientific content. "
        "If any source detail is ambiguous, copy its pixels/geometry rather than inventing a replacement. "
        "Presentation-only cleanup may normalize stroke appearance, background, contrast, and non-scientific borders "
        "inside their existing bounds; it must not move or obscure chemical structures, arrows, or text. "
        "Keep canvas dimensions, aspect ratio, panel order, and relative placement unchanged. "
        "The output must be scientifically and topologically identical to the source. "
        f"Style preset: {style_name}.{figure_type_prompt(figure_type)}{ocr_constraint}"
    )


def build_ocr_hollow_prompt(
    style_name: str,
    figure: dict[str, Any],
    ocr_text: str = "",
    figure_type: str = "simple-scheme",
) -> str:
    return (
        build_prompt(style_name, figure, ocr_text, figure_type)
        + " The input has all original text regions masked. Do not generate any letters, numbers, labels, or symbols. "
        "Redraw only the non-text chemistry graphics in pure black on white. Convert every solid filled icon, node, arrowhead, "
        "or shape into a black outline with a white hollow interior, while preserving bond connectivity and geometry. "
        "Every original reaction mark, chemical symbol, bond, arrow, stereochemical mark, panel marker, and label is mandatory; "
        "never omit a mark even when it is small or visually ambiguous."
    )


MECHANISM_PROMPT_REQUIRED_TOKENS = ("h\u03bd", "SN2\u2032 oxidative addition")
MECHANISM_PROMPT_MOJIBAKE_REPLACEMENTS = (
    ("h\u8c13", "h\u03bd"),
    ("\u9225?", "\u2032"),
    ("\u951f", ""),
    ("\ufffd", ""),
)


def repair_mechanism_prompt_text(prompt: str) -> str:
    """Repair known prompt mojibake and continue with canonical chemistry text."""
    repaired = prompt
    for damaged, canonical in MECHANISM_PROMPT_MOJIBAKE_REPLACEMENTS:
        repaired = repaired.replace(damaged, canonical)
    if "h\u03bd" not in repaired:
        repaired += " Preserve the exact photocatalysis label h\u03bd."
    if "SN2\u2032 oxidative addition" not in repaired:
        repaired += " Preserve the exact text 'SN2\u2032 oxidative addition'."
    return repaired


def build_mechanism_arrow_straighten_prompt(figure: dict[str, Any]) -> str:
    """Strict local-edit prompt for mechanism figures with curved process arrows."""
    prompt = (
        "Use the uploaded original chemical reaction mechanism image as the only base image. "
        "Perform a strict, pixel-preserving local inpaint; do not redraw, reinterpret, regenerate, crop, "
        "rescale, or relayout the figure. "
        "Copy every pixel outside the narrow strokes of the process arrows from the source unchanged. "
        "Never replace the source with a simplified diagram and never create a blank area where source ink exists. "
        "The only permitted modification is this: convert every curved, arc-shaped, circular, or free-form "
        "process arrow into a straight arrow or an orthogonal horizontal-and-vertical right-angle polyline arrow. "
        "Before editing, account for every blue, black, and magenta/purple arrow in the source. Preserve the "
        "complete arrow count: never omit, add, merge, split, or change any reaction-flow arrow. "
        "For each arrow, preserve its exact start object, end object, connection relationship, direction, "
        "color category, and line thickness. Prefer a single straight segment; only when it would cross a "
        "molecule, chemical bond, or text, use a horizontal-vertical right-angle route that avoids the obstacle. "
        "Use no arcs, curves, circular arrows, or free-form curved arrows. Every arrow must have a crisp, "
        "correctly oriented arrowhead. Keep blue arrows blue, magenta/purple photocatalytic-cycle arrows "
        "magenta/purple, and black auxiliary reaction arrows black. "
        "Everything except arrow geometry is locked and must remain visually identical to the original: "
        "all molecular structures, bond orders and angles, allenes, atom/group/formula labels, R-group "
        "positions and subscripts, charges, radical dots, oxidation states, catalysts, reagents, solvents, "
        "photocatalyst labels, h\u03bd, compound labels and numbers, all text including "
        "'SN2\u2032 oxidative addition', "
        "fonts, font sizes, weights, italic/subscript/superscript formatting, positions, distances, angles, "
        "white background, canvas size, and aspect ratio. Do not alter any chemical bond or character. "
        "The final image must retain approximately the same non-white ink density as the source; loss of a molecule, "
        "intermediate, label, colored step name, branch, or cycle segment is an invalid result. "
        "Before returning the image, compare all quadrants with the source and verify that no source content was erased. "
        "Convert every visibly isolated curved process arrow; returning the original image with all curved arrows "
        "unchanged is not a valid result. If one individual arrow cannot be converted without altering other content, "
        "leave that arrow unchanged (and only that arrow) instead of deleting or simplifying any surrounding chemistry. "
        "Reject any edit that changes chemistry, typography, layout, arrow direction, arrow color, or arrow count. "
        "Output the same high-resolution image with only the specified arrow-shape changes."
    )
    return repair_mechanism_prompt_text(prompt)


def build_academic_chemistry_safety_retry_prompt(
    figure_type: str,
    edit_profile: str,
) -> str:
    """Build a short, neutral retry prompt for scientific-image moderation false positives."""
    prompt = (
        "Academic chemistry diagram editing task. The uploaded image is a peer-reviewed scientific schematic "
        "containing only chemical structures, formulas, labels, and reaction arrows; it is not a photograph and "
        "contains no people. Use the upload as the sole base image. Preserve every molecule, atom, bond order, ring, "
        "stereochemical mark, charge, formula, label, number, condition, arrow endpoint, arrow direction, arrow color, "
        "and layout exactly. Do not add, remove, infer, rename, reconnect, crop, or rearrange scientific content. "
    )
    if edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE or figure_type == FIGURE_TYPE_MECHANISM:
        prompt += (
            "Change only the shape of reaction-flow arrows: replace each clearly curved process arrow with a straight "
            "arrow or a horizontal-and-vertical right-angle arrow. Keep the original arrow count, endpoints, direction, "
            "color, thickness, and connections. Leave an arrow unchanged if editing it could affect nearby chemistry. "
            "Keep all non-arrow pixels unchanged. Preserve h\u03bd and SN2\u2032 oxidative addition exactly if present. "
        )
    elif figure_type == FIGURE_TYPE_COLORED:
        prompt += (
            "Remove only broad decorative color fills, leaving affected interiors white while preserving every outline, "
            "inner aromatic bond, heteroatom, substituent, label, and scientifically meaningful color. "
        )
    else:
        prompt += (
            "Improve only line continuity, edge clarity, contrast, and the white background without changing content. "
        )
    return prompt + "Return one edited scientific diagram image and no explanatory text."


def chemistry_integrity_gate(redraw_row: dict[str, Any]) -> dict[str, Any]:
    """Fail closed unless the automated checks required by the render mode pass."""
    render_mode = str(redraw_row.get("render_mode") or "")
    edit_profile = str(redraw_row.get("edit_profile") or "standard")
    if render_mode in SOURCE_FAITHFUL_RENDER_MODES:
        return {"status": "pass", "failures": [], "human_check_required": True}

    failures: list[str] = []
    source_has_text = bool(str(redraw_row.get("ocr_source_text") or "").strip())
    text_protection = str(redraw_row.get("ocr_text_protection") or "")
    structure_status = str((redraw_row.get("structural_fidelity") or {}).get("status") or "")

    if edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE:
        mechanism_fidelity = redraw_row.get("mechanism_source_fidelity") or {}
        if mechanism_fidelity.get("status") != "pass":
            failures.extend(
                str(item) for item in mechanism_fidelity.get("failures") or ["mechanism source-fidelity check failed"]
            )
        return {
            "status": "failed" if failures else "needs_human_arrow_check",
            "failures": failures,
            "human_check_required": True,
            "manual_check": "Verify arrow count, endpoints, direction, branching, color, and route against the source.",
        }

    if str(redraw_row.get("figure_type") or "") == FIGURE_TYPE_COLORED:
        cleanup = redraw_row.get("colored_fill_cleanup") or {}
        if cleanup.get("status") != "pass":
            failures.extend(
                str(item) for item in cleanup.get("failures") or ["coloured fill cleanup check failed"]
            )

    if structure_status != "pass":
        failures.append("bidirectional line geometry check failed")
    if render_mode == "ocr-hollow-ai":
        source_ocr_status = str(redraw_row.get("ocr_source_status") or "")
        if source_ocr_status != "ok":
            failures.append("source OCR was not available")
        if str((redraw_row.get("content_fidelity") or {}).get("status") or "") != "pass":
            failures.append("source content fidelity check failed")
        if source_has_text and text_protection != "source_regions_restored":
            failures.append("source text pixels were not restored")

    return {
        "status": "failed" if failures else "pass",
        "failures": failures,
        "human_check_required": True,
    }


def retain_successful_mechanism_attempt_after_retry_error(
    redraw_row: dict[str, Any],
    out_path: Path,
    mechanism_attempts: list[dict[str, Any]],
    error: BaseException,
) -> bool:
    """Keep the last valid image when only a later mechanism retry fails.

    Mechanism edits deliberately retry outputs that fail the conservative pixel
    gate.  A provider error during a later retry must not erase the image from a
    completed earlier request: that image remains useful as a human-reviewable
    preview and is safer than silently reverting to no output.
    """
    if not mechanism_attempts or not out_path.is_file():
        return False
    last_attempt = mechanism_attempts[-1]
    source_fidelity = last_attempt.get("source_fidelity") or {}
    failures = [str(value) for value in last_attempt.get("failures") or []]
    retry_error = f"{type(error).__name__}: {error}"
    redraw_row["mechanism_edit_attempts"] = mechanism_attempts
    redraw_row["mechanism_source_fidelity"] = source_fidelity
    redraw_row["mechanism_retry_error"] = retry_error[-2000:]
    redraw_row["redrawn_image"] = str(out_path)
    redraw_row["status"] = "redrawn"
    redraw_row["output_disposition"] = "saved_with_integrity_warning"
    failure_summary = "; ".join(failures) or "automated mechanism integrity check requires review"
    redraw_row["notes"] = (
        "Kept the last successfully generated mechanism image for human review after "
        f"a later provider retry failed. Integrity warning: {failure_summary}. "
        f"Later retry error: {retry_error}"
    )
    redraw_row["chemistry_integrity"] = chemistry_integrity_gate(redraw_row)
    return True


def call_images_edit(
    api_key: str,
    base_url: str,
    image_path: Path,
    prompt: str,
    model: str,
    quality: str,
    background: str,
    output_format: str,
    image_field: str = "image[]",
    transport: str = "urllib",
    mask_path: Path | None = None,
) -> dict[str, Any]:
    if transport == "curl":
        return call_images_edit_curl(
            api_key,
            base_url,
            image_path,
            prompt,
            model,
            quality,
            background,
            output_format,
            image_field,
            mask_path=mask_path,
        )
    fields = {
        "model": model,
        "prompt": prompt,
        "quality": quality,
        "background": background,
        "output_format": output_format,
    }
    file_fields = image_edit_file_fields(image_path, image_field)
    if mask_path:
        file_fields.append(("mask", mask_path))
    content_type, body = build_multipart_form(fields, file_fields)
    req = urllib.request.Request(
        openai_api_url(base_url, "/images/edits"),
        data=body,
        headers=build_headers(api_key, content_type),
        method="POST",
    )
    return open_json_request(req, "Image edit request")


def call_images_edit_curl(
    api_key: str,
    base_url: str,
    image_path: Path,
    prompt: str,
    model: str,
    quality: str,
    background: str,
    output_format: str,
    image_field: str,
    runner: Any = subprocess.run,
    mask_path: Path | None = None,
) -> dict[str, Any]:
    """Call image edits with curl when a relay rejects urllib multipart uploads."""
    curl = shutil.which("curl") or shutil.which("curl.exe") or "curl"
    command = [
        curl,
        "--config", "-",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--connect-timeout", "10",
        "--max-time", "300",
        "-X", "POST",
        "-F", f"model={model}",
        "-F", f"prompt={prompt}",
        "-F", f"{image_field.strip() or 'image[]'}=@{image_path}",
        "-F", f"quality={quality}",
        "-F", f"background={background}",
        "-F", f"output_format={output_format}",
        openai_api_url(base_url, "/images/edits"),
    ]
    if mask_path:
        command[-1:-1] = ["-F", f"mask=@{mask_path}"]
    config = f'header = "Authorization: Bearer {api_key}"\n'.encode("utf-8")
    try:
        result = runner(command, input=config, capture_output=True, timeout=310, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"curl image edit failed: {type(exc).__name__}: {exc}") from exc
    response_bytes = bytes(result.stdout or b"")
    response_text = response_bytes.decode("utf-8", "replace")
    if result.returncode != 0:
        raise RuntimeError(f"curl image edit failed (exit {result.returncode}): {response_text[:500]}")
    return decode_json_object(response_bytes, "curl image edit request")


def call_responses_image_edit(
    api_key: str,
    base_url: str,
    image_path: Path,
    prompt: str,
    model: str,
    quality: str,
    background: str,
    output_format: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "input": prompt,
        "tools": [
            {
                "type": "image_generation",
                "quality": quality,
                "background": background,
                "output_format": output_format,
            }
        ],
    }
    req = urllib.request.Request(
        openai_api_url(base_url, "/responses"),
        data=json.dumps(payload).encode("utf-8"),
        headers=build_headers(api_key, "application/json"),
        method="POST",
    )
    return open_json_request(req, "Responses image edit request")


def call_chat_completions_image_edit(
    api_key: str,
    base_url: str,
    image_path: Path,
    prompt: str,
    model: str,
) -> dict[str, Any]:
    """Edit one image through a provider's multimodal Chat Completions route.

    Some OpenAI-compatible image relays expose GPT Image through
    /v1/chat/completions instead of multipart /v1/images/edits.  The current
    Stage 6 source image is embedded directly in the request as a data URI, so
    the fallback cannot accidentally reuse a stale remote image.
    """
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{guess_mime(image_path)};base64,{encoded}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    }
    last_response: dict[str, Any] = {}
    # Streaming avoids many reverse-proxy timeouts, but some relays emit empty
    # SSE choices and attach the image only to the final non-streaming JSON.
    # Try both transports before declaring the provider incapable of producing
    # an image for this request.
    stream_modes = [True] * max(CHAT_COMPLETION_NO_IMAGE_ATTEMPTS - 1, 0) + [False]
    for attempt in range(CHAT_COMPLETION_NO_IMAGE_ATTEMPTS):
        payload["stream"] = stream_modes[attempt]
        req = urllib.request.Request(
            openai_api_url(base_url, "/chat/completions"),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=build_headers(api_key, "application/json; charset=utf-8"),
            method="POST",
        )
        last_response = open_chat_completion_request(req, "Chat Completions image edit request")
        if extract_chat_completion_image_reference(last_response):
            return last_response
        if attempt + 1 < CHAT_COMPLETION_NO_IMAGE_ATTEMPTS:
            time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(
        "Chat Completions image edit response did not contain an image after "
        f"{CHAT_COMPLETION_NO_IMAGE_ATTEMPTS} attempts; "
        + f"request_modes={['stream' if mode else 'non-stream' for mode in stream_modes]}; "
        + chat_completion_response_summary(last_response)
    )


def open_chat_completion_request(
    request: urllib.request.Request,
    label: str,
    timeout: int = 600,
) -> dict[str, Any]:
    """Read either a normal JSON response or an OpenAI-compatible SSE stream."""
    for attempt in range(3):
        retry_delay = GENERIC_TRANSIENT_RETRY_DELAYS[
            min(attempt, len(GENERIC_TRANSIENT_RETRY_DELAYS) - 1)
        ]
        try:
            with urllib.request.urlopen(
                request,
                context=ssl.create_default_context(),
                timeout=timeout,
            ) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    return decode_json_object(response.read(), label)
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                image_items: list[Any] = []
                finish_reason = ""
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event_text = line[5:].strip()
                    if not event_text or event_text == "[DONE]":
                        continue
                    try:
                        event = json.loads(event_text)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if isinstance(choices, list) and choices:
                        choice = choices[0] or {}
                        finish_reason = str(choice.get("finish_reason") or finish_reason)
                        delta = choice.get("delta") or choice.get("message") or {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(content, str):
                            content_parts.append(content)
                        elif isinstance(content, list):
                            image_items.extend(content)
                        elif isinstance(content, dict):
                            image_items.append(content)
                        if isinstance(delta, dict):
                            for key in ("images", "image", "result", "output"):
                                value = delta.get(key)
                                if value is not None:
                                    image_items.append(value)
                            for key in ("reasoning_content", "refusal"):
                                value = delta.get(key)
                                if isinstance(value, str) and value.strip():
                                    reasoning_parts.append(value)
                    if isinstance(event, dict) and isinstance(event.get("delta"), str):
                        content_parts.append(event["delta"])
                    if isinstance(event, dict):
                        for key in ("data", "images", "image", "result", "output"):
                            value = event.get(key)
                            if value is not None:
                                image_items.append(value)
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(content_parts),
                }
                if reasoning_parts:
                    message["reasoning_content"] = "".join(reasoning_parts)
                if image_items:
                    message["images"] = image_items
                return {"choices": [{"message": message, "finish_reason": finish_reason}]}
        except urllib.error.HTTPError as exc:
            details = http_error_details(exc)
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise RuntimeError(f"{label} rejected: {details}") from exc
            retry_delay = transient_retry_delay(details, attempt)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{label} transport failed: {exc}") from exc
        time.sleep(retry_delay)
    raise RuntimeError(f"{label} failed after retries")


def extract_response_image_base64(response: dict[str, Any]) -> str | None:
    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image_generation_call":
            continue
        result = item.get("result")
        if isinstance(result, str) and result.strip():
            return result
    return None


def save_redrawn_image(response: dict[str, Any], out_path: Path) -> None:
    items = response.get("data") or []
    if not items:
        raise RuntimeError("image edit response missing data")
    b64 = items[0].get("b64_json")
    if not b64:
        raise RuntimeError("image edit response missing b64_json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(b64))


def save_response_redrawn_image(response: dict[str, Any], out_path: Path) -> None:
    b64 = extract_response_image_base64(response)
    if not b64:
        raise RuntimeError("responses image edit response missing image_generation result")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(b64))


_DATA_IMAGE_PATTERN = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", re.IGNORECASE)
_HTTP_IMAGE_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _decode_image_base64(value: str) -> bytes | None:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 32:
        return None
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    signatures = (
        decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        decoded.startswith(b"\xff\xd8\xff"),
        decoded.startswith((b"GIF87a", b"GIF89a")),
        decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
    )
    return decoded if any(signatures) else None


def _image_reference_from_string(value: str, allow_plain_url: bool = True) -> tuple[str, Any] | None:
    data_match = _DATA_IMAGE_PATTERN.search(value)
    if data_match:
        decoded = _decode_image_base64(data_match.group(1))
        if decoded:
            return "bytes", decoded
    markdown_match = _MARKDOWN_IMAGE_PATTERN.search(value)
    if markdown_match:
        return "url", markdown_match.group(1)
    if allow_plain_url:
        url_match = _HTTP_IMAGE_PATTERN.search(value)
        if url_match:
            return "url", url_match.group(0).rstrip(".,;)")
    decoded = _decode_image_base64(value)
    if decoded:
        return "bytes", decoded
    return None


def _image_reference_from_node(node: Any) -> tuple[str, Any] | None:
    if isinstance(node, str):
        return _image_reference_from_string(node)
    if isinstance(node, list):
        for item in node:
            found = _image_reference_from_node(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    for key in ("b64_json", "image_base64", "base64", "result"):
        value = node.get(key)
        if isinstance(value, str):
            found = _image_reference_from_string(value, allow_plain_url=False)
            if found:
                return found
    for key in ("image_url", "url", "image", "images", "content"):
        if key not in node:
            continue
        found = _image_reference_from_node(node[key])
        if found:
            return found
    return None


def extract_chat_completion_image_reference(response: dict[str, Any]) -> tuple[str, Any] | None:
    """Accept common New API / relay response shapes without binding to one vendor."""
    data = response.get("data")
    found = _image_reference_from_node(data)
    if found:
        return found
    choices = response.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or choice.get("delta")
            found = _image_reference_from_node(message)
            if found:
                return found
    return _image_reference_from_node(response.get("output"))


def chat_completion_response_summary(response: dict[str, Any]) -> str:
    """Return safe diagnostics for a successful response that produced no image."""
    keys = sorted(str(key) for key in response.keys()) if isinstance(response, dict) else []
    finish_reasons: list[str] = []
    text_parts: list[str] = []
    choices = response.get("choices") if isinstance(response, dict) else None
    if isinstance(choices, list):
        for choice in choices[:3]:
            if not isinstance(choice, dict):
                continue
            finish_reason = str(choice.get("finish_reason") or "").strip()
            if finish_reason:
                finish_reasons.append(finish_reason)
            message = choice.get("message") or choice.get("delta") or {}
            if not isinstance(message, dict):
                continue
            for field in ("content", "refusal", "reasoning_content"):
                value = message.get(field)
                if isinstance(value, str) and value.strip():
                    cleaned = _DATA_IMAGE_PATTERN.sub("[image data omitted]", value.strip())
                    text_parts.append(cleaned[:400])
    error = response.get("error") if isinstance(response, dict) else None
    if error:
        text_parts.append(str(error)[:400])
    summary = [f"response_keys={keys}", f"choice_count={len(choices) if isinstance(choices, list) else 0}"]
    if finish_reasons:
        summary.append("finish_reason=" + ",".join(finish_reasons))
    if text_parts:
        summary.append("provider_text=" + " | ".join(text_parts)[:600])
    else:
        summary.append("provider_text=<empty>")
    return "; ".join(summary)


def _write_validated_image(raw: bytes, out_path: Path) -> None:
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            suffix = out_path.suffix.lower()
            image_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}.get(suffix, "PNG")
            if image_format == "JPEG":
                image = image.convert("RGB")
            elif image.mode not in {"RGB", "RGBA", "L", "LA"}:
                image = image.convert("RGBA")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            options = {"quality": 95} if image_format != "PNG" else {"optimize": True}
            image.save(out_path, format=image_format, **options)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Chat Completions returned bytes that are not a valid image") from exc


def save_chat_completion_redrawn_image(response: dict[str, Any], out_path: Path) -> None:
    reference = extract_chat_completion_image_reference(response)
    if not reference:
        raise RuntimeError("Chat Completions image edit response did not contain an image")
    kind, value = reference
    if kind == "bytes":
        raw = value
    else:
        request = urllib.request.Request(
            str(value),
            headers={
                "Accept": "image/*",
                "User-Agent": "review-writer-figure-redraw/1.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                context=ssl.create_default_context(),
                timeout=180,
            ) as response_stream:
                raw = response_stream.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not download the Chat Completions image: {exc}") from exc
    _write_validated_image(raw, out_path)


def should_fail_over_image_provider(error: BaseException) -> bool:
    details = str(error).upper()
    return "ALL_CHANNELS_FAILED" in details or any(
        marker in details for marker in ("HTTP 502", "HTTP 503", "HTTP 504")
    )


def prepare_aspect_preserving_edit_input(source_path: Path, framed_path: Path) -> dict[str, Any]:
    """Letterbox a non-square edit source so square-only providers cannot reflow it.

    Several OpenAI-compatible relays return a 1024x1024 edit even when the
    uploaded chemistry figure is wide or tall.  Uploading the unframed source
    lets the model reinterpret the layout to fill that square.  A white square
    wrapper keeps the original content rectangle geometrically unchanged; the
    same rectangle is cropped back out after generation.
    """
    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGBA")
        background = Image.new("RGBA", source.size, "white")
        background.alpha_composite(source)
        source_rgb = background.convert("RGB")

    source_width, source_height = source_rgb.size
    longest_side = max(source_width, source_height)
    scale = min(1.0, AI_EDIT_MAX_INPUT_SIDE / max(1, longest_side))
    content_width = max(1, round(source_width * scale))
    content_height = max(1, round(source_height * scale))
    if (content_width, content_height) != source_rgb.size:
        content = source_rgb.resize((content_width, content_height), Image.Resampling.LANCZOS)
    else:
        content = source_rgb

    canvas_side = max(content_width, content_height)
    left = (canvas_side - content_width) // 2
    top = (canvas_side - content_height) // 2
    applied = content_width != content_height
    if applied:
        canvas = Image.new("RGB", (canvas_side, canvas_side), "white")
        canvas.paste(content, (left, top))
        framed_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(framed_path, format="PNG", optimize=True)
        input_path = framed_path
    else:
        input_path = source_path

    return {
        "status": "letterboxed" if applied else "square_source",
        "applied": applied,
        "input_path": str(input_path),
        "source_size": [source_width, source_height],
        "source_aspect_ratio": source_width / max(1, source_height),
        "canvas_size": [canvas_side, canvas_side],
        "content_box": [left, top, content_width, content_height],
    }


def append_aspect_ratio_prompt(prompt: str, framing: dict[str, Any]) -> str:
    """Tell the model that the technical padding is not editable content."""
    if not framing.get("applied"):
        return prompt
    width, height = framing["source_size"]
    return (
        prompt
        + f" The upload is a technical square wrapper around an original {width}x{height} figure. "
        "Keep the centered content rectangle at exactly its current scale, aspect ratio, and position. "
        "Leave every outer white padding band completely blank; do not expand, stretch, redistribute, "
        "or move any chemistry into that padding."
    )


def standard_ai_edit_uses_provider_canvas(render_mode: str, edit_profile: str) -> bool:
    """Allow ordinary generative redraws to keep the provider's native canvas.

    Pixel-local mechanism edits and OCR-coordinate workflows still require the
    exact source canvas.  A normal AI redraw is a newly rendered illustration,
    so cropping a square provider response back to a tall or wide source can
    delete valid generated content.
    """
    return render_mode == "ai-edit" and edit_profile == "standard"


def append_provider_canvas_prompt(prompt: str, framing: dict[str, Any]) -> str:
    """Require complete content while permitting the provider output ratio."""
    if not framing.get("applied"):
        return prompt
    width, height = framing["source_size"]
    return (
        prompt
        + f" The upload contains a complete original {width}x{height} figure inside a square technical canvas. "
        "The output may use the image provider's native canvas and aspect ratio. Keep every source panel, molecule, "
        "label, arrow, and boundary fully visible with safe white margins; do not crop any source content."
    )


def normalize_generated_aspect(
    generated_path: Path,
    source_path: Path,
    framing: dict[str, Any],
) -> dict[str, Any]:
    """Remove technical padding without cutting provider-generated content.

    A square-only provider can move labels or structures into the white wrapper
    even when the prompt asks it not to.  In that case the original fixed crop
    is unsafe: expand it to the generated ink (or retain the complete provider
    canvas) and keep the resulting native aspect ratio instead of stretching it
    back to the source dimensions.
    """
    with Image.open(source_path) as opened_source:
        source = ImageOps.exif_transpose(opened_source)
        source_size = source.size
    with Image.open(generated_path) as opened_generated:
        generated = ImageOps.exif_transpose(opened_generated).convert("RGB")
        provider_size = generated.size

    source_ratio = source_size[0] / max(1, source_size[1])
    provider_ratio = provider_size[0] / max(1, provider_size[1])
    crop_box = (0, 0, provider_size[0], provider_size[1])
    crop_mode = "provider_already_matches_source"
    padding_content = {
        "detected": False,
        "ink_pixels": 0,
        "ink_ratio": 0.0,
        "minimum_ink_pixels": 0,
        "guard_px": 0,
        "ink_bbox": None,
    }

    # A relay may honour the source ratio despite receiving the wrapper.  In
    # that case cropping by square-wrapper coordinates would incorrectly cut
    # valid content, so use the full response.
    ratio_delta = abs(provider_ratio - source_ratio) / max(source_ratio, 1e-9)
    if framing.get("applied") and ratio_delta > 0.015:
        canvas_width, canvas_height = framing["canvas_size"]
        left, top, width, height = framing["content_box"]
        scale_x = provider_size[0] / max(1, canvas_width)
        scale_y = provider_size[1] / max(1, canvas_height)
        crop_box = (
            max(0, round(left * scale_x)),
            max(0, round(top * scale_y)),
            min(provider_size[0], round((left + width) * scale_x)),
            min(provider_size[1], round((top + height) * scale_y)),
        )
        crop_mode = "letterbox_content_box"

        # Ignore a narrow antialiasing halo around the expected content box,
        # then look for a meaningful amount of real ink in the technical
        # padding.  Fixed cropping is forbidden when the provider has moved
        # content there because that would silently delete labels/structures.
        generated_ink = ink_mask(generated)
        total_ink_pixels = generated_ink.histogram()[255]
        guard_px = max(2, round(min(provider_size) * ASPECT_PADDING_GUARD_RATIO))
        protected_box = (
            max(0, crop_box[0] - guard_px),
            max(0, crop_box[1] - guard_px),
            min(provider_size[0], crop_box[2] + guard_px),
            min(provider_size[1], crop_box[3] + guard_px),
        )
        padding_zone = Image.new("L", provider_size, 255)
        if protected_box[2] > protected_box[0] and protected_box[3] > protected_box[1]:
            ImageDraw.Draw(padding_zone).rectangle(
                (
                    protected_box[0],
                    protected_box[1],
                    protected_box[2] - 1,
                    protected_box[3] - 1,
                ),
                fill=0,
            )
        padding_ink = ImageChops.multiply(generated_ink, padding_zone)
        padding_ink_pixels = padding_ink.histogram()[255]
        minimum_ink_pixels = max(
            ASPECT_PADDING_MIN_INK_PIXELS,
            round(total_ink_pixels * ASPECT_PADDING_MIN_INK_RATIO),
        )
        padding_ink_bbox = padding_ink.getbbox()
        padding_detected = bool(
            padding_ink_bbox and padding_ink_pixels >= minimum_ink_pixels
        )
        padding_content = {
            "detected": padding_detected,
            "ink_pixels": padding_ink_pixels,
            "ink_ratio": round(padding_ink_pixels / max(1, total_ink_pixels), 6),
            "minimum_ink_pixels": minimum_ink_pixels,
            "guard_px": guard_px,
            "ink_bbox": list(padding_ink_bbox) if padding_ink_bbox else None,
        }
        if padding_detected:
            generated_ink_bbox = generated_ink.getbbox() or (0, 0, *provider_size)
            safe_margin = max(
                4,
                round(min(provider_size) * ASPECT_PADDING_SAFE_MARGIN_RATIO),
            )
            crop_box = (
                max(0, min(crop_box[0], generated_ink_bbox[0] - safe_margin)),
                max(0, min(crop_box[1], generated_ink_bbox[1] - safe_margin)),
                min(provider_size[0], max(crop_box[2], generated_ink_bbox[2] + safe_margin)),
                min(provider_size[1], max(crop_box[3], generated_ink_bbox[3] + safe_margin)),
            )
            crop_mode = (
                "provider_canvas_preserved_for_padding_content"
                if crop_box == (0, 0, provider_size[0], provider_size[1])
                else "expanded_for_padding_content"
            )
    elif ratio_delta > 0.015:
        # Defensive fallback for callers that do not have framing metadata.
        if provider_ratio > source_ratio:
            target_width = max(1, round(provider_size[1] * source_ratio))
            offset = (provider_size[0] - target_width) // 2
            crop_box = (offset, 0, offset + target_width, provider_size[1])
        else:
            target_height = max(1, round(provider_size[0] / source_ratio))
            offset = (provider_size[1] - target_height) // 2
            crop_box = (0, offset, provider_size[0], offset + target_height)
        crop_mode = "center_crop_fallback"

    cropped = generated.crop(crop_box)
    preserve_generated_geometry = bool(padding_content["detected"])
    if preserve_generated_geometry:
        normalized = cropped
    elif cropped.size != source_size:
        normalized = cropped.resize(source_size, Image.Resampling.LANCZOS)
    else:
        normalized = cropped
    suffix = generated_path.suffix.lower()
    image_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}.get(suffix, "PNG")
    temporary = generated_path.with_name(f"{generated_path.stem}.aspect-normalized{generated_path.suffix}")
    save_options = {"quality": 95} if image_format != "PNG" else {"optimize": True}
    normalized.save(temporary, format=image_format, **save_options)
    temporary.replace(generated_path)
    return {
        "status": "normalized",
        "crop_mode": crop_mode,
        "provider_size": list(provider_size),
        "provider_aspect_ratio": provider_ratio,
        "crop_box": list(crop_box),
        "normalized_size": list(normalized.size),
        "normalized_aspect_ratio": normalized.width / max(1, normalized.height),
        "source_size": list(source_size),
        "source_aspect_ratio": source_ratio,
        "padding_content": padding_content,
        "provider_canvas_allowed": preserve_generated_geometry,
    }


def write_report(path: Path, style: dict[str, Any], source_rows: list[dict[str, Any]], redraw_rows: list[dict[str, Any]]) -> None:
    mechanism_profile = style.get("edit_profile") == MECHANISM_ARROW_STRAIGHTEN_PROFILE
    color_policy = (
        "preserve the source blue, black, and magenta/purple arrow classes; all other pixels are locked."
        if mechanism_profile
        else "crisp black scientific line art on a pure white background."
    )
    integrity_pass = sum(
        1 for row in redraw_rows if (row.get("chemistry_integrity") or {}).get("status") == "pass"
    )
    integrity_manual = sum(
        1
        for row in redraw_rows
        if (row.get("chemistry_integrity") or {}).get("status") == "needs_human_arrow_check"
    )
    integrity_failed = sum(
        1 for row in redraw_rows if (row.get("chemistry_integrity") or {}).get("status") == "failed"
    )
    lines = [
        "# Figure Redraw Report",
        "",
        f"- Style preset: {style['style_name']}",
        f"- Model: {style['model']}",
        f"- Quality: {style['quality']}",
        f"- Background: {style['background']}",
        f"- Output format: {style['output_format']}",
        f"- Render mode: {style['render_mode']}",
        f"- Edit profile: {style.get('edit_profile', 'standard')}",
        f"- Color policy: {color_policy}",
        "- Typography policy: OCR is disabled; source-faithful modes preserve source pixels and AI modes rely on source-pixel geometry checks.",
        "- Scientific policy: atoms, rings, bond orders, stereochemistry, charges, radicals, labels, and process topology are immutable.",
        "",
        f"- Source candidates processed: {len(source_rows)}",
        f"- Source candidates resolved: {sum(1 for r in source_rows if r.get('status') == 'resolved')}",
        f"- Redraw success: {sum(1 for r in redraw_rows if r.get('status') == 'redrawn')}",
        f"- Redraw skipped/failed: {sum(1 for r in redraw_rows if r.get('status') != 'redrawn')}",
        f"- Chemistry integrity passed: {integrity_pass}",
        f"- Chemistry integrity awaiting arrow-by-arrow review: {integrity_manual}",
        f"- Chemistry integrity rejected: {integrity_failed}",
        "",
        "## Mandatory Human Check",
        "",
        "Every redrawn figure must be checked against its source PDF before use.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_manifest_rows(existing: list[dict[str, Any]], updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updates_by_id = {str(row.get("figure_id")): row for row in updates if row.get("figure_id")}
    merged = [updates_by_id.pop(str(row.get("figure_id")), row) for row in existing if isinstance(row, dict)]
    merged.extend(updates_by_id.values())
    return merged


def run(args: argparse.Namespace) -> int:
    force_standard_ai_edit = bool(getattr(args, "force_standard_ai_edit", False))
    review_root = Path(args.review_root).resolve()
    args.edit_profile = getattr(args, "edit_profile", "standard")
    requested_figure_type = normalize_figure_type(getattr(args, "figure_type", FIGURE_TYPE_AUTO))
    load_dotenv(review_root)
    if not args.base_url:
        args.base_url = default_base_url()
    if not args.wire_api:
        args.wire_api = default_wire_api()
    fallback_provider = image_fallback_config()
    project = ensure_project_dir(review_root, args.project_id)
    tesseract_command = resolve_tesseract_command(review_root, args.tesseract_cmd)
    image_field = resolve_image_field(getattr(args, "image_field", ""), args.base_url)
    images_transport = getattr(args, "images_transport", "urllib")
    figures_file = load_candidate_file(review_root, args.project_id, args.figures_file)
    out_dir = project / "03_figure_redraw"
    source_dir = out_dir / "source"
    redrawn_dir = out_dir / "redrawn"
    source_dir.mkdir(parents=True, exist_ok=True)
    redrawn_dir.mkdir(parents=True, exist_ok=True)
    data = read_json(figures_file)
    figures = data.get("figures") if isinstance(data, dict) else data
    if not isinstance(figures, list):
        raise SystemExit(f"Invalid figure candidates structure: {figures_file}")
    figure_id = getattr(args, "figure_id", "")
    figures = [figure for figure in figures if isinstance(figure, dict)]
    if figure_id or args.paper_id:
        figures = select_requested_figures(figures, figure_id=figure_id, paper_id=args.paper_id)
        if not figures:
            requested = f"figure: {figure_id}" if figure_id else f"paper: {args.paper_id}"
            raise SystemExit(f"No figure candidates found for {requested}")
    style = {
        "style_name": args.style_name,
        "model": args.model,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "base_url": args.base_url,
        "wire_api": args.wire_api,
        "render_mode": args.render_mode,
        "edit_profile": args.edit_profile,
        "requested_figure_type": requested_figure_type,
        "ocr_language": args.ocr_language,
        "tesseract_command": tesseract_command,
        "image_field": image_field,
        "images_transport": images_transport,
        "fallback_provider": {
            "enabled": bool(fallback_provider.get("api_key")),
            "base_url": fallback_provider.get("base_url", ""),
            "wire_api": fallback_provider.get("wire_api", ""),
            "model": fallback_provider.get("model", ""),
        },
        "dry_run": bool(args.dry_run),
    }
    write_json(out_dir / "style_config.json", style)
    api_key = resolve_api_key(args.api_key, args.base_url)
    if args.edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE and args.render_mode != "ai-edit":
        raise SystemExit(
            "mechanism-arrow-straighten requires --render-mode ai-edit so source colors and text remain available to the editor."
        )
    source_rows: list[dict[str, Any]] = []
    redraw_rows: list[dict[str, Any]] = []
    limit = args.limit if args.limit and args.limit > 0 else len(figures)
    for index, figure in enumerate(figures[:limit], start=1):
        if not isinstance(figure, dict):
            continue
        # A direct Figures-page redraw must use its exact candidate instead of
        # replacing it with the paper-level Figure Review default.
        if not figure_id:
            figure = human_selected_figure(project, figure)
        requested_render_mode = args.render_mode
        effective_render_mode = requested_render_mode
        if figure.get("recommended_action") == "retable":
            continue
        figure_id = str(figure.get("figure_id") or f"F{index:03d}")
        source_image, notes = resolve_source_image(review_root, figure)
        classification = classify_chemical_figure(
            figure,
            source_image,
            requested_type=requested_figure_type,
        )
        figure_type = str(classification["figure_type"])
        effective_edit_profile = edit_profile_for_figure_type(figure_type, effective_render_mode)
        src_row = {
            "figure_id": figure_id,
            "section_id": figure.get("section_id"),
            "section_heading": figure.get("section_heading"),
            "target_paragraph_id": figure.get("target_paragraph_id") or figure.get("paragraph_id"),
            "paper_id": figure.get("paper_id"),
            "source_label": figure.get("source_label"),
            "source_type": figure.get("source_type"),
            "resolved_source_image": str(source_image) if source_image else None,
            "source_pdf": figure.get("source_pdf"),
            "source_page_hint": figure.get("source_page_hint"),
            "source_caption_text": figure.get("source_caption_text") or notes.get("matched_caption"),
            "recommended_action": figure.get("recommended_action"),
            "human_selected_source": bool(figure.get("human_selected_source")),
            "human_selected_candidate_index": figure.get("human_selected_candidate_index"),
            "status": "resolved" if source_image else "unresolved",
            "notes": notes,
        }
        source_rows.append(src_row)
        redraw_row = {
            "figure_id": figure_id,
            "section_id": figure.get("section_id"),
            "section_heading": figure.get("section_heading"),
            "target_paragraph_id": figure.get("target_paragraph_id") or figure.get("paragraph_id"),
            "paper_id": figure.get("paper_id"),
            "source_label": figure.get("source_label"),
            "source_type": figure.get("source_type"),
            "source_caption_text": figure.get("source_caption_text") or notes.get("matched_caption"),
            "source_image": str(source_image) if source_image else None,
            "redrawn_image": None,
            "prompt": None,
            "model": args.model,
            "quality": args.quality,
            "background": args.background,
            "output_format": args.output_format,
            "render_mode": effective_render_mode,
            "edit_profile": effective_edit_profile,
            "figure_type": figure_type,
            "figure_type_classification": classification,
            "requires_human_chemistry_approval": bool(classification.get("requires_human_approval")),
            "color_policy": (
                "preserve_source_arrow_colors"
                if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE
                else "remove_nonsemantic_fills_preserve_scientific_colors"
                if figure_type == FIGURE_TYPE_COLORED
                else "structure_locked_clarity_and_color_cleanup"
            ),
            "typography_protection": (
                "source_pixel_layout_preserved"
                if effective_render_mode in SOURCE_FAITHFUL_RENDER_MODES
                else "ocr_masked_source_glyphs_restored_once"
                if effective_render_mode == "ocr-hollow-ai"
                else "source_ocr_regions_restored"
                if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE
                else "ai_edit_requested_not_guaranteed"
            ),
            "human_selected_source": bool(figure.get("human_selected_source")),
            "human_selected_candidate_index": figure.get("human_selected_candidate_index"),
            "status": "skipped",
            "needs_human_check": True,
            "ocr_source_text": "",
            "ocr_output_text": "",
            "missing_ocr_tokens": [],
            "ocr_check_status": "not_run",
            "ocr_source_status": "not_run",
            "ocr_output_status": "not_run",
            "ocr_text_protection": "not_run",
            "notes": "",
        }
        if classification.get("selection") == "manual" or force_standard_ai_edit:
            redraw_row["render_mode_override"] = {
                "status": "human_selected_figure_type",
                "requested_render_mode": requested_render_mode,
                "requested_figure_type": figure_type,
                "reason": (
                    "Reviewer explicitly selected the figure type and AI redraw strategy; "
                    "high-risk scientific figures remain subject to manual chemistry approval."
                ),
            }
        if not source_image:
            redraw_row["status"] = "source_unresolved"
            redraw_row["notes"] = "Could not resolve source image from figure candidate or content_list."
            redraw_rows.append(redraw_row)
            continue
        copied_source = source_dir / f"{figure_id}{source_image.suffix.lower() or '.png'}"
        copied_source.write_bytes(source_image.read_bytes())
        source_ocr = {"status": "not_run", "text": ""}
        source_boxes: list[dict[str, Any]] = []
        edit_input = copied_source
        if effective_render_mode == "ocr-hollow-ai":
            source_ocr = extract_ocr_text(copied_source, args.ocr_language, command=tesseract_command)
            redraw_row["ocr_source_text"] = source_ocr["text"]
            redraw_row["ocr_source_status"] = source_ocr["status"]
        if effective_render_mode == "ocr-hollow-ai":
            source_regions = ocr_region_boxes(copied_source, args.ocr_language, command=tesseract_command)
            source_boxes = source_regions["boxes"]
            if source_regions["status"] == "ok" and source_regions["text"].strip():
                source_ocr = source_regions
                redraw_row["ocr_source_text"] = source_regions["text"]
                redraw_row["ocr_source_status"] = source_regions["status"]
        if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE:
            # The provider receives the original image without a mask. A mask can
            # miss short or connected curved arrows and would otherwise block a
            # requested edit before it reaches the model. The strict prompt and
            # source-pixel geometry gate remain mandatory after generation.
            redraw_row["mechanism_edit_mask"] = {
                "status": "disabled",
                "reason": "Whole-image mechanism edit explicitly enabled; no API mask or source compositing applied.",
            }
        if effective_render_mode == "ocr-hollow-ai":
            edit_input = source_dir / f"{figure_id}-ocr-masked.png"
            mask_ocr_regions(copied_source, edit_input, source_boxes)
        if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE:
            # Mechanism edits do not invoke OCR. Chemical bonds and labels are too
            # often misclassified, so acceptance relies on the source-pixel gate.
            prompt = build_mechanism_arrow_straighten_prompt(figure)
            redraw_row["ocr_prompt_injected"] = False
            redraw_row["ocr_disabled_for_mechanism"] = True
        else:
            prompt = (build_ocr_hollow_prompt if effective_render_mode == "ocr-hollow-ai" else build_prompt)(
                args.style_name,
                figure,
                source_ocr["text"] if effective_render_mode == "ocr-hollow-ai" and source_ocr["status"] == "ok" else "",
                figure_type,
            )
        aspect_framing: dict[str, Any] = {}
        api_edit_input = edit_input
        provider_canvas_allowed = standard_ai_edit_uses_provider_canvas(
            effective_render_mode,
            effective_edit_profile,
        )
        if effective_render_mode not in SOURCE_FAITHFUL_RENDER_MODES:
            framed_input = source_dir / f"{figure_id}-ai-edit-framed.png"
            aspect_framing = prepare_aspect_preserving_edit_input(edit_input, framed_input)
            api_edit_input = Path(str(aspect_framing["input_path"]))
            prompt = (
                append_provider_canvas_prompt(prompt, aspect_framing)
                if provider_canvas_allowed
                else append_aspect_ratio_prompt(prompt, aspect_framing)
            )
            redraw_row["aspect_ratio_input"] = aspect_framing
        if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE:
            # Repair text again at the final request boundary so a later prompt
            # suffix or platform-specific edit cannot reintroduce mojibake.
            prompt = repair_mechanism_prompt_text(prompt)
        safety_retry_prompt = build_academic_chemistry_safety_retry_prompt(
            figure_type,
            effective_edit_profile,
        )
        if effective_render_mode not in SOURCE_FAITHFUL_RENDER_MODES:
            safety_retry_prompt = (
                append_provider_canvas_prompt(safety_retry_prompt, aspect_framing)
                if provider_canvas_allowed
                else append_aspect_ratio_prompt(safety_retry_prompt, aspect_framing)
            )
        redraw_row["prompt"] = prompt
        if args.dry_run:
            redraw_row["status"] = "dry_run"
            redraw_row["notes"] = "API call skipped by --dry-run."
            redraw_rows.append(redraw_row)
            continue
        if effective_render_mode in {"ai-edit", "ocr-hollow-ai"} and not api_key:
            redraw_row["status"] = "missing_api_key"
            redraw_row["notes"] = "API key is not set. Pass --api-key or set OPENAI_API_KEY."
            redraw_rows.append(redraw_row)
            continue
        output_format = "png" if effective_render_mode in SOURCE_FAITHFUL_RENDER_MODES else args.output_format
        out_path = redrawn_dir / f"{figure_id}.{output_format}"
        try:
            mechanism_attempts: list[dict[str, Any]] = []
            colored_cleanup_attempts: list[dict[str, Any]] = []
            safety_moderation_retries: list[dict[str, Any]] = []
            max_attempts = (
                MECHANISM_MAX_EDIT_ATTEMPTS
                if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE
                else COLORED_CLEANUP_MAX_EDIT_ATTEMPTS
                if figure_type == FIGURE_TYPE_COLORED and effective_render_mode == "ai-edit"
                else 1
            )
            for attempt_number in range(1, max_attempts + 1):
                attempt_prompt = prompt
                if attempt_number > 1:
                    if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE:
                        attempt_prompt += (
                            " A previous attempt was rejected because source geometry changed. "
                            "Retry from the original upload, change only the arrow strokes, and preserve every molecule, "
                            "intermediate, label, colored step name, and non-arrow pixel."
                        )
                    elif figure_type == FIGURE_TYPE_COLORED:
                        attempt_prompt += (
                            " A previous attempt was rejected because most broad decorative colour fill remained. "
                            "Retry from the original upload. Fully whiten the interiors of all solid cyan/teal/coloured "
                            "benzene and fused-ring fills while preserving every black perimeter, internal double bond, "
                            "heteroatom, substituent, R-group, panel label, and all relative geometry."
                        )
                if effective_render_mode in SOURCE_FAITHFUL_RENDER_MODES:
                    rendering = (
                        render_source_faithful_bw(copied_source, out_path)
                        if effective_render_mode == "source-faithful-bw"
                        else render_source_faithful_outline_color(copied_source, out_path)
                        if effective_render_mode == "source-faithful-outline-color"
                        else render_source_faithful_color(copied_source, out_path)
                    )
                    redraw_row["rendering"] = rendering
                elif args.wire_api == "responses":
                    response = call_with_academic_safety_retry(
                        lambda request_prompt: call_responses_image_edit(
                            api_key,
                            args.base_url,
                            api_edit_input,
                            request_prompt,
                            args.model,
                            args.quality,
                            args.background,
                            args.output_format,
                        ),
                        attempt_prompt,
                        safety_retry_prompt,
                        safety_moderation_retries,
                        attempt_number,
                    )
                    save_response_redrawn_image(response, out_path)
                elif args.wire_api == "chat-completions":
                    response = call_with_academic_safety_retry(
                        lambda request_prompt: call_chat_completions_image_edit(
                            api_key,
                            args.base_url,
                            api_edit_input,
                            request_prompt,
                            args.model,
                        ),
                        attempt_prompt,
                        safety_retry_prompt,
                        safety_moderation_retries,
                        attempt_number,
                    )
                    save_chat_completion_redrawn_image(response, out_path)
                else:
                    try:
                        response = call_with_academic_safety_retry(
                            lambda request_prompt: call_images_edit(
                                api_key,
                                args.base_url,
                                api_edit_input,
                                request_prompt,
                                args.model,
                                args.quality,
                                args.background,
                                args.output_format,
                                image_field,
                                images_transport,
                            ),
                            attempt_prompt,
                            safety_retry_prompt,
                            safety_moderation_retries,
                            attempt_number,
                        )
                        save_redrawn_image(response, out_path)
                    except RuntimeError as primary_error:
                        can_fail_over = (
                            effective_edit_profile == "standard"
                            and fallback_provider.get("api_key")
                            and should_fail_over_image_provider(primary_error)
                        )
                        if not can_fail_over:
                            raise
                        try:
                            fallback_response = call_with_academic_safety_retry(
                                lambda request_prompt: call_chat_completions_image_edit(
                                    fallback_provider["api_key"],
                                    fallback_provider["base_url"],
                                    api_edit_input,
                                    request_prompt,
                                    fallback_provider["model"],
                                ),
                                attempt_prompt,
                                safety_retry_prompt,
                                safety_moderation_retries,
                                attempt_number,
                            )
                            save_chat_completion_redrawn_image(fallback_response, out_path)
                        except RuntimeError as fallback_error:
                            raise RuntimeError(
                                "Primary image provider was unavailable and the configured "
                                f"Chat Completions fallback also failed: {fallback_error}"
                            ) from fallback_error
                        redraw_row["provider_failover"] = {
                            "status": "used",
                            "reason": "primary_provider_unavailable",
                            "primary_base_url": args.base_url,
                            "primary_wire_api": args.wire_api,
                            "fallback_base_url": fallback_provider["base_url"],
                            "fallback_wire_api": fallback_provider["wire_api"],
                            "fallback_model": fallback_provider["model"],
                        }
                if effective_render_mode not in SOURCE_FAITHFUL_RENDER_MODES and not provider_canvas_allowed:
                    redraw_row["aspect_ratio_normalization"] = normalize_generated_aspect(
                        out_path,
                        copied_source,
                        aspect_framing,
                    )
                elif provider_canvas_allowed:
                    with Image.open(out_path) as provider_output:
                        provider_size = list(provider_output.size)
                    redraw_row["aspect_ratio_normalization"] = {
                        "status": "provider_canvas_preserved",
                        "provider_size": provider_size,
                        "source_size": list(aspect_framing.get("source_size") or []),
                        "reason": "Standard ai-edit outputs retain the complete provider canvas without cropping or stretching.",
                    }
                if effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE:
                    source_fidelity = mechanism_source_fidelity_check(copied_source, out_path)
                    attempt_failures = list(source_fidelity["failures"])
                    mechanism_attempts.append(
                        {
                            "attempt": attempt_number,
                            "status": "failed" if attempt_failures else "pass",
                            "failures": attempt_failures,
                            "source_fidelity": source_fidelity,
                        }
                    )
                    if not attempt_failures:
                        break
                    continue
                if figure_type == FIGURE_TYPE_COLORED and effective_render_mode == "ai-edit":
                    cleanup = colored_fill_cleanup_check(copied_source, out_path)
                    colored_cleanup_attempts.append(
                        {
                            "attempt": attempt_number,
                            "status": cleanup["status"],
                            "failures": cleanup["failures"],
                            "cleanup": cleanup,
                        }
                    )
                    if cleanup["status"] == "pass":
                        break
                    continue
                else:
                    break
            if mechanism_attempts:
                redraw_row["mechanism_edit_attempts"] = mechanism_attempts
                redraw_row["mechanism_source_fidelity"] = mechanism_attempts[-1]["source_fidelity"]
            if colored_cleanup_attempts:
                redraw_row["colored_fill_cleanup_attempts"] = colored_cleanup_attempts
                redraw_row["colored_fill_cleanup"] = colored_cleanup_attempts[-1]["cleanup"]
            if safety_moderation_retries:
                redraw_row["safety_moderation_retries"] = safety_moderation_retries
            redraw_row["redrawn_image"] = str(out_path)
            redraw_row["status"] = "redrawn"
            if mechanism_attempts and mechanism_attempts[-1]["status"] != "pass":
                redraw_row["output_disposition"] = "saved_with_integrity_warning"
                redraw_row["notes"] = "Saved to Redrawn Output after mechanism integrity retries: " + "; ".join(
                    mechanism_attempts[-1]["failures"]
                )
            if colored_cleanup_attempts and colored_cleanup_attempts[-1]["status"] != "pass":
                redraw_row["output_disposition"] = "saved_with_integrity_warning"
                redraw_row["notes"] = "Saved to Redrawn Output after colour-fill cleanup retries: " + "; ".join(
                    colored_cleanup_attempts[-1]["failures"]
                )
            if effective_render_mode == "ocr-hollow-ai":
                generated_ocr = ocr_region_boxes(out_path, args.ocr_language, command=tesseract_command)
                # Any model text is erased before source glyphs are restored once.
                if generated_ocr["boxes"]:
                    mask_ocr_regions(out_path, out_path, generated_ocr["boxes"])
                restore_source_text_regions(copied_source, out_path, source_boxes)
                redraw_row["ocr_text_protection"] = (
                    "source_regions_restored" if source_boxes else "not_available"
                )
            if effective_render_mode == "ocr-hollow-ai":
                fidelity = restore_missing_source_ink(copied_source, out_path)
                redraw_row["content_fidelity"] = fidelity
                structure = structural_fidelity_check(copied_source, out_path)
                redraw_row["structural_fidelity"] = structure
                if fidelity["status"] != "pass" or structure["status"] != "pass":
                    redraw_row["output_disposition"] = "saved_with_integrity_warning"
                    redraw_row["notes"] = "Saved to Redrawn Output with fidelity warning: " + (
                        f"missing source marks {fidelity['unrecoverable_component_count']} components / "
                        f"{fidelity['unrecoverable_pixel_count']} pixels; "
                        f"new or displaced output ink {structure['unmatched_output_ink_pixels']} pixels "
                        f"(limit {structure['max_unmatched_output_ink_pixels']})."
                    )
            elif effective_render_mode == "ai-edit" and effective_edit_profile == "standard":
                redraw_row["structural_fidelity"] = structural_fidelity_check(copied_source, out_path)
            if effective_render_mode == "ocr-hollow-ai":
                output_ocr = extract_ocr_text(out_path, args.ocr_language, command=tesseract_command)
                redraw_row["ocr_output_text"] = output_ocr["text"]
                redraw_row["ocr_output_status"] = output_ocr["status"]
                if source_ocr["status"] == "ok" and output_ocr["status"] == "ok":
                    comparison = compare_ocr_text(source_ocr["text"], output_ocr["text"])
                    redraw_row["missing_ocr_tokens"] = comparison["missing_tokens"]
                    redraw_row["ocr_check_status"] = comparison["status"]
                elif source_ocr["status"] != "ok":
                    redraw_row["ocr_check_status"] = "not_available"
                else:
                    redraw_row["ocr_check_status"] = "needs_human_check"
            integrity = chemistry_integrity_gate(redraw_row)
            redraw_row["chemistry_integrity"] = integrity
            if redraw_row["status"] == "redrawn" and integrity["status"] == "failed":
                redraw_row["output_disposition"] = "saved_with_integrity_warning"
                redraw_row["notes"] = "Saved to Redrawn Output with chemistry integrity warning: " + "; ".join(integrity["failures"])
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            if safety_moderation_retries:
                redraw_row["safety_moderation_retries"] = safety_moderation_retries
            retained = (
                effective_edit_profile == MECHANISM_ARROW_STRAIGHTEN_PROFILE
                and retain_successful_mechanism_attempt_after_retry_error(
                    redraw_row,
                    out_path,
                    mechanism_attempts,
                    exc,
                )
            )
            if (
                not retained
                and figure_type == FIGURE_TYPE_COLORED
                and colored_cleanup_attempts
                and out_path.is_file()
            ):
                redraw_row["colored_fill_cleanup_attempts"] = colored_cleanup_attempts
                redraw_row["colored_fill_cleanup"] = colored_cleanup_attempts[-1]["cleanup"]
                redraw_row["colored_cleanup_retry_error"] = f"{type(exc).__name__}: {exc}"[-2000:]
                redraw_row["redrawn_image"] = str(out_path)
                redraw_row["status"] = "redrawn"
                redraw_row["output_disposition"] = "saved_with_integrity_warning"
                redraw_row["notes"] = (
                    "Kept the last generated coloured-chemistry image for human review after a later cleanup retry failed. "
                    + "; ".join(colored_cleanup_attempts[-1]["failures"])
                )
                redraw_row["chemistry_integrity"] = chemistry_integrity_gate(redraw_row)
                retained = True
            if not retained:
                redraw_row["status"] = "failed"
                redraw_row["notes"] = f"{type(exc).__name__}: {exc}"
        redraw_rows.append(redraw_row)
    current_redrawn_count = sum(1 for row in redraw_rows if row.get("status") == "redrawn")
    if args.paper_id or figure_id:
        previous_source = read_json(out_dir / "source_figure_manifest.json") if (out_dir / "source_figure_manifest.json").exists() else {}
        previous_redrawn = read_json(out_dir / "redrawn_figure_manifest.json") if (out_dir / "redrawn_figure_manifest.json").exists() else {}
        source_rows = merge_manifest_rows(previous_source.get("figures", []) if isinstance(previous_source, dict) else [], source_rows)
        redraw_rows = merge_manifest_rows(previous_redrawn.get("figures", []) if isinstance(previous_redrawn, dict) else [], redraw_rows)
    write_json(out_dir / "source_figure_manifest.json", {"project_id": args.project_id, "figures": source_rows})
    write_json(out_dir / "redrawn_figure_manifest.json", {"project_id": args.project_id, "figures": redraw_rows})
    write_report(out_dir / "figure_redraw_report.md", style, source_rows, redraw_rows)
    redrawn_count = current_redrawn_count if (args.paper_id or figure_id) else sum(
        1 for row in redraw_rows if row.get("status") == "redrawn"
    )
    if args.require_redrawn and redrawn_count == 0:
        raise SystemExit(
            "No figures were redrawn. Fix figure_candidates.json/source_image_path or rerun without --require-redrawn only if figures are explicitly skipped."
        )
    print(f"Wrote redraw outputs to {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Redraw review figure candidates into a unified style.")
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--figure-id", default="", help="Redraw exactly one figure candidate without paper-level source substitution.")
    parser.add_argument("--paper-id", default="", help="Redraw one paper after a Figure Review selection and preserve other manifest rows.")
    parser.add_argument("--figures-file", default="")
    parser.add_argument("--base-url", default="", help="Overrides OPENAI_BASE_URL from the process or project .env.")
    parser.add_argument(
        "--wire-api",
        choices=["images", "responses", "chat-completions"],
        default="",
        help="Image API transport; defaults to IMAGE_OPENAI_WIRE_API or images.",
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL)
    parser.add_argument("--quality", default="high")
    parser.add_argument("--background", default="opaque")
    parser.add_argument("--output-format", default="png")
    parser.add_argument("--ocr-language", default="eng", help="Tesseract language used for advisory AI-edit OCR checks.")
    parser.add_argument("--tesseract-cmd", default="", help="Optional path to a Tesseract executable.")
    parser.add_argument(
        "--image-field",
        default="",
        help="Multipart field name for the source image; leave blank for endpoint-based compatibility defaults.",
    )
    parser.add_argument(
        "--images-transport",
        choices=["urllib", "curl"],
        default="urllib",
        help="Multipart transport for image edits (default: urllib).",
    )
    parser.add_argument(
        "--render-mode",
        choices=["source-faithful-bw", "source-faithful-color", "source-faithful-outline-color", "ai-edit", "ocr-hollow-ai"],
        default="source-faithful-bw",
        help="Use ocr-hollow-ai for OCR-masked hollow black-and-white redraws with source glyph restoration.",
    )
    parser.add_argument(
        "--edit-profile",
        choices=["standard", MECHANISM_ARROW_STRAIGHTEN_PROFILE],
        default="standard",
        help="Use mechanism-arrow-straighten for a strict local edit that converts only curved process arrows.",
    )
    parser.add_argument(
        "--figure-type",
        default=FIGURE_TYPE_AUTO,
        help=(
            "Automatic or reviewer-selected scientific figure type. Supported values include auto, "
            "mechanism-cycle, simple-scheme, reaction-scope, complex-multipanel, low-resolution, "
            "colored-chemistry, data-table, scientific-plot, and general-scientific."
        ),
    )
    parser.add_argument(
        "--force-standard-ai-edit",
        action="store_true",
        help=(
            "Generate a reviewer-requested standard AI-edit alternative even when the "
            "default safety router would select a source-faithful mode."
        ),
    )
    parser.add_argument("--style-name", default=DEFAULT_STYLE_NAME)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-redrawn", action="store_true", help="Fail when no figure is redrawn successfully.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
