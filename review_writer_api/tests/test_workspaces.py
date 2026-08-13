from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from review_writer_api.workspaces import HostedWorkspaceManager, WorkspaceAccessError


class HostedWorkspaceManagerTests(unittest.TestCase):
    def test_user_workspace_symlink_cannot_alias_another_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = HostedWorkspaceManager(Path(temporary) / "users")
            first_user = str(uuid.uuid4())
            second_user = str(uuid.uuid4())
            first_root = manager.user_root(first_user)
            alias = manager.root / second_user
            real_resolve = Path.resolve
            real_is_symlink = Path.is_symlink

            def resolve(path: Path, *args, **kwargs) -> Path:
                return first_root if path == alias else real_resolve(path, *args, **kwargs)

            def is_symlink(path: Path) -> bool:
                return path == alias or real_is_symlink(path)

            with patch.object(Path, "resolve", resolve), patch.object(
                Path, "is_symlink", is_symlink
            ):
                with self.assertRaises(WorkspaceAccessError):
                    manager.user_root(second_user)

    def test_project_workspace_symlink_cannot_alias_another_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = HostedWorkspaceManager(Path(temporary) / "users")
            user_id = str(uuid.uuid4())
            projects = manager.user_root(user_id) / "review-projects"
            target = projects / "alpha"
            target.mkdir()
            alias = projects / "beta"
            real_resolve = Path.resolve
            real_is_symlink = Path.is_symlink

            def resolve(path: Path, *args, **kwargs) -> Path:
                return target if path == alias else real_resolve(path, *args, **kwargs)

            def is_symlink(path: Path) -> bool:
                return path == alias or real_is_symlink(path)

            with patch.object(Path, "resolve", resolve), patch.object(
                Path, "is_symlink", is_symlink
            ):
                with self.assertRaises(WorkspaceAccessError):
                    manager.project_path(user_id, "beta")


if __name__ == "__main__":
    unittest.main()
