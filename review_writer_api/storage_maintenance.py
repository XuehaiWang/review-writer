"""Conservative hosted-workspace housekeeping, never publication retention.

Only succeeded job-staging directories are eligible. Failed/cancelled jobs may
be retried indefinitely; unknown, referenced, and linked paths are left alone.
"""
from datetime import timedelta
import os
from pathlib import Path
import shutil
import stat
import threading
import time
import uuid

from sqlalchemy import String, cast, func, or_, select, text

from .database import database_session, utc_now
from .workflow_models import WorkflowArtifact, WorkflowJob, WorkflowSystemState, LibraryPaper, LibraryArtifact

STATE_KEY = "storage-maintenance-v1"
GRACE_HOURS = 48
_lock = threading.Lock()


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def category(path):
    parts = path.parts
    if any(p in parts for p in ("job-staging", ".staging", ".parse", ".upload-staging")):
        return "temporary"
    if "vectors" in parts:
        return "indexes"
    if "review-library" in parts:
        return "library"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}:
        return "images"
    if path.suffix.lower() in {".pdf", ".docx", ".tex"}:
        return "exports"
    return "manuscripts_and_other"


def scan_workspace(root, *, seconds=10, max_files=200000):
    """Do not follow links; bounded scans advertise partial totals explicitly."""
    started = time.monotonic()
    sizes = dict.fromkeys(("library", "images", "exports", "temporary", "indexes", "manuscripts_and_other"), 0)
    count, skipped, partial = 0, 0, False
    def inaccessible(_error):
        nonlocal partial
        partial = True
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=inaccessible):
        if time.monotonic() - started > seconds:
            partial = True
            break
        dirs[:] = [d for d in dirs if not linked(Path(directory) / d)]
        for name in files:
            if count >= max_files or time.monotonic() - started > seconds:
                partial = True
                break
            path = Path(directory) / name
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    skipped += 1
                    continue
                sizes[category(path.relative_to(root))] += info.st_size
                count += 1
            except OSError:
                partial = True
        if partial and (count >= max_files or time.monotonic() - started > seconds):
            break
    return {"categories": sizes, "total_bytes": sum(sizes.values()), "file_count": count,
            "partial": partial, "skipped_files": skipped, "scanned_at": utc_now().isoformat()}


def disk_status(root):
    usage = shutil.disk_usage(root)
    # Capacity refers to the filesystem containing the workspace, not DB usage.
    return {"total_bytes": usage.total, "free_bytes": usage.free, "used_bytes": usage.used,
            "low_space": usage.free < 5 * 1024**3 or usage.free / max(1, usage.total) < .10}


def safe_tree(path, root, cutoff):
    """Preflight all entries; unknown links/special files/new writes veto deletion."""
    if not path.is_relative_to(root) or path == root:
        return None
    for parent in (path, *path.parents):
        if parent == root:
            break
        if linked(parent):
            return None
    if not path.is_dir() or path.resolve() != path:
        return None
    total = 0
    started = time.monotonic()
    def inaccessible(error):
        raise error
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=inaccessible):
        if time.monotonic() - started > 5:
            return None
        for entry in [Path(directory), *(Path(directory) / n for n in dirs + files)]:
            if time.monotonic() - started > 5:
                return None
            info = entry.lstat()
            if linked(entry) or info.st_mtime > cutoff or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                return None
        total += sum((Path(directory) / n).stat().st_size for n in files)
    return total


