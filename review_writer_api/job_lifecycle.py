"""Project lifetime checks shared by deletion, workers, and the model gateway."""

from datetime import datetime
from typing import Any

from sqlalchemy import exists, or_, select, update

from .database import Project
from .workflow_models import WorkflowJob


def active_job_project(job=WorkflowJob):
    """Library jobs are independent; project jobs require a live, owned project."""
    return or_(
        job.project_id.is_(None),
        exists(
            select(Project.id).where(
                Project.id == job.project_id,
                Project.user_id == job.user_id,
                Project.deleted_at.is_(None),
            )
        ),
    )


def cancel_project_jobs(session, now: datetime, *predicates: Any) -> None:
    """Cancel matching unfinished project jobs and fence their old workers.

    Clearing the lease immediately releases the user's queue slot. Subprocess
    cancellation checks stop execution; revoked leases also prevent late writes
    or further model calls while those processes are shutting down.
    """
    session.execute(
        update(WorkflowJob)
        .where(
            WorkflowJob.project_id.is_not(None),
            WorkflowJob.status.in_(("queued", "running", "cancel_requested")),
            *predicates,
        )
        .values(
            status="cancelled",
            cancellation_requested=True,
            finished_at=now,
            updated_at=now,
            lease_owner="",
            lease_token=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
