"""User-isolated filesystem workspaces for the review workflow."""

from __future__ import annotations

import uuid
from pathlib import Path

from review_writer_core.workspace import WorkspaceConfigurationError, validate_project_id


class WorkspaceAccessError(ValueError):
    pass


class HostedWorkspaceManager:
    """Give every authenticated user an independent workflow review root."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def user_root(self, user_id: str) -> Path:
        try:
            safe_user_id = str(uuid.UUID(str(user_id)))
        except ValueError as exc:
            raise WorkspaceAccessError("Authenticated user ID is invalid.") from exc
        candidate = (self.root / safe_user_id).resolve()
        if candidate.parent != self.root:
            raise WorkspaceAccessError("User workspace escaped the configured root.")
        self._ensure_layout(candidate)
        return candidate

    def project_path(self, user_id: str, project_slug: str) -> Path:
        try:
            safe_slug = validate_project_id(project_slug)
        except WorkspaceConfigurationError as exc:
            raise WorkspaceAccessError(str(exc)) from exc
        user_root = self.user_root(user_id)
        projects_root = (user_root / "review-projects").resolve()
        candidate = (projects_root / safe_slug).resolve()
        if candidate.parent != projects_root:
            raise WorkspaceAccessError("Project workspace escaped the authenticated user root.")
        return candidate

    @staticmethod
    def _ensure_layout(user_root: Path) -> None:
        directories = (
            user_root / "review-projects",
            user_root / "review-library" / "metadata" / "papers",
            user_root / "review-library" / "metadata" / "extraction_prompts",
            user_root / "review-library" / "registry",
            user_root / "review-library" / "uploads",
            user_root / "review-library" / "downloads",
            user_root / ".review-writer",
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
