"""Stable workflow-domain errors shared by services, APIs, and workers."""

from __future__ import annotations

from typing import Any


class WorkflowError(Exception):
    code = "WORKFLOW_ERROR"
    status_code = 400
    retryable = False

    def __init__(self, message: str = "Workflow operation failed.", *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": str(self),
                "retryable": self.retryable,
                "details": self.details,
            }
        }


class WorkflowConflict(WorkflowError):
    code = "STATE_CONFLICT"
    status_code = 409


class WorkflowNotFound(WorkflowError):
    code = "WORKFLOW_NOT_FOUND"
    status_code = 404


class WorkflowValidationError(WorkflowError):
    code = "WORKFLOW_VALIDATION_FAILED"
    status_code = 422


class WorkflowMigrationRequired(WorkflowError):
    code = "WORKFLOW_MIGRATION_REQUIRED"
    status_code = 503

    def __init__(
        self,
        message: str = "Workflow migration must complete before workflow access.",
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, details=details)
