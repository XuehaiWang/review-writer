#!/usr/bin/env python3
"""Build a locked-down journal-style modern-survey PDF bundle with LuaLaTeX."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "review_writer_core").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfReader  # noqa: E402
from review_writer_core.latex_renderer import TEMPLATE_VERSION, render_tex  # noqa: E402
from review_writer_core.manuscript_state import build_manuscript_state  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sanitize_publication_markdown(markdown: Any) -> tuple[str, int]:
    """Remove unrecoverable decoder sentinels without blocking publication.

    U+FFFD does not identify the character that was lost upstream, so trying to
    guess a scientific symbol here would be unsafe. Replace it with a space to
    keep neighbouring tokens separate and record the intervention in the
    renderer audit instead.
    """

    source = str(markdown or "")
    count = source.count("\ufffd")
    return source.replace("\ufffd", " "), count


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def trusted_source(raw: Any) -> Path:
    source = Path(str(raw or "")).resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("A PDF render asset is not a trusted regular file.")
    return source


def materialize_assets(
    artifact_paths: dict[str, Any],
    bundle: Path,
) -> tuple[dict[str, str], str]:
    assets = bundle / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, str] = {}
    converter_version = "not-used"
    for index, (artifact_id, raw_path) in enumerate(sorted(artifact_paths.items())):
        source = trusted_source(raw_path)
        suffix = source.suffix.casefold()
        if suffix == ".svg":
            converter = shutil.which(
                str(os.environ.get("REVIEW_PDF_SVG_CONVERTER") or "rsvg-convert")
            )
            if not converter:
                raise RuntimeError(
                    "An approved SVG requires the configured offline rsvg-convert preprocessor."
                )
            destination = assets / f"asset-{index:04d}.pdf"
            result = subprocess.run(
                [converter, "--format=pdf", "--output", str(destination), str(source)],
                cwd=bundle,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            if result.returncode != 0 or not destination.is_file():
                raise RuntimeError("The controlled SVG-to-PDF conversion failed.")
            version = subprocess.run(
                [converter, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            converter_version = version.stdout.strip()[:200] or "rsvg-convert"
        elif suffix in {".png", ".jpg", ".jpeg", ".pdf"}:
            destination = assets / f"asset-{index:04d}{suffix}"
            shutil.copy2(source, destination)
        else:
            raise RuntimeError(f"Unsupported approved PDF asset type: {suffix or '<none>'}")
        resolved[str(artifact_id)] = str(destination.resolve())
    return resolved, converter_version


def compiler_version(compiler: str) -> str:
    result = subprocess.run(
        [compiler, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    return result.stdout.splitlines()[0][:240] if result.stdout else Path(compiler).name


def compile_tex(bundle: Path, tex_path: Path) -> tuple[Path, str, str]:
    compiler = shutil.which(str(os.environ.get("REVIEW_PDF_LUALATEX") or "lualatex"))
    if not compiler:
        raise RuntimeError(
            "LuaLaTeX is not installed in the PDF renderer runtime. Configure the pinned PDF worker before retrying."
        )
    output = bundle / "manuscript.pdf"
    logs: list[str] = []
    environment = {
        **os.environ,
        # LuaTeX 2025 loads its own Unicode tables through absolute paths.
        # Restricted mode permits those distribution files while still
        # rejecting unsafe relative traversal; output remains paranoid and
        # the worker is additionally read-only, unprivileged, and shell-free.
        "openin_any": "r",
        "openout_any": "p",
        "SOURCE_DATE_EPOCH": str(os.environ.get("SOURCE_DATE_EPOCH") or "1704067200"),
    }
    command = [
        compiler,
        "--no-shell-escape",
        "--interaction=nonstopmode",
        "--halt-on-error",
        "--file-line-error",
        f"--output-directory={bundle}",
        str(tex_path),
    ]
    for _pass in range(2):
        result = subprocess.run(
            command,
            cwd=bundle,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        logs.append(result.stdout)
        if result.returncode != 0:
            preview = result.stdout[-6000:]
            raise RuntimeError(f"LuaLaTeX compilation failed.\n{preview}")
    if not output.is_file() or output.stat().st_size < 1000:
        raise RuntimeError("LuaLaTeX produced no usable PDF.")
    return output, "\n\n".join(logs), compiler_version(compiler)


def _font_is_embedded(font: Any) -> bool:
    try:
        value = font.get_object()
        descendants = value.get("/DescendantFonts") or []
        candidates = [value, *(item.get_object() for item in descendants)]
        for candidate in candidates:
            descriptor = candidate.get("/FontDescriptor")
            descriptor = descriptor.get_object() if descriptor else None
            if descriptor and any(descriptor.get(key) for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                return True
    except Exception:
        return False
    return False


def visual_page_qa(pdf_path: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run portable page-geometry QA with PyMuPDF when it is installed."""

    try:
        import pymupdf as fitz
    except ImportError:
        return (
            "pypdf-structural-only",
            [],
            [{"type": "visual_backend_unavailable", "message": "PyMuPDF is unavailable; only structural PDF QA was run."}],
            [],
        )
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    thumbnail_dir = pdf_path.parent / "page-qa"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(str(pdf_path)) as document:
        for index, page in enumerate(document, start=1):
            page_rect = page.rect
            text_boxes = [
                fitz.Rect(block[:4])
                for block in page.get_text("blocks")
                if len(block) >= 4 and str(block[4] or "").strip()
            ]
            image_boxes: list[Any] = []
            image_instances: list[dict[str, Any]] = []
            for image in page.get_images(full=True):
                try:
                    rects = page.get_image_rects(image[0])
                except Exception:
                    continue
                pixel_width = int(image[2] or 0) if len(image) > 3 else 0
                pixel_height = int(image[3] or 0) if len(image) > 3 else 0
                for rect in rects:
                    image_boxes.append(rect)
                    dpi_x = pixel_width * 72.0 / max(1.0, rect.width)
                    dpi_y = pixel_height * 72.0 / max(1.0, rect.height)
                    image_instances.append(
                        {
                            "bbox": [round(value, 2) for value in (rect.x0, rect.y0, rect.x1, rect.y1)],
                            "pixel_width": pixel_width,
                            "pixel_height": pixel_height,
                            "effective_dpi": round(min(dpi_x, dpi_y), 1),
                        }
                    )
            boxes = [*text_boxes, *image_boxes]
            boxes = [box for box in boxes if not box.is_empty and not box.is_infinite]
            content = fitz.Rect()
            for box in boxes:
                content |= box
            page_area = max(1.0, page_rect.width * page_rect.height)
            occupied_area = sum(
                max(0.0, (box & page_rect).width * (box & page_rect).height)
                for box in boxes
            )
            coverage = min(1.0, occupied_area / page_area) if boxes else 0.0
            outside = bool(
                boxes
                and (
                    content.x0 < page_rect.x0 - 1
                    or content.y0 < page_rect.y0 - 1
                    or content.x1 > page_rect.x1 + 1
                    or content.y1 > page_rect.y1 + 1
                )
            )
            if outside:
                blockers.append({"type": "content_outside_page", "page": index, "message": "Rendered content exceeds the page boundary."})
            if coverage == 0:
                warnings.append({"type": "blank_page", "page": index, "message": "The rendered page contains no detected text or image blocks."})
            elif coverage < 0.08:
                warnings.append({"type": "excessive_blank_area", "page": index, "message": "The rendered page uses less than 8% of its available area."})
            bottom_blank_ratio = (
                max(0.0, page_rect.y1 - content.y1) / max(1.0, page_rect.height)
                if boxes
                else 1.0
            )
            if index < len(document) and boxes and bottom_blank_ratio > 0.48:
                warnings.append(
                    {
                        "type": "large_bottom_whitespace",
                        "page": index,
                        "ratio": round(bottom_blank_ratio, 4),
                        "message": "A non-final page leaves nearly half of the page empty; inspect float placement and column breaks.",
                    }
                )
            orphan_images = 0
            for image_box in image_boxes:
                nearby_caption = any(
                    text_box.y0 >= image_box.y1 - 4
                    and text_box.y0 <= image_box.y1 + 90
                    and min(text_box.x1, image_box.x1)
                    - max(text_box.x0, image_box.x0)
                    > 0
                    for text_box in text_boxes
                )
                if not nearby_caption:
                    orphan_images += 1
            if orphan_images:
                warnings.append(
                    {
                        "type": "image_caption_not_detected",
                        "page": index,
                        "image_count": orphan_images,
                        "message": "One or more image blocks have no nearby caption text; inspect figure/caption separation.",
                    }
                )
            overlap_pairs = 0
            for image_box in image_boxes:
                image_area = max(1.0, image_box.width * image_box.height)
                for text_box in text_boxes:
                    overlap = image_box & text_box
                    if (
                        not overlap.is_empty
                        and overlap.width * overlap.height / image_area > 0.08
                    ):
                        overlap_pairs += 1
            if overlap_pairs:
                warnings.append(
                    {
                        "type": "text_image_overlap",
                        "page": index,
                        "pair_count": overlap_pairs,
                        "message": "Rendered text overlaps a figure image beyond the layout tolerance.",
                    }
                )
            low_resolution = [
                item
                for item in image_instances
                if float(item.get("effective_dpi") or 0) < 120
                and int(item.get("pixel_width") or 0) > 0
                and int(item.get("pixel_height") or 0) > 0
            ]
            if low_resolution:
                warnings.append(
                    {
                        "type": "low_effective_image_resolution",
                        "page": index,
                        "image_count": len(low_resolution),
                        "minimum_effective_dpi": min(
                            item["effective_dpi"] for item in low_resolution
                        ),
                        "message": "One or more rendered images are below 120 effective DPI; use a higher-resolution source or reduce its printed size.",
                    }
                )
            thumbnail_path = thumbnail_dir / f"page-{index:03d}.png"
            try:
                page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False).save(
                    str(thumbnail_path)
                )
            except Exception:
                thumbnail_path = Path()
            pages.append(
                {
                    "page": index,
                    "content_coverage": round(coverage, 4),
                    "content_bbox": [round(value, 2) for value in (content.x0, content.y0, content.x1, content.y1)] if boxes else [],
                    "outside_page": outside,
                    "bottom_blank_ratio": round(bottom_blank_ratio, 4),
                    "text_block_count": len(text_boxes),
                    "image_block_count": len(image_boxes),
                    "orphan_image_count": orphan_images,
                    "text_image_overlap_count": overlap_pairs,
                    "low_resolution_image_count": len(low_resolution),
                    "image_instances": image_instances,
                    "thumbnail_path": (
                        thumbnail_path.relative_to(pdf_path.parent).as_posix()
                        if thumbnail_path and thumbnail_path.is_file()
                        else ""
                    ),
                }
            )
    return "pymupdf", blockers, warnings, pages


