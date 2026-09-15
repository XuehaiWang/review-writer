"""Library-stage native job handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from review_writer_api.errors import LiteratureSearchFailed, WorkflowValidationError
from review_writer_api.job_handlers.support import (
    bibliography_needs_bounded_agent as _bibliography_needs_bounded_agent,
    bibliography_source_names as _bibliography_source_names,
    literature_search_failure_message as _literature_search_failure_message,
)
from review_writer_api.scientific_runner import ScientificRunFailed
from review_writer_core.bibliography_audit import audit_bibliography
from review_writer_core.mineru_bibliography import (
    as_document_audit_extraction,
    extract_mineru_bibliography,
)
from review_writer_core.paper_sources.service import default_connectors
from review_writer_core.publication_metadata import read_pdf_first_page_text


class LibraryJobHandlers:
    def library_bibliography_audit(self, context, payload):
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise WorkflowValidationError("Bibliography audit requires current metadata.")

        def library_file(raw_path: Any, label: str) -> Path:
            root = self.workspaces.user_root(context.user_id).resolve()
            relative = PurePosixPath(str(raw_path or ""))
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise WorkflowValidationError(f"Bibliography audit {label} path is invalid.")
            resolved = (root / Path(*relative.parts)).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise WorkflowValidationError(
                    f"Bibliography audit {label} path is invalid."
                ) from exc
            if not resolved.is_file():
                raise WorkflowValidationError(
                    f"Bibliography audit {label} is unavailable."
                )
            return resolved

        pdf_path = library_file(payload.get("pdf_relative_path"), "PDF")
        markdown_path = library_file(payload.get("markdown_relative_path"), "Markdown")
        staging = self._staging(context.user_id, context.job_id)
        local_output = staging / "publication-date-extraction.json"
        normal, secrets = self._text_gateway_environment(context)
        context.report_progress(1, 6)
        self.runner.run(
            [
                sys.executable,
                "-m",
                "review_writer_api.scientific_tasks",
                "publication-date-extract",
                "--pdf",
                str(pdf_path),
                "--markdown",
                str(markdown_path),
                "--filename",
                str(pdf_path.name),
                "--output",
                str(local_output),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("publication-date-extraction.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=120,
            max_attempts=1,
        )
        local_extraction = json.loads(local_output.read_text(encoding="utf-8"))
        if not isinstance(local_extraction, dict):
            raise WorkflowValidationError(
                "Bibliography audit local publication extraction is invalid."
            )
        context.report_progress(2, 6)
        markdown_text = markdown_path.read_text(encoding="utf-8", errors="replace")
        mineru_document_extraction = as_document_audit_extraction(
            extract_mineru_bibliography(
                [],
                markdown_text,
                filename=str(pdf_path.name),
                pdf_first_page_text=read_pdf_first_page_text(pdf_path),
            )
        )
        result = audit_bibliography(
            metadata,
            connectors=default_connectors(_bibliography_source_names()),
            pdf_path=pdf_path,
            local_extraction=local_extraction,
            document_agent_extraction=mineru_document_extraction,
            network_mode=str(payload.get("network_mode") or "fallback"),
            previous_audit=(
                payload.get("previous_audit")
                if isinstance(payload.get("previous_audit"), dict)
                else None
            ),
        )
        context.report_progress(3, 6)
        agent_gateway_available = bool(
            str(normal.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "").strip()
            and str(secrets.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
        )
        if _bibliography_needs_bounded_agent(result) and agent_gateway_available:
            metadata_input = staging / "bibliography-role-metadata.json"
            role_output = staging / "bibliography-role-extraction.json"
            self._write_json(metadata_input, metadata)
            self.runner.run(
                [
                    sys.executable,
                    "-m",
                    "review_writer_api.scientific_tasks",
                    "bibliography-role-extract",
                    "--markdown",
                    str(markdown_path),
                    "--metadata",
                    str(metadata_input),
                    "--output",
                    str(role_output),
                ],
                cwd=self.root,
                staging_directory=staging,
                expected_outputs=("bibliography-role-extraction.json",),
                env=normal,
                secret_env=secrets,
                cancel_requested=context.cancellation_requested,
                timeout_seconds=120,
                max_attempts=1,
            )
            agent_extraction = json.loads(role_output.read_text(encoding="utf-8"))
            if not isinstance(agent_extraction, dict):
                raise WorkflowValidationError(
                    "Bibliography role extraction result is invalid."
                )
            context.report_progress(4, 6)
            if agent_extraction.get("fields"):
                combined_extraction = {
                    **mineru_document_extraction,
                    "status": "reliable",
                    "method": "mineru_deterministic+bounded_document_agent",
                    "model_attempted": True,
                    "fields": {
                        **dict(mineru_document_extraction.get("fields") or {}),
                        **dict(agent_extraction.get("fields") or {}),
                    },
                }
                result = audit_bibliography(
                    metadata,
                    connectors=default_connectors(_bibliography_source_names()),
                    pdf_path=pdf_path,
                    local_extraction=local_extraction,
                    document_agent_extraction=combined_extraction,
                    network_mode=str(payload.get("network_mode") or "fallback"),
                    previous_audit=result,
                )
                context.report_progress(5, 6)
            else:
                result = {
                    **result,
                    "document_agent_extraction": agent_extraction,
                }
        elif _bibliography_needs_bounded_agent(result):
            result = {
                **result,
                "document_agent_extraction": {
                    "schema_version": 1,
                    "status": "unavailable",
                    "method": "bounded_document_agent",
                    "fields": {},
                    "model_attempted": False,
                    "model_error": "The internal model gateway is unavailable.",
                },
            }
        context.repository.update_job_progress(context.job_id, 6, 6)
        return result

    def library_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        command = [
            sys.executable,
            "-m",
            "review_writer_api.scientific_tasks",
            "literature-search",
            "--review-root",
            str(self.workspaces.user_root(context.user_id)),
            "--output",
            str(staging / "search-result.json"),
            "--topic",
            str(payload["topic"]),
            "--limit",
            str(max(1, min(int(payload.get("limit") or 20), 50))),
        ]
        for key, flag in (("year_from", "--year-from"), ("year_to", "--year-to")):
            if payload.get(key) is not None:
                command.extend([flag, str(int(payload[key]))])
        if payload.get("email"):
            command.extend(["--mailto", str(payload["email"])])
        try:
            self.runner.run(
                command,
                cwd=self.root,
                staging_directory=staging,
                expected_outputs=("search-result.json",),
                env={},
                secret_env={},
                cancel_requested=context.cancellation_requested,
            )
        except ScientificRunFailed as exc:
            raise LiteratureSearchFailed(
                _literature_search_failure_message(exc),
                details={
                    "attempts": exc.attempts,
                    "category": str((exc.details or {}).get("category") or "unknown"),
                },
            ) from exc
        return self._result(staging, "search-result.json")

    def library_download(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        task_workspace = staging / "library-workspace"
        if task_workspace.is_symlink():
            raise RuntimeError("Library download workspace is not trusted.")
        task_workspace.mkdir(exist_ok=True)
        (staging / "candidates.json").write_text(
            json.dumps(payload["candidates"], ensure_ascii=False), encoding="utf-8"
        )
        command = [
            sys.executable,
            "-m",
            "review_writer_api.scientific_tasks",
            "literature-download",
            "--review-root",
            str(task_workspace),
            "--input",
            str(staging / "candidates.json"),
            "--output",
            str(staging / "download-result.json"),
        ]
        if payload.get("email"):
            command.extend(["--email", str(payload["email"])])
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("download-result.json",),
            # OA resolution uses Crossref/Europe PMC/Semantic Scholar and the
            # optional email supplied in the request. It must not depend on
            # unrelated text, image, or MinerU provider settings.
            env={},
            secret_env={},
            cancel_requested=context.cancellation_requested,
            timeout_seconds=35 * 60,
        )
        return self._result(staging, "download-result.json")