class StorageMaintenance:
    def __init__(self, sessions, root):
        self.sessions = sessions
        self.root = Path(root).resolve()

    def snapshot(self):
        with database_session(self.sessions) as session:
            state = session.get(WorkflowSystemState, STATE_KEY)
            saved = dict(state.value_json) if state else {}
        return {**saved, "disk": disk_status(self.root), "grace_hours": GRACE_HOURS,
                "statistics_pending": "usage" not in saved}

    def run(self, *, manual=False):
        if not _lock.acquire(blocking=False):
            return {**self.snapshot(), "busy": True}
        try:
            with database_session(self.sessions) as session:
                if session.bind.dialect.name == "postgresql":
                    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(721094311)")):
                        return {**self.snapshot(), "busy": True}
                state = session.get(WorkflowSystemState, STATE_KEY)
                previous = dict(state.value_json) if state else {}
                # Multiple ingest workers share one hourly maintenance pass.
                last = previous.get("last_run_epoch", 0)
                if time.time() - last < (60 if manual else 3500):
                    return {**previous, "disk": disk_status(self.root), "cooldown": True}
                result = self._clean(session)
                usage = scan_workspace(self.root)
                saved = {"usage": usage, "last_cleanup": result, "last_run_epoch": time.time()}
                if state is None:
                    session.add(WorkflowSystemState(key=STATE_KEY, value_json=saved))
                else:
                    state.value_json = saved
            return {**saved, "disk": disk_status(self.root), "grace_hours": GRACE_HOURS}
        finally:
            _lock.release()

    def _clean(self, session):
        cutoff = utc_now() - timedelta(hours=GRACE_HOURS)
        result = {"at": utc_now().isoformat(), "removed_directories": 0, "freed_bytes": 0,
                  "skipped": 0, "errors": 0, "error_reasons": [], "partial": False}
        # Protect persisted paths and any other job's references, including
        # retry chains and failed jobs. These are checks, never deletion targets.
        started = time.monotonic()
        for user in self.root.iterdir():
            if time.monotonic() - started > 20:
                result["partial"] = True
                return result
            if linked(user) or not user.is_dir():
                continue
            try:
                if str(uuid.UUID(user.name)) != user.name:
                    continue
            except ValueError:
                continue
            base = user / ".review-writer" / "job-staging"
            if linked(user / ".review-writer") or linked(base) or not base.is_dir():
                continue
            for path in base.iterdir():
                if time.monotonic() - started > 20 or result["removed_directories"] >= 100:
                    result["partial"] = True
                    return result
                try:
                    job_id = uuid.UUID(path.name)
                    if str(job_id) != path.name:
                        raise ValueError("Non-canonical ID")
                except ValueError:
                    result["skipped"] += 1
                    continue
                job = session.get(WorkflowJob, job_id)
                updated = job.updated_at if job else None
                comparison = cutoff if updated and updated.tzinfo else cutoff.replace(tzinfo=None)
                if (not job or str(job.user_id) != user.name or job.status != "succeeded"
                        or not updated or updated > comparison or job.lease_owner or job.lease_token or job.lease_expires_at):
                    result["skipped"] += 1
                    continue
                if self._referenced(session, job_id):
                    result["skipped"] += 1
                    continue
                try:
                    size = safe_tree(path, self.root, cutoff.timestamp())
                    if size is None:
                        result["skipped"] += 1
                        continue
                    # The canonical, terminal job directory only; never infer
                    # filesystem paths from task payloads or user input.
                    shutil.rmtree(path)
                    result["removed_directories"] += 1
                    result["freed_bytes"] += size
                except OSError as exc:
                    result["errors"] += 1
                    reason = "permission_denied" if isinstance(exc, PermissionError) else "files_changed" if isinstance(exc, FileNotFoundError) else "filesystem_error"
                    if reason not in result["error_reasons"]:
                        result["error_reasons"].append(reason)
        return result

    @staticmethod
    def _referenced(session, job_id):
        token = str(job_id)
        def path_ref(column):
            # Metadata may contain a provenance job_id without depending on its
            # temporary files. Match a staging path, not that provenance alone.
            normalized = func.replace(func.replace(cast(column, String), "\\\\", "/"), "\\", "/")
            return normalized.contains(f"job-staging/{token}")
        checks = (
            select(LibraryArtifact.id).where(path_ref(LibraryArtifact.relative_path)),
            select(WorkflowArtifact.id).where(or_(path_ref(WorkflowArtifact.relative_path), path_ref(WorkflowArtifact.metadata_json))),
            select(LibraryPaper.id).where(or_(path_ref(LibraryPaper.pdf_relative_path),
                path_ref(LibraryPaper.markdown_relative_path), path_ref(LibraryPaper.metadata_json))),
            select(WorkflowJob.id).where(WorkflowJob.id != job_id, or_(
                WorkflowJob.retry_of_job_id == job_id, cast(WorkflowJob.payload_json, String).contains(token),
                cast(WorkflowJob.result_json, String).contains(token))),
        )
        return any(session.scalar(query.limit(1)) is not None for query in checks)
