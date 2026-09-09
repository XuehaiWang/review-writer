"""Draft-stage domain errors shared by service action modules."""

from review_writer_api.errors import WorkflowConflict


class DraftNotReady(WorkflowConflict):
    code = "DRAFT_NOT_READY"


class DraftApprovalBlocked(WorkflowConflict):
    code = "DRAFT_APPROVAL_BLOCKED"
