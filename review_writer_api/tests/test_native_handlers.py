from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from review_writer_api.native_handlers import NativeWorkflowHandlers
from review_writer_api.workspaces import HostedWorkspaceManager


class _Context:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.job_id = str(uuid.uuid4())

    @staticmethod
    def cancellation_requested() -> bool:
        return False


class _RecordingRunner:
    def __init__(self):
        self.command: tuple[str, ...] = ()

    def run(self, command, **_kwargs):
        self.command = tuple(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "added_count": 0,
                    "already_present_count": 0,
                    "failed_count": 0,
                    "results": [],
                }
            ),
            encoding="utf-8",
        )


class NativeWorkflowHandlerTests(unittest.TestCase):
    def test_download_scientific_process_writes_only_to_job_isolated_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _RecordingRunner()
            handlers = NativeWorkflowHandlers(runner, workspaces, None)
            context = _Context(str(uuid.uuid4()))

            result = handlers.library_download(
                context,
                {"candidates": [{"candidate_id": "crossref:1"}]},
            )

            task_root = Path(
                runner.command[runner.command.index("--review-root") + 1]
            )
            user_root = workspaces.user_root(context.user_id)
            self.assertEqual(
                user_root
                / ".review-writer"
                / "job-staging"
                / context.job_id
                / "library-workspace",
                task_root,
            )
            self.assertNotEqual(user_root, task_root)
            self.assertEqual([], result["results"])


if __name__ == "__main__":
    unittest.main()
