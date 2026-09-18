"""One source-bound paragraph revision; single and batch jobs share this path."""

import json
import sys
from typing import Any

from review_writer_api.errors import WorkflowValidationError


class DraftJobHandlers:
    def draft_synthesis(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        input_path = staging / "draft-synthesis-input.json"
        self._write_json(input_path, payload)
        retry_of = getattr(context, "retry_of_job_id", None)
        output = staging / "draft-synthesis-output.json"
        if retry_of and not output.exists():
            previous = self._staging(context.user_id, retry_of) / output.name
            if previous.is_file() and not previous.is_symlink():
                self._write_json(output, json.loads(previous.read_text(encoding="utf-8")))
        normal, secrets = self._text_gateway_environment(context)
        self.runner.run([sys.executable, "-m", "review_writer_core.draft_synthesis",
            "--input", str(input_path), "--output", str(staging / "draft-synthesis-output.json")],
            cwd=self.root, staging_directory=staging, expected_outputs=("draft-synthesis-output.json",),
            env=normal, secret_env=secrets, cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60)
        return self._result(staging, "draft-synthesis-output.json")

    @staticmethod
    def _restore_artifact_urls(markdown: str, paths: dict[str, Any]) -> str:
        restored = str(markdown or "")
        for artifact_id, raw_path in (paths or {}).items():
            if raw_path:
                restored = restored.replace(str(raw_path), f"/api/v1/artifacts/{artifact_id}/content")
        return restored

    def draft_rewrite(self, context, payload):
        if payload.get("revision_mode") != "dialogue":
            raise WorkflowValidationError("This legacy revision task is retired. Start a paragraph dialogue from the current Draft.")
        if not str(payload.get("draft_text") or "").strip():
            raise WorkflowValidationError("The rewrite task did not receive the current Draft content.")
        staging, _workspace, project = self._compatibility_workspace(context, payload, name="draft-workspace")
        normal, secrets = self._text_gateway_environment(context)
        first = project / "04_first_draft"
        self._write_json(first / "paragraph_revision_request.json", payload["dialogue"])
        output = first / "paragraph_revision_result.json"
        self.runner.run(
            [sys.executable, str(self.root / "skills" / "review-first-draft-feedback-loop" / "scripts" / "revise_paragraph.py"),
             "--project", str(project)],
            cwd=self.root, staging_directory=staging,
            expected_outputs=(output.relative_to(staging).as_posix(),),
            env=normal, secret_env=secrets, cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60, max_attempts=1,
        )
        return json.loads(output.read_text(encoding="utf-8"))
