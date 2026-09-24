"""One admin read model over existing job failures and minimal API events."""
from datetime import timedelta
import logging
from pathlib import Path
import re
import uuid
import threading
from .daemon_executor import DaemonWorkerPool

from sqlalchemy import String, cast, delete, literal, select, union_all
from .database import Project, User, database_session, utc_now
from .workflow_models import SystemErrorEvent, WorkflowJob

logger = logging.getLogger(__name__)


class FailureRecorder:
    """Bounded best-effort writes: a stuck DB cannot grow threads or the queue."""
    def __init__(self):
        self.pool = None
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(64)

    def submit(self, callback, request_id):
        if not self.slots.acquire(blocking=False):
            logger.error("system_error_queue_full request_id=%s", request_id)
            return None
        try:
            def safely_record():
                try:
                    callback()
                except Exception:
                    logger.error("system_error_persistence_failed request_id=%s", request_id)
            with self.lock:
                if self.pool is None:
                    self.pool = DaemonWorkerPool(1, thread_name_prefix="failure-log")
                future = self.pool.submit(safely_record)
        except Exception:
            self.slots.release()
            logger.error("system_error_queue_closed request_id=%s", request_id)
            return None
        future.add_done_callback(lambda _: self.slots.release())
        return future

    def close(self):
        with self.lock:
            if self.pool is not None:
                self.pool.shutdown(wait=False, cancel_futures=True)
                self.pool = None


def prune_failures(sessions):
    with database_session(sessions) as session:
        session.execute(delete(SystemErrorEvent).where(
            SystemErrorEvent.created_at < utc_now() - timedelta(days=30)))


def safe_summary(value):
    value = str(value or "")
    value = re.sub(r"https?://\S+", "[URL]", value)
    value = re.sub(r"(?i)\b(?:sk-|bearer\s+)[\w.\-]+", "[REDACTED]", value)
    value = re.sub(r'''(?ix)(["']?(?:api[_-]?key|key|password|(?:access_|refresh_)?token|authorization|secret)["']?\s*[:=]\s*)["']?[^\s,;}]+''', r"\1[REDACTED]", value)
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)[:1000]


def record_failure(sessions, request, status_code, exc=None):
    if sessions is None:
        return
    principal = getattr(request.state, "principal", None)
    # Do not turn anonymous login failures, probes, or normal 404s into a log flood.
    if status_code < 500 and (principal is None or status_code in {401, 404}):
        return
    code = getattr(request.state, "failure_code", "") or (
        type(exc).__name__ if exc else f"HTTP_{status_code}")
    route = getattr(request.scope.get("route"), "path", "<unmatched>")
    location = safe_summary(getattr(request.state, "failure_summary", ""))[:240]
    if not location and exc and exc.__traceback__:
        tb = exc.__traceback__
        while tb.tb_next:
            tb = tb.tb_next
        location = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
    try:
        with database_session(sessions) as session:
            user_id = uuid.UUID(principal.user_id) if principal else None
            if user_id and session.get(User, user_id) is None:
                user_id = None
            project_id = None
            raw_project = request.path_params.get("project_id")
            if raw_project and user_id:
                try:
                    project = session.get(Project, uuid.UUID(str(raw_project)))
                    if project and project.user_id == user_id:
                        project_id = project.id
                except ValueError:
                    pass
            session.add(SystemErrorEvent(user_id=user_id, project_id=project_id,
                request_id=request.state.request_id, method=request.method[:12],
                route=route[:240], status_code=status_code, error_code=str(code)[:96],
                location=location[:240]))
    except Exception:
        # Logging must not change the user's response or recursively log itself.
        logger.error("system_error_persistence_failed request_id=%s", request.state.request_id)
    logger.warning(
        "request_failed request_id=%s status=%s code=%s route=%s summary=%s",
        request.state.request_id,
        status_code,
        str(code)[:96],
        route,
        location or "-",
    )


def list_failures(sessions, *, query="", source="", days=30, limit=50, offset=0):
    cutoff = utc_now() - timedelta(days=days)
    jobs = select(cast(WorkflowJob.id, String).label("id"), literal("job").label("source"),
        WorkflowJob.user_id, cast(WorkflowJob.project_id, String).label("project_id"),
        WorkflowJob.error_code, WorkflowJob.error_message.label("message"),
        WorkflowJob.job_type.label("operation"), literal(0).label("status_code"),
        literal("").label("request_id"), WorkflowJob.updated_at.label("created_at")
    ).where(WorkflowJob.status == "failed", WorkflowJob.updated_at >= cutoff)
    events = select(cast(SystemErrorEvent.id, String), literal("api"), SystemErrorEvent.user_id,
        cast(SystemErrorEvent.project_id, String), SystemErrorEvent.error_code,
        SystemErrorEvent.location, SystemErrorEvent.route, SystemErrorEvent.status_code,
        SystemErrorEvent.request_id, SystemErrorEvent.created_at
    ).where(SystemErrorEvent.created_at >= cutoff)
    records = union_all(jobs, events).subquery()
    stmt = select(records, User.email).outerjoin(User, User.id == records.c.user_id)
    if source:
        stmt = stmt.where(records.c.source == source)
    if query.strip():
        pattern = query.strip()[:160]
        stmt = stmt.where(User.email.icontains(pattern, autoescape=True)
            | records.c.id.icontains(pattern, autoescape=True)
            | records.c.project_id.icontains(pattern, autoescape=True)
            | records.c.error_code.icontains(pattern, autoescape=True)
            | records.c.request_id.icontains(pattern, autoescape=True))
    with database_session(sessions) as session:
        rows = session.execute(stmt.order_by(records.c.created_at.desc(), records.c.source, records.c.id)
                               .offset(offset).limit(limit+1)).mappings().all()
    return {"items": [{**r, "message":safe_summary(r["message"])} for r in rows[:limit]],
            "has_more":len(rows)>limit}
