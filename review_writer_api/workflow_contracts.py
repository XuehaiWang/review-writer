"""Stable workflow-stage contracts shared by the API and migration tooling."""

from __future__ import annotations

from collections.abc import Mapping


# Public stages match the seven frontend pages. Values are independently
# versioned checkpoints, NOT additional user stages. Keep their persisted IDs:
# merging them would destroy approval/retry boundaries in existing projects.
STAGE_CHECKPOINTS = {
    "library": (),  # Account-wide library; no mandatory project approval.
    "discovery": ("discovery",),
    "planning": ("matrix",),
    "sections": ("blueprint", "sections"),
    "images": ("figure-review", "figures"),
    "draft": ("draft",),
    "final": ("final",),
}
USER_STAGES = tuple(STAGE_CHECKPOINTS)
WORKFLOW_CHECKPOINTS = tuple(
    checkpoint for checkpoints in STAGE_CHECKPOINTS.values() for checkpoint in checkpoints
)

# Modern artifact paths identify the workflow stage that owns their current
# pointer.  Some later stages may publish an improved copy of an upstream
# artifact (for example, Draft fact repair can publish a new Matrix).  Pointer
# invalidation must follow this logical ownership rather than the stage that
# happened to write the immutable version.
ARTIFACT_OWNER_STAGE_BY_PREFIX = {
    "discovery": "discovery",
    "matrix": "matrix",
    "planning": "matrix",
    "blueprint": "blueprint",
    "sections": "sections",
    "figure-review": "figure-review",
    "figures": "figures",
    "draft": "draft",
    "final": "final",
}

COMPOSITE_STAGE_BY_INTERNAL_STAGE = {
    checkpoint: stage
    for stage, checkpoints in STAGE_CHECKPOINTS.items()
    for checkpoint in checkpoints
}

COMPLETED_STAGE_STATUSES = frozenset(
    {"approved", "complete", "completed", "success", "succeeded"}
)
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


def composite_stage(internal_stage: str) -> str:
    """Return the seven-stage UI stage for an internal workflow stage."""

    if internal_stage in USER_STAGES:
        return internal_stage
    try:
        return COMPOSITE_STAGE_BY_INTERNAL_STAGE[internal_stage]
    except KeyError as exc:
        raise ValueError(f"Unknown internal workflow stage: {internal_stage}") from exc


def current_user_stage(stage_statuses: Mapping[str, str]) -> str:
    """Return the first user-visible stage whose internal work is incomplete."""
    return composite_stage(current_checkpoint(stage_statuses))


def current_checkpoint(stage_states: Mapping[str, object]) -> str:
    """Single completion rule for local, hosted and transactional navigation."""
    for stage_id in WORKFLOW_CHECKPOINTS:
        value = stage_states.get(stage_id)
        status = str(value.get("status") if isinstance(value, Mapping) else value or "pending").strip().lower()
        if status not in COMPLETED_STAGE_STATUSES:
            return stage_id
    return "final"


def completed_user_stages(completed_checkpoints: tuple[str, ...]) -> tuple[str, ...]:
    """A stage is complete only when ALL its checkpoints are complete."""
    completed = set(completed_checkpoints)
    return tuple(
        stage for stage, checkpoints in STAGE_CHECKPOINTS.items()
        if checkpoints and all(checkpoint in completed for checkpoint in checkpoints)
    )


def public_stage_view(
    *,
    current_stage: str,
    completed_checkpoints: tuple[str, ...],
    discovery_status: str,
) -> tuple[str, tuple[str, ...]]:
    """The single place that names internal checkpoints for the reader.

    Repositories and services keep checkpoint IDs, because invalidation and
    approval are expressed in them. Display naming happens exactly once, here,
    so a list response, a read, a create and an update always agree.

    Whether the outline owns the 03/04 boundary is a completion fact decided by
    the service; this function only names the result.
    """
    completed = completed_user_stages(tuple(completed_checkpoints))
    stage = composite_stage(current_stage)
    # An account-wide library entry point, not an unstarted discovery run.
    if stage == "discovery" and str(discovery_status or "pending") == "pending" and not completed:
        stage = "library"
    return stage, completed
