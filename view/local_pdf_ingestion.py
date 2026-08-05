#!/usr/bin/env python3
"""Register locally uploaded PDFs in the canonical review-library format."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_LOCAL_PDF_BYTES = 80 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 2_000_000
_INGEST_LOCK = threading.RLock()
_METADATA_MODULE: Any | None = None
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


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
            "Local PDF parsing requires pypdf. Install requirements-workflow.txt in the active Python environment."
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


def ingest_local_pdf(review_root: Path, original_filename: object, staged_pdf: Path) -> dict[str, Any]:
    """Copy one staged PDF into the library and build searchable/generative metadata."""
    root = Path(review_root).resolve()
    filename = sanitize_pdf_filename(original_filename)
    staged_pdf = Path(staged_pdf).resolve()
    validation = validate_local_pdf(staged_pdf)
    digest = str(validation["sha256"])
    with _INGEST_LOCK:
        rows = _existing_metadata(root)
        duplicate = _duplicate_for_digest(rows, digest)
        if duplicate:
            return {
                "status": "duplicate_file",
                "paper_id": duplicate.get("paper_id"),
                "filename": filename,
                "sha256": digest,
                "message": "This PDF is already registered in Library.",
            }

        paper_id = _next_paper_id(rows)
        upload_dir = root / "review-library" / "uploads"
        metadata_dir = root / "review-library" / "metadata" / "papers"
        upload_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        final_pdf = upload_dir / f"{paper_id}.pdf"
        final_md = upload_dir / f"{paper_id}.md"
        meta_path = metadata_dir / f"{paper_id}.metadata.json"
        copied = final_pdf.with_suffix(".pdf.tmp")
        shutil.copyfile(staged_pdf, copied)
        copied.replace(final_pdf)
        try:
            reader = _pdf_reader(final_pdf)
            info = _document_info(reader)
            page_texts, warnings = _extract_pages(reader)
            extracted_text = "\n\n".join(text for text in page_texts if text)
            title = info.get("title", "")
            if _looks_like_generated_title(title):
                title = _title_from_text(extracted_text, filename)
            title = _clean_text(title) or Path(filename).stem
            markdown = _markdown_document(title, page_texts)
            md_tmp = final_md.with_suffix(".md.tmp")
            md_tmp.write_text(markdown, encoding="utf-8")
            md_tmp.replace(final_md)

            prep = _metadata_module(root)
            job = {
                "slug": prep.slugify(Path(filename).stem),
                "pdf_name": filename,
                "relative_pdf_path": str(final_pdf.relative_to(root)),
                "extracted_dir": None,
            }
            metadata, _, _, _ = prep.build_metadata(
                paper_id,
                job,
                final_pdf,
                final_md,
                None,
                None,
                root,
            )
            metadata["title"] = _field(title, "pdf_document_info" if info.get("title") else "pdf_first_page", 0.88)
            authors = _authors_from_info(info.get("author", ""))
            if authors:
                metadata["authors"] = _field(authors, "pdf_document_info", 0.72)
            year = _year_from_info(info, extracted_text)
            if year:
                metadata["year"] = _field(year, "pdf_document_info_or_front_matter", 0.68)
            doi = _doi_from_text(extracted_text)
            if doi:
                metadata["doi"] = _field(doi, "pdf_text_regex", 0.9)
            abstract = _abstract_from_text(extracted_text)
            if abstract:
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
                "mode": "local_pdf_upload+pypdf",
                "model": None,
                "created_at": utc_now(),
                "inputs": {
                    "page_count": len(reader.pages),
                    "pages_with_text": sum(bool(text) for text in page_texts),
                    "extracted_text_chars": len(extracted_text),
                    "original_upload_name": filename,
                },
                "notes": warnings,
            }
            prep.apply_structured_tags_to_compat_fields(metadata)
            prep.update_quality(metadata)
            metadata["quality"]["warnings"] = list(
                dict.fromkeys(list(metadata["quality"].get("warnings") or []) + warnings)
            )
            metadata["quality"]["needs_human_check"] = True
            meta_tmp = meta_path.with_suffix(".json.tmp")
            meta_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            meta_tmp.replace(meta_path)
            _rebuild_registry(root)
        except Exception:
            final_pdf.unlink(missing_ok=True)
            final_md.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise

    return {
        "status": "uploaded",
        "paper_id": paper_id,
        "filename": filename,
        "title": title,
        "page_count": len(reader.pages),
        "extracted_text_chars": len(extracted_text),
        "needs_ocr": any("OCR is recommended" in warning for warning in warnings),
        "warnings": warnings,
        "metadata_path": str(meta_path),
        "pdf_path": str(final_pdf),
        "markdown_path": str(final_md),
    }