def inspect_pdf(pdf_path: Path, log: str, state: dict[str, Any]) -> dict[str, Any]:
    reader = PdfReader(str(pdf_path))
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = list(
        state.get("validation", {}).get("warning_issues") or []
    )
    page_sizes = {
        (round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2))
        for page in reader.pages
    }
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not reader.pages:
        blockers.append({"type": "empty_pdf", "message": "The PDF has no pages."})
    if len(page_sizes) > 1:
        blockers.append({"type": "inconsistent_page_size", "message": "PDF page sizes are inconsistent."})
    if "�" in extracted:
        replacement_contexts: list[str] = []
        for match in re.finditer("�", extracted):
            start = max(0, match.start() - 50)
            end = min(len(extracted), match.end() + 50)
            context = re.sub(r"\s+", " ", extracted[start:end]).strip()
            if context and context not in replacement_contexts:
                replacement_contexts.append(context)
            if len(replacement_contexts) >= 5:
                break
        # Some embedded scientific glyphs are rendered correctly but expose
        # U+FFFD through a PDF font's ToUnicode map. That is a text-extraction
        # limitation, not sufficient evidence of a visually broken PDF. Keep
        # the diagnostic, but do not prevent an otherwise valid download.
        warnings.append(
            {
                "type": "pdf_text_extraction_replacement_character",
                "message": (
                    "PDF text extraction contains replacement characters; "
                    "the rendered PDF was retained and the contexts are "
                    "available for optional review."
                ),
                "count": extracted.count("�"),
                "contexts": replacement_contexts,
            }
        )
    extracted_casefold = extracted.casefold()
    internal_markers = sorted(
        marker
        for marker in (
            "paragraph_id",
            "inserted_figure",
            "target_paragraph_id",
            "output_artifact_id",
        )
        if marker in extracted_casefold
    )
    if internal_markers:
        blockers.append(
            {
                "type": "internal_workflow_marker",
                "message": "Internal paragraph or figure-routing metadata leaked into the PDF.",
                "markers": internal_markers,
            }
        )
    if "undefined references" in log.casefold() or "undefined citation" in log.casefold():
        blockers.append({"type": "undefined_latex_reference", "message": "LuaLaTeX reported undefined references."})
    fonts: dict[str, bool] = {}
    for page in reader.pages:
        resources = page.get("/Resources") or {}
        font_map = resources.get("/Font") or {}
        font_map = font_map.get_object() if hasattr(font_map, "get_object") else font_map
        for name, font in font_map.items():
            fonts[str(name)] = _font_is_embedded(font)
    unembedded = sorted(name for name, embedded in fonts.items() if not embedded)
    if unembedded:
        blockers.append(
            {
                "type": "font_not_embedded",
                "message": "Every PDF font must be embedded.",
                "fonts": unembedded,
            }
        )
    visual_backend, visual_blockers, visual_warnings, page_visuals = visual_page_qa(pdf_path)
    blockers.extend(visual_blockers)
    warnings.extend(visual_warnings)
    return {
        "schema_version": 1,
        "status": "blocked" if blockers else "pass_with_warnings" if warnings else "pass",
        "page_count": len(reader.pages),
        "page_sizes": [list(value) for value in sorted(page_sizes)],
        "font_count": len(fonts),
        "all_fonts_embedded": not unembedded,
        "replacement_character_count": extracted.count("�"),
        "visual_backend": visual_backend,
        "page_visuals": page_visuals,
        "blocking_issues": blockers,
        "warning_issues": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    bundle = Path(args.output_dir).resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    artifact_paths, converter_version = materialize_assets(
        dict(payload.get("artifact_paths") or {}), bundle
    )
    final_markdown, sanitized_replacement_count = sanitize_publication_markdown(
        payload.get("final_markdown")
    )
    state = build_manuscript_state(final_markdown, artifact_paths=artifact_paths)
    if sanitized_replacement_count:
        state.setdefault("validation", {}).setdefault("warning_issues", []).append(
            {
                "type": "source_unicode_replacement_character_sanitized",
                "message": (
                    "Unrecoverable Unicode replacement characters were changed "
                    "to spaces before PDF layout."
                ),
                "count": sanitized_replacement_count,
            }
        )
    if not state.get("validation", {}).get("valid"):
        raise RuntimeError(
            "Final Manuscript State failed publication gates: "
            + json.dumps(state["validation"]["blocking_issues"], ensure_ascii=False)
        )
    profile = str(payload.get("language_profile") or "en")
    template_path = ROOT / "review_writer_core" / "resources" / "pdf" / "modern-survey.tex"
    template = template_path.read_text(encoding="utf-8")
    tex = render_tex(state, profile=profile, template=template)
    tex_path = bundle / "manuscript.tex"
    tex_path.write_text(tex, encoding="utf-8")
    pdf_path, compile_log, compiler = compile_tex(bundle, tex_path)
    (bundle / "compile.log").write_text(compile_log, encoding="utf-8")
    qa = inspect_pdf(pdf_path, compile_log, state)
    if qa["blocking_issues"]:
        raise RuntimeError(
            "PDF deterministic QA failed: "
            + json.dumps(qa["blocking_issues"], ensure_ascii=False)
        )
    manifest = {
        "schema_version": 1,
        "template": "modern-survey",
        "template_version": TEMPLATE_VERSION,
        "language_profile": profile,
        "compiler": compiler,
        "shell_escape": False,
        "source_final_artifact_id": payload.get("source_final_artifact_id"),
        "source_release_artifact_id": payload.get("source_release_artifact_id"),
        "source_markdown_sha256": state["source_markdown_sha256"],
        "semantic_sha256": state["semantic_sha256"],
        "template_sha256": sha256_file(template_path),
        "svg_converter": converter_version,
        "sanitized_source_replacement_character_count": sanitized_replacement_count,
        "asset_sha256": {
            artifact_id: sha256_file(Path(path))
            for artifact_id, path in artifact_paths.items()
        },
    }
    write_json(bundle / "manuscript_state.json", state)
    write_json(bundle / "render_manifest.json", manifest)
    write_json(bundle / "pdf_qa.json", qa)
    print(f"Rendered {pdf_path} ({qa['page_count']} pages, {profile}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
