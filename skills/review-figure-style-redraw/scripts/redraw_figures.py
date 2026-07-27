#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from collections import deque
import json
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageOps


SOURCE_FAITHFUL_SCALE_FACTOR = 4
SOURCE_FAITHFUL_DARK_INK_THRESHOLD = 180
CONTENT_INK_THRESHOLD = 192
CONTENT_MATCH_RADIUS = 3
MAX_RESTORED_COMPONENT_PIXELS = 1024
STRUCTURE_MATCH_RADIUS = 4
MAX_UNMATCHED_OUTPUT_INK_RATIO = 0.04
MIN_UNMATCHED_OUTPUT_INK_PIXELS = 64


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
    command_args = [command, str(image_path), "stdout", "-l", language]
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
            [command, str(image_path), "stdout", "-l", language, "tsv"],
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
    """Reject AI line art that introduces geometry not supported by the source image."""
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
    max_unmatched_output = max(
        MIN_UNMATCHED_OUTPUT_INK_PIXELS,
        round(source_ink_pixels * MAX_UNMATCHED_OUTPUT_INK_RATIO),
    )
    return {
        "status": "failed" if unmatched_output > max_unmatched_output else "pass",
        "match_radius_px": STRUCTURE_MATCH_RADIUS,
        "source_ink_pixels": source_ink_pixels,
        "unmatched_output_ink_pixels": unmatched_output,
        "unmatched_source_ink_pixels": unmatched_source,
        "max_unmatched_output_ink_pixels": max_unmatched_output,
    }


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
        upscaled = background.convert("RGB").resize(target_size, Image.Resampling.LANCZOS)
        grayscale = ImageOps.autocontrast(ImageOps.grayscale(upscaled))
        # Light color fills in source figures must become white, while dark colored
        # bonds and labels remain black ink.  A high threshold turns pale cyan and
        # magenta fills into opaque black blocks that hide structure interiors.
        black_and_white = grayscale.point(
            lambda value: 255 if value >= SOURCE_FAITHFUL_DARK_INK_THRESHOLD else 0,
            mode="1",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        black_and_white.save(output_path, format="PNG")
        return {
            "width": target_size[0],
            "height": target_size[1],
            "scale_factor": SOURCE_FAITHFUL_SCALE_FACTOR,
            "color_mode": "1-bit black and white",
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
    """Use the dedicated xiaoleai key when its OpenAI-compatible endpoint is selected."""
    if cli_value:
        return cli_value
    if "api.xiaoleai.team" in base_url:
        return os.environ.get("XIAOLEAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    return os.environ.get("OPENAI_API_KEY", "")


def resolve_image_field(configured: str, base_url: str) -> str:
    """Choose the multipart image field required by the selected OpenAI-compatible relay."""
    if configured.strip():
        return configured.strip()
    if "api.xiaoleai.team" in base_url:
        return "image"
    return "image[]"


def default_base_url() -> str:
    return os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")


def openai_api_url(base_url: str, endpoint: str) -> str:
    """Build an OpenAI-compatible v1 endpoint without duplicating a configured /v1 suffix."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{endpoint}"
    return f"{base}/v1{endpoint}"


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


def build_prompt(style_name: str, figure: dict[str, Any], ocr_text: str = "") -> str:
    ocr_constraint = ""
    if ocr_text.strip():
        ocr_constraint = (
            " OCR transcription (advisory integrity constraint): preserve every legible token below exactly; "
            "do not add, omit, or substitute it. OCR transcription: "
            f"{ocr_text.strip()[:2000]}"
        )
    return (
        "Restyle the input image into a unified high-quality organic chemistry review figure style. "
        "Preserve every chemical structure, bond connectivity, stereochemistry, atom label, substituent label, "
        "reaction arrow direction, reagent, catalyst, solvent, stoichiometry, temperature, time, yield, "
        "footnote marker, panel layout, numbering, and relative placement exactly as in the source. "
        "Do not add, remove, rename, reorder, summarize, or reinterpret any chemistry or text. "
        "If any source text is hard to read, preserve it faithfully rather than inventing new text. "
        "Change only visual style: use pure black lines on a white background with no color or grayscale fills. "
        "Keep every font family appearance, font size, baseline, text position, and panel geometry exactly unchanged. "
        "Keep the figure scientifically identical to the source. "
        f"Style preset: {style_name}.{ocr_constraint}"
    )


def build_ocr_hollow_prompt(style_name: str, figure: dict[str, Any], ocr_text: str = "") -> str:
    return (
        build_prompt(style_name, figure, ocr_text)
        + " The input has all original text regions masked. Do not generate any letters, numbers, labels, or symbols. "
        "Redraw only the non-text chemistry graphics in pure black on white. Convert every solid filled icon, node, arrowhead, "
        "or shape into a black outline with a white hollow interior, while preserving bond connectivity and geometry. "
        "Every original reaction mark, chemical symbol, bond, arrow, stereochemical mark, panel marker, and label is mandatory; "
        "never omit a mark even when it is small or visually ambiguous."
    )


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
        )
    fields = {
        "model": model,
        "prompt": prompt,
        "quality": quality,
        "background": background,
        "output_format": output_format,
    }
    content_type, body = build_multipart_form(fields, image_edit_file_fields(image_path, image_field))
    req = urllib.request.Request(
        openai_api_url(base_url, "/images/edits"),
        data=body,
        headers=build_headers(api_key, content_type),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Image edit request rejected: {http_error_details(exc)}") from exc


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
    config = f'header = "Authorization: Bearer {api_key}"\n'.encode("utf-8")
    try:
        result = runner(command, input=config, capture_output=True, timeout=310, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"curl image edit failed: {type(exc).__name__}: {exc}") from exc
    response_bytes = bytes(result.stdout or b"")
    response_text = response_bytes.decode("utf-8", "replace")
    if result.returncode != 0:
        raise RuntimeError(f"curl image edit failed (exit {result.returncode}): {response_text[:500]}")
    return json.loads(response_text)


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
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def write_report(path: Path, style: dict[str, Any], source_rows: list[dict[str, Any]], redraw_rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Figure Redraw Report",
        "",
        f"- Style preset: {style['style_name']}",
        f"- Model: {style['model']}",
        f"- Quality: {style['quality']}",
        f"- Background: {style['background']}",
        f"- Output format: {style['output_format']}",
        f"- Render mode: {style['render_mode']}",
        "- Color policy: pure black lines on a white background.",
        "- Typography policy: source pixel layout is preserved in source-faithful-bw mode.",
        "",
        f"- Source candidates processed: {len(source_rows)}",
        f"- Source candidates resolved: {sum(1 for r in source_rows if r.get('status') == 'resolved')}",
        f"- Redraw success: {sum(1 for r in redraw_rows if r.get('status') == 'redrawn')}",
        f"- Redraw skipped/failed: {sum(1 for r in redraw_rows if r.get('status') != 'redrawn')}",
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
    review_root = Path(args.review_root).resolve()
    load_dotenv(review_root)
    if not args.base_url:
        args.base_url = default_base_url()
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
        "ocr_language": args.ocr_language,
        "tesseract_command": tesseract_command,
        "image_field": image_field,
        "images_transport": images_transport,
        "dry_run": bool(args.dry_run),
    }
    write_json(out_dir / "style_config.json", style)
    api_key = resolve_api_key(args.api_key, args.base_url)
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
        if figure.get("recommended_action") == "retable":
            continue
        figure_id = str(figure.get("figure_id") or f"F{index:03d}")
        source_image, notes = resolve_source_image(review_root, figure)
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
            "render_mode": args.render_mode,
            "color_policy": "pure_black_and_white",
            "typography_protection": (
                "source_pixel_layout_preserved"
                if args.render_mode == "source-faithful-bw"
                else "ocr_masked_source_glyphs_restored_once"
                if args.render_mode == "ocr-hollow-ai"
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
            "notes": "",
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
        if args.render_mode in {"ai-edit", "ocr-hollow-ai"}:
            source_ocr = extract_ocr_text(copied_source, args.ocr_language, command=tesseract_command)
            redraw_row["ocr_source_text"] = source_ocr["text"]
            redraw_row["ocr_source_status"] = source_ocr["status"]
        if args.render_mode == "ocr-hollow-ai":
            source_regions = ocr_region_boxes(copied_source, args.ocr_language, command=tesseract_command)
            source_boxes = source_regions["boxes"]
            if source_regions["status"] == "ok":
                source_ocr = source_regions
                redraw_row["ocr_source_text"] = source_regions["text"]
                redraw_row["ocr_source_status"] = source_regions["status"]
            edit_input = source_dir / f"{figure_id}-ocr-masked.png"
            mask_ocr_regions(copied_source, edit_input, source_boxes)
        prompt = (build_ocr_hollow_prompt if args.render_mode == "ocr-hollow-ai" else build_prompt)(args.style_name, figure, source_ocr["text"] if source_ocr["status"] == "ok" else "")
        redraw_row["prompt"] = prompt
        if args.dry_run:
            redraw_row["status"] = "dry_run"
            redraw_row["notes"] = "API call skipped by --dry-run."
            redraw_rows.append(redraw_row)
            continue
        if args.render_mode in {"ai-edit", "ocr-hollow-ai"} and not api_key:
            redraw_row["status"] = "missing_api_key"
            redraw_row["notes"] = "API key is not set. Pass --api-key or set OPENAI_API_KEY."
            redraw_rows.append(redraw_row)
            continue
        output_format = "png" if args.render_mode == "source-faithful-bw" else args.output_format
        out_path = redrawn_dir / f"{figure_id}.{output_format}"
        try:
            if args.render_mode == "source-faithful-bw":
                rendering = render_source_faithful_bw(copied_source, out_path)
                redraw_row["rendering"] = rendering
            elif args.wire_api == "responses":
                response = call_responses_image_edit(
                    api_key,
                    args.base_url,
                    edit_input,
                    prompt,
                    args.model,
                    args.quality,
                    args.background,
                    args.output_format,
                )
                save_response_redrawn_image(response, out_path)
            else:
                response = call_images_edit(
                    api_key,
                    args.base_url,
                    edit_input,
                    prompt,
                    args.model,
                    args.quality,
                    args.background,
                    args.output_format,
                    image_field,
                    images_transport,
                )
                save_redrawn_image(response, out_path)
            redraw_row["redrawn_image"] = str(out_path)
            redraw_row["status"] = "redrawn"
            if args.render_mode == "ocr-hollow-ai":
                generated_ocr = ocr_region_boxes(out_path, args.ocr_language, command=tesseract_command)
                # Any model text is erased before source glyphs are restored once.
                if generated_ocr["boxes"]:
                    mask_ocr_regions(out_path, out_path, generated_ocr["boxes"])
                restore_source_text_regions(copied_source, out_path, source_boxes)
                fidelity = restore_missing_source_ink(copied_source, out_path)
                redraw_row["content_fidelity"] = fidelity
                structure = structural_fidelity_check(copied_source, out_path)
                redraw_row["structural_fidelity"] = structure
                if fidelity["status"] != "pass" or structure["status"] != "pass":
                    redraw_row["status"] = "structural_fidelity_failed"
                    redraw_row["redrawn_image"] = None
                    redraw_row["notes"] = "Rejected by fidelity gate: " + (
                        f"missing source marks {fidelity['unrecoverable_component_count']} components / "
                        f"{fidelity['unrecoverable_pixel_count']} pixels; "
                        f"new or displaced output ink {structure['unmatched_output_ink_pixels']} pixels "
                        f"(limit {structure['max_unmatched_output_ink_pixels']})."
                    )
            if args.render_mode in {"ai-edit", "ocr-hollow-ai"}:
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
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
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
    parser.add_argument("--review-root", default="/home/ps/review-writer")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--figure-id", default="", help="Redraw exactly one figure candidate without paper-level source substitution.")
    parser.add_argument("--paper-id", default="", help="Redraw one paper after a Figure Review selection and preserve other manifest rows.")
    parser.add_argument("--figures-file", default="")
    parser.add_argument("--base-url", default="", help="Overrides OPENAI_BASE_URL from the process or project .env.")
    parser.add_argument("--wire-api", choices=["images", "responses"], default="images")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--quality", default="high")
    parser.add_argument("--background", default="opaque")
    parser.add_argument("--output-format", default="png")
    parser.add_argument("--ocr-language", default="eng", help="Tesseract language used for advisory AI-edit OCR checks.")
    parser.add_argument("--tesseract-cmd", default="", help="Optional path to a Tesseract executable.")
    parser.add_argument(
        "--image-field",
        default="",
        help="Multipart field name for the source image (auto-detects image for xiaoleai; otherwise image[]).",
    )
    parser.add_argument(
        "--images-transport",
        choices=["urllib", "curl"],
        default="urllib",
        help="Multipart transport for image edits (default: urllib).",
    )
    parser.add_argument(
        "--render-mode",
        choices=["source-faithful-bw", "ai-edit", "ocr-hollow-ai"],
        default="source-faithful-bw",
        help="Use ocr-hollow-ai for OCR-masked gpt-image-2 hollow black-and-white redraws with source glyph restoration.",
    )
    parser.add_argument("--style-name", default="organic-review-clean-v1")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-redrawn", action="store_true", help="Fail when no figure is redrawn successfully.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
