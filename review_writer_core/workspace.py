"""Workspace discovery and canonical artifact paths.

The workflow has a versioned on-disk contract, but it must not depend on a
particular checkout depth or machine-specific absolute path.  This module is
the single place where that contract is described.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,95})")

STAGE_DIRECTORIES: dict[str, str] = {
    "discovery": "00_discovery",
    "matrix": "01_matrix_outline",
    "blueprint": "01_matrix_outline",
    "sections": "02_section_drafting",
    "figure-review": "02_section_drafting",
    "figures": "03_figure_redraw",
    "draft": "04_first_draft",
    "draft-feedback-loop": "04_first_draft",
    "final-conclusion": "04_first_draft",
    "final-overview-figure": "05_final_audit",
    "final": "05_final_audit",
}


class WorkspaceConfigurationError(ValueError):
    """Raised when a workspace or project path is invalid."""


def _looks_like_review_root(path: Path) -> bool:
    return (path / "review_writer_core").is_dir() and (path / "skills").is_dir()


def discover_review_root(start: str | Path | None = None) -> Path:
    """Find the Review Writer root without relying on ``parents[N]``.

    ``REVIEW_WRITER_ROOT`` has priority.  Otherwise the supplied path (or this
    module) is walked upward until the stable workspace markers are found.
    """
    configured = os.environ.get("REVIEW_WRITER_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        if not _looks_like_review_root(root):
            raise WorkspaceConfigurationError(
                f"REVIEW_WRITER_ROOT is not a Review Writer workspace: {root}"
            )
        return root

    candidate = Path(start).expanduser().resolve() if start else Path(__file__).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for path in (candidate, *candidate.parents):
        if _looks_like_review_root(path):
            return path
    raise WorkspaceConfigurationError(
        f"Could not find a Review Writer workspace above {candidate}. "
        "Set REVIEW_WRITER_ROOT explicitly."
    )


def validate_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not PROJECT_ID_RE.fullmatch(value):
        raise WorkspaceConfigurationError(
            "project-id must be one safe component containing only letters, "
            "numbers, underscores, or hyphens"
        )
    return value


@dataclass(frozen=True)
class WorkspacePaths:
    """Canonical, traversal-safe paths for one workspace."""

    root: Path

    @classmethod
    def discover(cls, start: str | Path | None = None) -> "WorkspacePaths":
        return cls(discover_review_root(start))

    @property
    def projects_root(self) -> Path:
        return self.root / "review-projects"

    @property
    def library_root(self) -> Path:
        return self.root / "review-library"

    @property
    def skills_root(self) -> Path:
        return self.root / "skills"

    def project(self, project_id: str) -> Path:
        return self.projects_root / validate_project_id(project_id)

    def stage(self, project_id: str, stage_id: str) -> Path:
        try:
            directory = STAGE_DIRECTORIES[stage_id]
        except KeyError as exc:
            raise WorkspaceConfigurationError(f"Unknown stage id: {stage_id}") from exc
        return self.project(project_id) / directory
