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
        lexical = self.root / safe_user_id
        if lexical.is_symlink():
            raise WorkspaceAccessError("User workspace is not trusted.")
        candidate = lexical.resolve()
        if candidate.parent != self.root:
            raise WorkspaceAccessError("User workspace escaped the configured root.")
        candidate.mkdir(exist_ok=True)
        if lexical.is_symlink():
            raise WorkspaceAccessError("User workspace is not trusted.")
        self._ensure_layout(candidate)
        return candidate

    def project_path(self, user_id: str, project_slug: str) -> Path:
        try:
            safe_slug = validate_project_id(project_slug)
        except WorkspaceConfigurationError as exc:
            raise WorkspaceAccessError(str(exc)) from exc
        user_root = self.user_root(user_id)
        projects_root = (user_root / "review-projects").resolve()
        lexical = projects_root / safe_slug
        if lexical.is_symlink():
            raise WorkspaceAccessError("Project workspace is not trusted.")
        candidate = lexical.resolve()
        if candidate.parent != projects_root:
            raise WorkspaceAccessError(
                "Project workspace escaped the authenticated user root."
            )
        return candidate

    def trusted_user_directory(self, user_id: str, *parts: str) -> Path:
        """Resolve/create a user-owned internal directory without traversing symlinks."""
        current = self.user_root(user_id)
        for part in parts:
            if not part or part in {".", ".."} or Path(part).name != part:
                raise WorkspaceAccessError("Workspace directory component is invalid.")
            candidate = current / part
            if candidate.is_symlink():
                raise WorkspaceAccessError(
                    "Workspace internal directory is not trusted."
                )
            candidate.mkdir(exist_ok=True)
            resolved = candidate.resolve()
            if candidate.is_symlink() or resolved.parent != current:
                raise WorkspaceAccessError(
                    "Workspace internal directory escaped the user root."
                )
            current = resolved
        return current

    @classmethod
    def _ensure_layout(cls, user_root: Path) -> None:
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
            relative = directory.relative_to(user_root)
            current = user_root
            for part in relative.parts:
                candidate = current / part
                if candidate.is_symlink():
                    raise WorkspaceAccessError(
                        "Workspace internal directory is not trusted."
                    )
                candidate.mkdir(exist_ok=True)
                resolved = candidate.resolve()
                if candidate.is_symlink() or resolved.parent != current:
                    raise WorkspaceAccessError(
                        "Workspace internal directory escaped the user root."
                    )
                current = resolved
