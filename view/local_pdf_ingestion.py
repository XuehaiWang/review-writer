#!/usr/bin/env python3
"""Register locally uploaded PDFs in the canonical review-library format."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple


MAX_LOCAL_PDF_BYTES = 80 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 2_000_000
MINERU_UPLOAD_TIMEOUT_SECONDS = 35 * 60
MINERU_MAX_PAGES_PER_TASK = 200
_INGEST_LOCK = threading.RLock()
_METADATA_MODULE: Any | None = None
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


class MinerUPdfPart(NamedTuple):
    pdf_path: Path
    slug: str
    page_offset: int
    start_page: int
    end_page: int


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _field(value: Any, source: str, confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "confidence": confidence,
        "human_checked": False,
    }


def _clean_text(value: object) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_pdf_filename(filename: object) -> str:
    name = Path(str(filename or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).rstrip(". ")
    if not name or name in {".", ".."}:
        raise ValueError("The uploaded PDF needs a valid file name.")
    if Path(name).suffix.casefold() != ".pdf":
        raise ValueError("Only .pdf files can be uploaded.")
    stem = Path(name).stem[:180].strip(". ") or "uploaded-paper"
    return f"{stem}.pdf"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_local_pdf(path: Path) -> dict[str, Any]:
    path = Path(path)
    size = path.stat().st_size
    if size < 512:
        raise ValueError("The uploaded file is too small to be a valid PDF.")
    if size > MAX_LOCAL_PDF_BYTES:
        raise ValueError(f"Each PDF must be {MAX_LOCAL_PDF_BYTES // (1024 * 1024)} MB or smaller.")
    with path.open("rb") as stream:
        head = stream.read(1024).lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if not head.startswith(b"%PDF-"):
        raise ValueError("The uploaded file does not contain a PDF signature.")
    return {"size_bytes": size, "sha256": sha256_file(path)}


def _metadata_module(review_root: Path):
    global _METADATA_MODULE
    if _METADATA_MODULE is not None:
        return _METADATA_MODULE
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "review-metadata-prep"
        / "scripts"
        / "prepare_metadata.py"
    )
    if not script.is_file():
        raise RuntimeError(f"Metadata preparation script is missing: {script}")
    spec = importlib.util.spec_from_file_location("review_local_upload_metadata", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the canonical metadata preparation module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _METADATA_MODULE = module
    return module


def _pdf_reader(path: Path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "Local PDF parsing requires pypdf. Install requirements.txt in the active Python environment."
        ) from exc
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise ValueError("Password-protected PDFs must be unlocked before upload.")
    return reader


def _document_info(reader: Any) -> dict[str, str]:
    raw = reader.metadata or {}

    def get(*keys: str) -> str:
        for key in keys:
            value = getattr(raw, key, None)
            if not value and isinstance(raw, dict):
                value = raw.get(key) or raw.get("/" + key.lstrip("/"))
            cleaned = _clean_text(value)
            if cleaned:
                return cleaned
        return ""

    return {
        "title": get("title", "Title"),
        "author": get("author", "Author"),
        "subject": get("subject", "Subject"),
        "creation_date": get("creation_date", "CreationDate"),
    }


def _extract_pages(reader: Any) -> tuple[list[str], list[str]]:
    page_texts: list[str] = []
    warnings: list[str] = []
    remaining = MAX_EXTRACTED_TEXT_CHARS
    for index, page in enumerate(reader.pages, start=1):
        if remaining <= 0:
            warnings.append("pdf_text_truncated_at_2000000_characters")
            break
        try:
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"page_{index}_text_extraction_failed:{type(exc).__name__}")
            text = ""
        cleaned = _clean_text(text)[:remaining]
        remaining -= len(cleaned)
        page_texts.append(cleaned)
    if sum(len(text) for text in page_texts) < 200:
        warnings.append("pdf_text_unavailable_or_scanned; OCR is recommended")
    return page_texts, warnings


def _looks_like_generated_title(value: str) -> bool:
    low = value.casefold().strip()
    return not value or low in {
        "untitled",
        "untitled document",
        "microsoft word",
        "article",
        "main document",
    } or low.startswith("doi:")


def _title_from_text(text: str, filename: str) -> str:
    candidates: list[str] = []
    for raw in text[:8000].splitlines()[:80]:
        line = re.sub(r"\s+", " ", raw).strip(" -|_")
        if not (18 <= len(line) <= 280):
            continue
        if re.search(r"\b(abstract|keywords?|doi|received|accepted|journal|copyright)\b", line, re.I):
            continue
        if len(line.split()) >= 4:
            candidates.append(line)
    if candidates:
        return max(candidates[:16], key=lambda value: (len(value.split()), len(value)))
    return re.sub(r"[_-]+", " ", Path(filename).stem).strip()


def _authors_from_info(raw: str) -> list[str]:
    if not raw:
        return []
    parts = [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"\s*;\s*|\s+and\s+", raw)
        if part.strip()
    ]
    return list(dict.fromkeys(parts))[:100]


def _abstract_from_text(text: str) -> str:
    match = re.search(
        r"(?:^|\n)\s*abstract\s*[:.\-]?\s*(.{80,5000}?)(?=\n\s*(?:keywords?|introduction|1\.?\s+introduction)\b)",
        text,
        re.I | re.S,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _year_from_info(info: dict[str, str], text: str) -> int | None:
    for haystack in (info.get("creation_date", ""), text[:12000]):
        match = _YEAR_RE.search(haystack)
        if match:
            year = int(match.group(0))
            if 1800 <= year <= datetime.now().year + 1:
                return year
    return None


def _doi_from_text(text: str) -> str | None:
    match = _DOI_RE.search(text[:100_000])
    return match.group(0).rstrip(".,;)").casefold() if match else None


def _markdown_document(title: str, page_texts: list[str]) -> str:
    parts = [f"# {title}", ""]
    for index, text in enumerate(page_texts, start=1):
        if not text:
            continue
        parts.extend([f"<!-- source_pdf_page: {index} -->", text, ""])
    return "\n".join(parts).rstrip() + "\n"


def _existing_metadata(review_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    directory = Path(review_root) / "review-library" / "metadata" / "papers"
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("P*.metadata.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rows.append((path, data))
    return rows


def _next_paper_id(rows: list[tuple[Path, dict[str, Any]]]) -> str:
    used: list[int] = []
    for path, metadata in rows:
        match = re.fullmatch(r"P(\d+)", str(metadata.get("paper_id") or path.name.split(".", 1)[0]))
        if match:
            used.append(int(match.group(1)))
    return f"P{max(used, default=0) + 1:03d}"


def _duplicate_for_digest(rows: list[tuple[Path, dict[str, Any]]], digest: str) -> dict[str, Any] | None:
    for _, metadata in rows:
        if str((metadata.get("source_file") or {}).get("sha256") or "") == digest:
            return metadata
    return None


def _rebuild_registry(review_root: Path) -> None:
    scripts = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "review-literature-acquisition"
        / "scripts"
    )
    if str(scripts) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(scripts))
    from literature_acquisition import _rebuild_registry as rebuild_registry

    rebuild_registry(Path(review_root))


def _mineru_content_list(extracted_dir: Path) -> Path | None:
    direct = sorted(extracted_dir.glob("*_content_list.json"))
    if direct:
        return direct[0]
    nested = sorted(extracted_dir.rglob("*_content_list.json"))
    return nested[0] if nested else None


def _mineru_artifact_paths(review_root: Path, slug: str) -> dict[str, Path]:
    output_dir = Path(review_root) / "mineru-outputs"
    return {
        "output_dir": output_dir,
        "markdown": output_dir / "markdown" / f"{slug}.md",
        "extracted_dir": output_dir / "extracted" / slug,
        "raw_zip": output_dir / "raw_zips" / f"{slug}.zip",
        "manifest": output_dir / "manifests" / f"{slug}.json",
    }


def _remove_mineru_artifacts(review_root: Path, slug: str) -> None:
    artifacts = _mineru_artifact_paths(review_root, slug)
    for key in ("markdown", "raw_zip", "manifest"):
        artifacts[key].unlink(missing_ok=True)
    extracted_dir = artifacts["extracted_dir"]
    if extracted_dir.is_dir():
        shutil.rmtree(extracted_dir)


def _process_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    text = "\n".join([completed.stderr or "", completed.stdout or ""])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    priority = [
        line
        for line in lines
        if "[failed]" in line
        or "MinerU HTTP" in line
        or "MinerU API error" in line
    ]
    selected = priority[-3:] if priority else lines[-8:]
    return " | ".join(dict.fromkeys(selected))[:1800]


def _split_pdf_for_mineru(
    pdf_path: Path,
    chunk_dir: Path,
    slug: str,
    *,
    max_pages: int = MINERU_MAX_PAGES_PER_TASK,
) -> list[MinerUPdfPart]:
    """Create provider-sized PDFs while retaining the original page offsets."""

    if max_pages < 1:
        raise ValueError("MinerU maximum pages per task must be positive.")
    reader = _pdf_reader(pdf_path)
    page_count = len(reader.pages)
    if page_count <= max_pages:
        return []
    try:
        from pypdf import PdfWriter
    except ImportError as exc:
        raise RuntimeError("PDF chunking requires pypdf.") from exc

    chunk_dir.mkdir(parents=True, exist_ok=False)
    parts: list[MinerUPdfPart] = []
    for part_index, start in enumerate(range(0, page_count, max_pages), start=1):
        end = min(start + max_pages, page_count)
        part_slug = f"{slug}-part-{part_index:03d}"
        part_path = chunk_dir / f"{part_slug}.pdf"
        writer = PdfWriter()
        for page_index in range(start, end):
            writer.add_page(reader.pages[page_index])
        with part_path.open("xb") as stream:
            writer.write(stream)
        parts.append(
            MinerUPdfPart(
                pdf_path=part_path,
                slug=part_slug,
                page_offset=start,
                start_page=start + 1,
                end_page=end,
            )
        )
    return parts


def _prefixed_asset_path(raw: Any, prefix: str, part_dir: Path) -> Any:
    value = str(raw or "").strip()
    if not value or re.match(r"^(?:[a-z]+:|[/\\])", value, re.I):
        return raw
    normalized = value.replace("\\", "/")
    if not (part_dir / Path(*normalized.split("/"))).is_file():
        return raw
    return f"{prefix}/{normalized}"


def _merge_mineru_parts(
    output_dir: Path,
    slug: str,
    parts: list[MinerUPdfPart],
) -> None:
    """Merge chunk output into the same artifact contract as a single PDF."""

    final_extracted = output_dir / "extracted" / slug
    final_markdown = output_dir / "markdown" / f"{slug}.md"
    final_extracted.mkdir(parents=True, exist_ok=False)
    final_markdown.parent.mkdir(parents=True, exist_ok=True)
    combined_blocks: list[dict[str, Any]] = []
    markdown_sections: list[str] = []
    full_sections: list[str] = []

    for part_index, part in enumerate(parts, start=1):
        part_extracted = output_dir / "extracted" / part.slug
        part_markdown = output_dir / "markdown" / f"{part.slug}.md"
        part_content = _mineru_content_list(part_extracted) if part_extracted.is_dir() else None
        if not part_markdown.is_file() or not part_content or not part_content.is_file():
            raise RuntimeError(
                f"MinerU returned an incomplete result for pages {part.start_page}-{part.end_page}."
            )
        try:
            blocks = json.loads(part_content.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"MinerU content is unreadable for pages {part.start_page}-{part.end_page}."
            ) from exc
        if not isinstance(blocks, list):
            raise RuntimeError(
                f"MinerU content has an invalid structure for pages {part.start_page}-{part.end_page}."
            )

        part_folder = f"part-{part_index:03d}"
        copied_part = final_extracted / "parts" / part_folder
        copied_part.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(part_extracted, copied_part)
        asset_prefix = f"parts/{part_folder}"
        for raw_block in blocks:
            if not isinstance(raw_block, dict):
                continue
            block = deepcopy(raw_block)
            for page_key in ("page_idx", "page_id"):
                if isinstance(block.get(page_key), int):
                    block[page_key] += part.page_offset
            for path_key in ("img_path", "image_path", "path"):
                if path_key in block:
                    block[path_key] = _prefixed_asset_path(
                        block[path_key], asset_prefix, part_extracted
                    )
            combined_blocks.append(block)

        page_marker = (
            f"<!-- mineru_source_pages: {part.start_page}-{part.end_page} -->"
        )
        markdown_text = part_markdown.read_text(encoding="utf-8", errors="replace")
        markdown_text = markdown_text.replace(
            f"../extracted/{part.slug}/",
            f"../extracted/{slug}/{asset_prefix}/",
        )
        markdown_sections.extend([page_marker, markdown_text.strip()])

        part_full = part_extracted / "full.md"
        if not part_full.is_file():
            candidates = sorted(part_extracted.rglob("*.md"))
            part_full = candidates[0] if candidates else part_markdown
        full_text = part_full.read_text(encoding="utf-8", errors="replace")
        full_text = full_text.replace("(images/", f"({asset_prefix}/images/")
        full_text = full_text.replace('src="images/', f'src="{asset_prefix}/images/')
        full_text = full_text.replace("src='images/", f"src='{asset_prefix}/images/")
        full_sections.extend([page_marker, full_text.strip()])

    final_markdown.write_text("\n\n".join(markdown_sections).rstrip() + "\n", encoding="utf-8")
    (final_extracted / "full.md").write_text(
        "\n\n".join(full_sections).rstrip() + "\n", encoding="utf-8"
    )
    (final_extracted / f"{slug}_content_list.json").write_text(
        json.dumps(combined_blocks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_mineru_parser(review_root: Path, pdf_path: Path, slug: str) -> dict[str, Any]:
    """Run the canonical MinerU single-PDF workflow and validate its required outputs."""
    root = Path(review_root).resolve()
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "mineru-precise-parse-review-writer"
        / "scripts"
        / "parse_review_writer_pdfs.py"
    )
    if not script.is_file():
        raise RuntimeError(f"MinerU parser script is missing: {script}")
    artifacts = _mineru_artifact_paths(root, slug)
    artifacts["manifest"].parent.mkdir(parents=True, exist_ok=True)
    page_count = len(_pdf_reader(pdf_path).pages)
    chunk_dir: Path | None = None
    parts: list[MinerUPdfPart] = []
    if page_count > MINERU_MAX_PAGES_PER_TASK:
        chunk_dir = Path(tempfile.mkdtemp(prefix=f".{slug}-mineru-chunks-", dir=root))
        chunk_dir.rmdir()
        parts = _split_pdf_for_mineru(pdf_path, chunk_dir, slug)
    command = [
        sys.executable,
        str(script),
        "--input-dir",
        str(chunk_dir or pdf_path.parent),
        "--output-dir",
        str(artifacts["output_dir"]),
        "--manifest-path",
        str(artifacts["manifest"]),
        "--force",
    ]
    if not parts:
        command[4:4] = ["--pdf", str(pdf_path)]
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ),
            timeout=MINERU_UPLOAD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "MinerU precise parsing timed out after 35 minutes; the PDF was not admitted to Library."
        ) from exc
    finally:
        if chunk_dir is not None:
            shutil.rmtree(chunk_dir, ignore_errors=True)
    if completed.returncode != 0:
        detail = _process_error_detail(completed)
        raise RuntimeError(
            "MinerU precise parsing failed; the PDF was not admitted to Library."
            + (f" {detail}" if detail else "")
        )
    if parts:
        _merge_mineru_parts(artifacts["output_dir"], slug, parts)

    markdown_path = artifacts["markdown"]
    extracted_dir = artifacts["extracted_dir"]
    content_path = _mineru_content_list(extracted_dir) if extracted_dir.is_dir() else None
    missing: list[str] = []
    if not markdown_path.is_file():
        missing.append("Markdown")
    if not extracted_dir.is_dir():
        missing.append("extracted directory")
    if not content_path or not content_path.is_file():
        missing.append("content_list.json")
    if missing:
        raise RuntimeError(
            "MinerU returned an incomplete parse (missing " + ", ".join(missing) + "); the PDF was not admitted to Library."
        )
    try:
        blocks = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("MinerU content_list.json is unreadable; the PDF was not admitted to Library.") from exc
    if not isinstance(blocks, list):
        raise RuntimeError("MinerU content_list.json has an invalid structure; the PDF was not admitted to Library.")
    provider_request_id = ""
    try:
        manifest_data = json.loads(artifacts["manifest"].read_text(encoding="utf-8"))
        batches = manifest_data.get("batches") if isinstance(manifest_data, dict) else []
        if isinstance(batches, list) and batches and isinstance(batches[0], dict):
            provider_request_id = str(batches[0].get("batch_id") or "")
    except (OSError, json.JSONDecodeError):
        provider_request_id = ""
    full_md = extracted_dir / "full.md"
    if not full_md.is_file():
        full_candidates = sorted(extracted_dir.rglob("*.md"))
        full_md = full_candidates[0] if full_candidates else markdown_path
    return {
        "slug": slug,
        "markdown_copy": markdown_path,
        "extracted_dir": extracted_dir,
        "content_list": content_path,
        "full_md": full_md,
        "manifest_path": artifacts["manifest"],
        "content_block_count": len(blocks),
        "provider_request_id": provider_request_id,
        "mineru_part_count": max(1, len(parts)),
    }


def ingest_local_pdf(review_root: Path, original_filename: object, staged_pdf: Path) -> dict[str, Any]:
    """Run MinerU for one uploaded PDF, then admit its complete artifacts to Library."""
    root = Path(review_root).resolve()
    filename = sanitize_pdf_filename(original_filename)
    staged_pdf = Path(staged_pdf).resolve()
    validation = validate_local_pdf(staged_pdf)
    digest = str(validation["sha256"])
    with _INGEST_LOCK:
        rows = _existing_metadata(root)
        duplicate = _duplicate_for_digest(rows, digest)
        duplicate_mode = str((duplicate or {}).get("extraction", {}).get("mode") or "")
        duplicate_paths = (duplicate or {}).get("source_paths") or {}
        duplicate_content = Path(str(duplicate_paths.get("content_list") or "").strip()) if duplicate else None
        if duplicate and "mineru" in duplicate_mode.casefold() and duplicate_content and duplicate_content.is_file():
            return {
                "status": "duplicate_file",
                "paper_id": duplicate.get("paper_id"),
                "filename": filename,
                "sha256": digest,
                "message": "This PDF is already registered in Library.",
            }

        upgrading_existing = bool(duplicate)
        paper_id = str(duplicate.get("paper_id")) if duplicate else _next_paper_id(rows)
        upload_dir = root / "review-library" / "uploads"
        metadata_dir = root / "review-library" / "metadata" / "papers"
        upload_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        final_pdf = upload_dir / f"{paper_id}.pdf"
        meta_path = metadata_dir / f"{paper_id}.metadata.json"
        created_pdf = False
        if not final_pdf.is_file():
            copied = final_pdf.with_suffix(".pdf.tmp")
            shutil.copyfile(staged_pdf, copied)
            copied.replace(final_pdf)
            created_pdf = True
        prep = _metadata_module(root)
        slug = prep.slugify_mineru(final_pdf.stem)
        try:
            mineru = _run_mineru_parser(root, final_pdf, slug)
            reader = _pdf_reader(final_pdf)
            info = _document_info(reader)
            final_md = Path(mineru["markdown_copy"])
            content_path = Path(mineru["content_list"])
            extracted_text = final_md.read_text(encoding="utf-8", errors="replace")
            job = {
                "slug": slug,
                "pdf_name": filename,
                "relative_pdf_path": str(final_pdf.relative_to(root)),
                "extracted_dir": str(mineru["extracted_dir"]),
                "markdown_copy": str(final_md),
                "full_md": str(mineru["full_md"]),
            }
            metadata, _, _, _ = prep.build_metadata(
                paper_id,
                job,
                final_pdf,
                final_md,
                content_path,
                duplicate,
                root,
            )
            title = str((metadata.get("title") or {}).get("value") or "").strip()
            info_title = info.get("title", "")
            if (not title or _looks_like_generated_title(title)) and not _looks_like_generated_title(info_title):
                title = _clean_text(info_title)
                metadata["title"] = _field(title, "pdf_document_info", 0.88)
            title = title or _title_from_text(extracted_text, filename) or Path(filename).stem
            if not (metadata.get("title") or {}).get("value"):
                metadata["title"] = _field(title, "mineru_markdown_front_matter", 0.82)
            authors = _authors_from_info(info.get("author", ""))
            if authors and not (metadata.get("authors") or {}).get("value"):
                metadata["authors"] = _field(authors, "pdf_document_info", 0.72)
            year = _year_from_info(info, extracted_text)
            if year and not (metadata.get("year") or {}).get("value"):
                metadata["year"] = _field(year, "pdf_document_info_or_front_matter", 0.68)
            doi = _doi_from_text(extracted_text)
            if doi and not (metadata.get("doi") or {}).get("value"):
                metadata["doi"] = _field(doi, "pdf_text_regex", 0.9)
            abstract = _abstract_from_text(extracted_text)
            if abstract and not (metadata.get("abstract") or {}).get("value"):
                metadata["abstract"] = _field(abstract, "pdf_text_abstract_region", 0.78)
            metadata["source_file"].update(
                {
                    "pdf_name": filename,
                    "original_upload_name": filename,
                    "relative_pdf_path": str(final_pdf.relative_to(root)),
                    "sha256": digest,
                    "size_bytes": validation["size_bytes"],
                }
            )
            metadata["acquisition"] = {
                "provider": "local_upload",
                "source_url": None,
                "acquired_at": utc_now(),
            }
            metadata["extraction"] = {
                "mode": "local_pdf_upload+mineru_precise",
                "model": "mineru-vlm",
                "created_at": utc_now(),
                "inputs": {
                    "page_count": len(reader.pages),
                    "extracted_text_chars": len(extracted_text),
                    "original_upload_name": filename,
                    "content_blocks": int(mineru["content_block_count"]),
                    "mineru_part_count": int(mineru.get("mineru_part_count") or 1),
                    "content_list": str(content_path),
                    "extracted_dir": str(mineru["extracted_dir"]),
                    "manifest": str(mineru["manifest_path"]),
                },
                "notes": [
                    "mineru_precise_parse_completed_before_library_admission",
                    *(
                        ["mineru_precise_parse_auto_chunked"]
                        if int(mineru.get("mineru_part_count") or 1) > 1
                        else []
                    ),
                ],
            }
            prep.apply_structured_tags_to_compat_fields(metadata)
            prep.update_quality(metadata)
            metadata["quality"]["needs_human_check"] = True
            meta_tmp = meta_path.with_suffix(".json.tmp")
            meta_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            meta_tmp.replace(meta_path)
            _rebuild_registry(root)
        except Exception:
            _remove_mineru_artifacts(root, slug)
            if created_pdf:
                final_pdf.unlink(missing_ok=True)
            if not upgrading_existing:
                meta_path.unlink(missing_ok=True)
            raise

    return {
        "status": "upgraded_to_mineru" if upgrading_existing else "uploaded",
        "paper_id": paper_id,
        "filename": filename,
        "title": title,
        "page_count": len(reader.pages),
        "provider_request_id": str(mineru.get("provider_request_id") or ""),
        "extracted_text_chars": len(extracted_text),
        "mineru_ready": True,
        "content_block_count": int(mineru["content_block_count"]),
        "warnings": list(metadata.get("quality", {}).get("warnings") or []),
        "metadata_path": str(meta_path),
        "pdf_path": str(final_pdf),
        "markdown_path": str(final_md),
        "content_list_path": str(content_path),
        "extracted_dir": str(mineru["extracted_dir"]),
    }
