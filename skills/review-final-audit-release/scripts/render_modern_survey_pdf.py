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
        import fitz
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
    with fitz.open(str(pdf_path)) as document:
        for index, page in enumerate(document, start=1):
            page_rect = page.rect
            boxes = [fitz.Rect(block[:4]) for block in page.get_text("blocks") if len(block) >= 4]
            for image in page.get_images(full=True):
                try:
                    boxes.extend(page.get_image_rects(image[0]))
                except Exception:
                    continue
            boxes = [box for box in boxes if not box.is_empty and not box.is_infinite]
            content = fitz.Rect()
            for box in boxes:
                content |= box
            coverage = (
                max(0.0, content.width * content.height) / max(1.0, page_rect.width * page_rect.height)
                if boxes
                else 0.0
            )
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
            pages.append(
                {
                    "page": index,
                    "content_coverage": round(coverage, 4),
                    "content_bbox": [round(value, 2) for value in (content.x0, content.y0, content.x1, content.y1)] if boxes else [],
                    "outside_page": outside,
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
        blockers.append(
            {
                "type": "unicode_replacement_character",
                "message": "The PDF contains replacement characters.",
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
    state = build_manuscript_state(
        str(payload.get("final_markdown") or ""), artifact_paths=artifact_paths
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
