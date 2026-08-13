"""Stable workflow-stage contracts shared by the API and migration tooling."""

from __future__ import annotations

from collections.abc import Mapping


INTERNAL_STAGES = (
    "discovery",
    "matrix",
    "blueprint",
    "sections",
    "figure-review",
    "figures",
    "draft",
    "final",
)

USER_STAGES = (
    "library",
    "discovery",
    "planning",
    "sections",
    "images",
    "draft",
    "final",
)

COMPOSITE_STAGE_BY_INTERNAL_STAGE = {
    "discovery": "discovery",
    "matrix": "planning",
    "blueprint": "planning",
    "sections": "sections",
    "figure-review": "images",
    "figures": "images",
    "draft": "draft",
    "final": "final",
}

COMPLETED_STAGE_STATUSES = frozenset(
    {"approved", "complete", "completed", "success", "succeeded"}
)
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


def composite_stage(internal_stage: str) -> str:
    """Return the seven-stage UI stage for an internal workflow stage."""

    try:
        return COMPOSITE_STAGE_BY_INTERNAL_STAGE[internal_stage]
    except KeyError as exc:
        raise ValueError(f"Unknown internal workflow stage: {internal_stage}") from exc


def current_user_stage(stage_statuses: Mapping[str, str]) -> str:
    """Return the first user-visible stage whose internal work is incomplete."""

    for stage_id in INTERNAL_STAGES:
        status = str(stage_statuses.get(stage_id, "pending") or "pending").strip().lower()
        if status not in COMPLETED_STAGE_STATUSES:
            return composite_stage(stage_id)
    return "final"
