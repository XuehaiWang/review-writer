"""Bounded ZIP ingestion. Archive names are display data, never output paths."""
from pathlib import Path, PurePosixPath
import stat
import struct
import time
import uuid
import zipfile
import zlib

from review_writer_api.errors import WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_ARCHIVE_PDFS = 300
MAX_EXPANSION_SECONDS = 120


def open_archive(path: Path):
    # Bound central-directory allocation BEFORE ZipFile constructs ZipInfo
    # objects. Multi-volume/ZIP64 archives are unnecessary at these limits.
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size > MAX_ARCHIVE_BYTES:
            raise WorkflowValidationError("ZIP files must be 256 MB or smaller.")
        handle.seek(max(0, size - 65557))
        tail = handle.read()
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise WorkflowValidationError("Invalid or damaged ZIP. Extract the PDFs and upload them directly.")
    _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment_size = struct.unpack(
        "<4s4H2IH", tail[offset:offset + 22])
    if (disk or directory_disk or disk_count != count or count > MAX_ARCHIVE_ENTRIES
            or directory_size > 2 * 1024 * 1024 or directory_offset + directory_size > size
            or offset + 22 + comment_size != len(tail)):
        raise WorkflowValidationError("ZIP directory exceeds safety limits or uses an unsupported format.")
    return zipfile.ZipFile(path)


def archive_path(library, principal, archive_id: str) -> Path:
    principal.require(Permission.PROJECT_WRITE)
    try:
        token = str(uuid.UUID(archive_id))
    except ValueError as exc:
        raise WorkflowValidationError("Invalid archive identifier.") from exc
    root = library.workspace_manager.trusted_user_directory(
        principal.user_id, "review-library", ".upload-staging")
    path = root / f"{token}.zip.part"
    if path.is_symlink() or path.resolve().parent != root:
        raise WorkflowValidationError("Untrusted archive path.")
    return path


def inspect_archive(archive: zipfile.ZipFile, max_pdf_bytes: int):
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise WorkflowValidationError("ZIP contains too many entries (maximum 1000).")
    pdfs, ignored, total = [], 0, 0
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        mode = entry.external_attr >> 16
        if (name.startswith("/") or ".." in parts or ":" in name or "\x00" in name
                or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})):
            raise WorkflowValidationError("ZIP contains an unsafe path or linked file.")
        if entry.flag_bits & 1:
            raise WorkflowValidationError("Encrypted ZIP files are not supported. Extract the PDFs first.")
        total += entry.file_size
        if total > MAX_EXPANDED_BYTES:
            raise WorkflowValidationError("ZIP expanded size exceeds 1 GB.")
        if entry.is_dir():
            continue
        if entry.file_size > 1024 * 1024 and entry.file_size > max(entry.compress_size, 1) * 200:
            raise WorkflowValidationError("ZIP compression ratio exceeds the safety limit.")
        if name.lower().endswith(".pdf") and "__MACOSX" not in parts and not parts[-1].startswith("._"):
            if entry.file_size > max_pdf_bytes:
                raise WorkflowValidationError("Each PDF must be 80 MB or smaller.")
            pdfs.append(entry)
        else:
            ignored += 1
    if not pdfs:
        raise WorkflowValidationError("No PDF files were found in the ZIP.")
    if len(pdfs) > MAX_ARCHIVE_PDFS:
        raise WorkflowValidationError("ZIP contains too many PDFs (maximum 300).")
    return pdfs, ignored


def collect_archive(library, jobs, context, payload, principal):
    """Expand off-request; children use the unchanged upload/parse/index handler."""
    from review_writer_api.job_service import JobCancellationRequested
    from review_writer_api.errors import WorkflowConflict

    path = archive_path(library, principal, str(payload["archive_id"]))
    if not path.is_file():
        raise WorkflowNotFound("The ZIP is no longer available. Please upload it again.")
    batch_id = str(payload["batch_id"])
    failures, submitted = [], 0
    started = time.monotonic()
    # On resumption do not overwrite files already owned by parsing workers.
    existing = jobs.repository.archive_upload_keys(principal.user_id, payload["archive_id"])
    try:
        with open_archive(path) as archive:
            pdfs, ignored = inspect_archive(archive, library.MAX_PDF_BYTES)
            context.report_progress(0, len(pdfs))
            for index, entry in enumerate(pdfs):
                context.checkpoint()
                if jobs.repository.upload_batch_cancelled(principal.user_id, batch_id):
                    raise JobCancellationRequested()
                if time.monotonic() - started > MAX_EXPANSION_SECONDS:
                    raise WorkflowValidationError("ZIP collection timed out. Import smaller folders or ZIP files.")
                token = str(uuid.uuid5(uuid.UUID(payload["archive_id"]), str(entry.header_offset)))
                if token not in existing:
                    staged, safe_name = library.begin_upload(principal, entry.filename)
                    transferred = False
                    try:
                        size = 0
                        with archive.open(entry) as source, staged.open("xb") as target:
                            while chunk := source.read(1024 * 1024):
                                context.checkpoint()
                                if jobs.repository.upload_batch_cancelled(principal.user_id, batch_id):
                                    raise JobCancellationRequested()
                                size += len(chunk)
                                if size > library.MAX_PDF_BYTES or time.monotonic() - started > MAX_EXPANSION_SECONDS:
                                    raise WorkflowValidationError("ZIP entry exceeded the size or time limit.")
                                target.write(chunk)
                        library.validate_staged_upload(staged, size)
                        jobs.submit(principal, scope="library", project_id=None, job_type="library.upload",
                            idempotency_key=token, operation_key=f"archive-entry:{token}", payload={
                                "filename": safe_name, "display_name": entry.filename,
                                "staging_id": staged.name.removesuffix(".pdf.part"),
                                "batch_id": batch_id, "archive_id": payload["archive_id"],
                            })
                        transferred = True
                        existing.add(token)
                    except WorkflowConflict:
                        if jobs.repository.upload_batch_cancelled(principal.user_id, batch_id):
                            raise JobCancellationRequested()
                        raise
                    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, WorkflowValidationError) as exc:
                        failures.append({"filename": entry.filename, "message": str(exc)})
                    finally:
                        if not transferred:
                            staged.unlink(missing_ok=True)
                    if transferred:
                        submitted += 1
                else:
                    submitted += 1
                context.report_progress(index + 1, len(pdfs))
        path.unlink(missing_ok=True)
        return {"pdf_count": len(pdfs), "submitted_count": submitted, "ignored_count": ignored,
                "failed_count": len(failures), "failures": failures}
    except zipfile.BadZipFile as exc:
        path.unlink(missing_ok=True)
        raise WorkflowValidationError("Invalid or damaged ZIP. Extract the PDFs and upload them directly.") from exc
    except (JobCancellationRequested, WorkflowValidationError):
        path.unlink(missing_ok=True)
        raise
