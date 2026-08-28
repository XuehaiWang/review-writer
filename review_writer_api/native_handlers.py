"""Production JobService handlers for Library and Discovery."""

from __future__ import annotations

import json
import hashlib
import base64
import io
import os
import re
import shutil
import sys
import uuid
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from review_writer_api.model_gateway import ModelGatewayService
from review_writer_api.errors import LiteratureSearchFailed, WorkflowValidationError
from review_writer_api.scientific_runner import ScientificRunFailed, ScientificRunner
from review_writer_api.security import Principal, Role
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.draft_bibliography import repair_numbered_references
from review_writer_core.providers import DEFAULT_IMAGE_MODEL
from review_writer_core.bibliography_audit import audit_bibliography
from review_writer_core.paper_sources.service import default_connectors
from review_writer_core.review_titles import build_publication_overview_text
from review_writer_core.writing_contracts import (
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
)


SENSITIVE_KEY = re.compile(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
DIRECT_PROVIDER_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "REVIEW_WRITING_API_KEY",
    "REVIEW_WRITING_BASE_URL",
    "REVIEW_WRITING_MODEL",
    "REVIEW_WRITING_WIRE_API",
    "REVIEW_CONCLUSION_API_KEY",
    "REVIEW_CONCLUSION_BASE_URL",
    "REVIEW_CONCLUSION_MODEL",
    "REVIEW_CONCLUSION_WIRE_API",
    "IMAGE_OPENAI_API_KEY",
    "IMAGE_OPENAI_BASE_URL",
    "IMAGE_OPENAI_MODEL",
    "IMAGE_FALLBACK_MODEL",
    "IMAGE_OPENAI_WIRE_API",
    "MINERU_API_TOKEN",
)
ARTIFACT_URL = re.compile(r"/api/v1/artifacts/([0-9a-fA-F-]{36})/content")
SAFE_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
EVALUATE_DRAFT_TIMEOUT_SECONDS = 30 * 60
OPTIMIZE_DRAFT_MIN_TIMEOUT_SECONDS = 2 * 60 * 60
OPTIMIZE_DRAFT_TIMEOUT_PER_ITERATION_SECONDS = 45 * 60
OPTIMIZE_DRAFT_MAX_TIMEOUT_SECONDS = 6 * 60 * 60
SECTION_GENERATION_MIN_TIMEOUT_SECONDS = 15 * 60
SECTION_GENERATION_BASE_TIMEOUT_SECONDS = 5 * 60
SECTION_GENERATION_TIMEOUT_PER_PENDING_SECTION_SECONDS = 6 * 60
SECTION_GENERATION_MAX_TIMEOUT_SECONDS = 90 * 60


def _matrix_live_payload(
    status: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    """Build a bounded, UI-safe view of completed Matrix extraction results."""

    entries = checkpoint.get("entries")
    entries = entries if isinstance(entries, dict) else {}
    completed = status.get("completed_papers")
    completed_ids = (
        [str(item) for item in completed if str(item)]
        if isinstance(completed, list)
        else [str(item) for item in entries]
    )
    items: list[dict[str, Any]] = []
    for paper_id in completed_ids:
        entry = entries.get(paper_id)
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        facts = [item for item in result.get("facts") or [] if isinstance(item, dict)]
        tags = result.get("evidence_backed_tags")
        tag_count = (
            sum(len(value) for value in tags.values() if isinstance(value, list))
            if isinstance(tags, dict)
            else 0
        )
        previews = []
        for fact in facts[:3]:
            value = str(fact.get("value") or "").strip()
            previews.append(
                {
                    "fact_id": str(fact.get("fact_id") or ""),
                    "field_id": str(fact.get("field_id") or "fact"),
                    "value": value[:260] + ("…" if len(value) > 260 else ""),
                    "support_level": str(fact.get("support_level") or ""),
                }
            )
        automatic = result.get("automatic_resolution")
        items.append(
            {
                "paper_id": paper_id,
                "status": str(result.get("status") or "complete"),
                "fact_count": len(facts),
                "classification_count": tag_count,
                "automatic_resolution_status": str(
                    automatic.get("status") or ""
                    if isinstance(automatic, dict)
                    else ""
                ),
                "facts_preview": previews,
            }
        )
    return {
        "schema_version": 1,
        "phase": str(status.get("phase") or "extracting"),
        "current": max(0, int(status.get("current") or 0)),
        "total": max(0, int(status.get("total") or 0)),
        "current_paper_id": str(status.get("current_paper_id") or ""),
        "target_axis_ids": [
            str(item) for item in status.get("target_axis_ids") or [] if str(item)
        ],
        "items": items,
    }


def _bibliography_source_names() -> tuple[str, ...]:
    """Use public Crossref by default and keyed sources only when configured."""

    names = ["crossref"]
    if str(os.environ.get("OPENALEX_API_KEY") or "").strip():
        names.append("openalex")
    return tuple(names)


def _bibliography_needs_bounded_agent(audit: Any) -> bool:
    """Use one role-reading fallback only while automatic resolution is incomplete."""

    if not isinstance(audit, dict):
        return True
    if str(audit.get("manual_review_status") or "") in {
        "resolved",
        "supporting_only",
        "rejected",
    }:
        return False
    missing = audit.get("automatic_resolution_missing_fields")
    return str(audit.get("status") or "") != "verified" or bool(
        isinstance(missing, list) and missing
    )


def _section_generation_timeout_seconds(
    tasks: Any, resume_checkpoint: Any = None
) -> int:
    """Scale the subprocess budget to unfinished chapters, preserving resume gains."""

    task_ids = {
        str(task.get("section_id") or "").strip()
        for task in tasks or []
        if isinstance(task, dict) and str(task.get("section_id") or "").strip()
    }
    entries = (
        resume_checkpoint.get("entries")
        if isinstance(resume_checkpoint, dict)
        else None
    )
    completed_ids = {
        str(section_id)
        for section_id, entry in (entries or {}).items()
        if str(section_id) in task_ids and isinstance(entry, dict)
    }
    pending_count = max(1, len(task_ids - completed_ids))
    calculated = (
        SECTION_GENERATION_BASE_TIMEOUT_SECONDS
        + pending_count * SECTION_GENERATION_TIMEOUT_PER_PENDING_SECTION_SECONDS
    )
    return min(
        SECTION_GENERATION_MAX_TIMEOUT_SECONDS,
        max(SECTION_GENERATION_MIN_TIMEOUT_SECONDS, calculated),
    )


def _literature_search_failure_message(error: ScientificRunFailed) -> str:
    diagnostic = str((error.details or {}).get("stderr") or "")
    lowered = diagnostic.casefold()
    if "private destination is blocked" in lowered:
        return (
            "Crossref resolved through a private or transparent-proxy address that is not "
            "trusted by this deployment. Configure REVIEW_WRITER_TRUSTED_PROXY_NETWORKS "
            "and retry."
        )
    if any(marker in lowered for marker in ("[winerror 10060]", "timed out", "timeouterror")):
        return "Crossref did not respond before timeout. Check this server's outbound network or proxy, then retry."
    if any(marker in lowered for marker in ("getaddrinfo failed", "name or service not known", "temporary failure in name resolution", "nodename nor servname")):
        return "Crossref could not be resolved by DNS. Check this server's DNS and outbound network, then retry."
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return "Crossref TLS certificate verification failed. Check this server's certificate trust store or HTTPS proxy."
    if "http error 429" in lowered:
        return "Crossref rate-limited the literature search. Wait briefly and retry; providing a contact email may improve reliability."
    if "http error 403" in lowered:
        return "Crossref rejected the literature search request. Check the server network or proxy policy, then retry."
    if re.search(r"http error 5\d\d", lowered):
        return "Crossref is temporarily unavailable. Retry the literature search shortly."
    return "Crossref literature search failed before results were returned. Check outbound access to https://api.crossref.org and retry."


class NativeWorkflowHandlers:
    def __init__(
        self,
        runner: ScientificRunner,
        workspaces: HostedWorkspaceManager,
        provider_settings: Any | None,
        model_gateway: ModelGatewayService | None = None,
    ):
        self.runner = runner
        self.workspaces = workspaces
        self.provider_settings = provider_settings
        self.model_gateway = model_gateway
        self.root = Path(__file__).resolve().parents[1]

    def mapping(self) -> dict[str, Any]:
        return {
            "library.search": self.library_search,
            "library.download": self.library_download,
            "library.bibliography-audit": self.library_bibliography_audit,
            "discovery.search": self.discovery_search,
            "matrix.enrich": self.matrix_enrich,
            "sections.generate": self.sections_generate,
            "figures.redraw": self.figures_redraw,
            "draft.evaluate": self.draft_evaluate,
            "draft.optimize": self.draft_optimize,
            "draft.rewrite": self.draft_rewrite,
            "draft.accept-rewrite": self.draft_accept_rewrite,
            "final.build": self.final_front_matter,
            "final.conclusion": self.final_conclusion,
            "final.overview": self.final_overview,
            "final.export": self.final_export,
            "final.pdf": self.final_pdf,
        }

    def _environment(self, user_id: str) -> tuple[dict[str, str], dict[str, str]]:
        if self.provider_settings is None:
            return {}, {}
        principal = Principal(user_id, frozenset({Role.USER}))
        values = self.provider_settings.runtime_environment(principal)
        secrets = {key: value for key, value in values.items() if SENSITIVE_KEY.search(key)}
        normal = {key: value for key, value in values.items() if key not in secrets}
        return normal, secrets

    def _text_gateway_environment(self, context) -> tuple[dict[str, str], dict[str, str]]:
        if self.model_gateway is not None:
            # In split-worker deployments the worker must never decrypt or
            # materialize text/image provider credentials. It receives only a
            # lease-bound gateway token.
            return self.model_gateway.environment_for_job(context)
        return self._environment(context.user_id)

    @staticmethod
    def _paper_source_environment() -> tuple[dict[str, str], dict[str, str]]:
        normal_keys = {
            "CROSSREF_MAILTO",
            "REVIEW_DISCOVERY_SOURCES",
            "REVIEW_DISCOVERY_MULTI_SOURCE_ENABLED",
        }
        secret_keys = {"OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"}
        normal = {
            key: str(os.environ.get(key) or "")
            for key in normal_keys
            if str(os.environ.get(key) or "").strip()
        }
        secrets = {
            key: str(os.environ.get(key) or "")
            for key in secret_keys
            if str(os.environ.get(key) or "").strip()
        }
        return normal, secrets

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
            timeout_seconds=5 * 60,
        )
        local_extraction = json.loads(local_output.read_text(encoding="utf-8"))
        if not isinstance(local_extraction, dict):
            raise WorkflowValidationError(
                "Bibliography audit local publication extraction is invalid."
            )
        context.report_progress(2, 6)
        result = audit_bibliography(
            metadata,
            connectors=default_connectors(_bibliography_source_names()),
            pdf_path=pdf_path,
            local_extraction=local_extraction,
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
                timeout_seconds=5 * 60,
            )
            agent_extraction = json.loads(role_output.read_text(encoding="utf-8"))
            if not isinstance(agent_extraction, dict):
                raise WorkflowValidationError(
                    "Bibliography role extraction result is invalid."
                )
            context.report_progress(4, 6)
            if agent_extraction.get("fields"):
                result = audit_bibliography(
                    metadata,
                    connectors=default_connectors(_bibliography_source_names()),
                    pdf_path=pdf_path,
                    local_extraction=local_extraction,
                    document_agent_extraction=agent_extraction,
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

    def _image_gateway_environment(self, context) -> tuple[dict[str, str], dict[str, str]]:
        if self.model_gateway is not None:
            return self.model_gateway.environment_for_job(context)
        return self._environment(context.user_id)

    def _staging(self, user_id: str, job_id: str) -> Path:
        try:
            safe_job_id = str(uuid.UUID(str(job_id)))
        except ValueError as exc:
            raise RuntimeError("Native job identifier is invalid.") from exc
        return self.workspaces.trusted_user_directory(
            user_id, ".review-writer", "job-staging", safe_job_id
        )

    @staticmethod
    def _result(staging: Path, filename: str) -> dict[str, Any]:
        payload = json.loads((staging / filename).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Scientific task result is not an object.")
        return payload

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _assert_safe_project_id(project_id: str) -> str:
        if not SAFE_PROJECT_ID.fullmatch(project_id) or ".." in project_id:
            raise RuntimeError("Project identifier is unsafe for a scientific workspace.")
        return project_id

    @staticmethod
    def _trusted_user_file(user_root: Path, raw: Any) -> Path | None:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            candidate = Path(value).resolve(strict=True)
            relative = candidate.relative_to(user_root.resolve(strict=True))
        except (OSError, ValueError):
            return None
        current = user_root.resolve(strict=True)
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                return None
        return candidate if candidate.is_file() else None

    @staticmethod
    def _replace_artifact_urls(markdown: str, paths: dict[str, Any]) -> str:
        normalized = {str(key): str(value) for key, value in (paths or {}).items()}

        def replacement(match: re.Match[str]) -> str:
            return normalized.get(match.group(1), match.group(0))

        return ARTIFACT_URL.sub(replacement, str(markdown or ""))

    def _compatibility_workspace(
        self,
        context,
        payload: dict[str, Any],
        *,
        name: str,
        markdown_key: str = "draft_text",
    ) -> tuple[Path, Path, Path]:
        """Materialize immutable PostgreSQL payloads for established scientific CLIs."""

        staging = self._staging(context.user_id, context.job_id)
        workspace = staging / name
        if workspace.is_symlink():
            raise RuntimeError("Scientific compatibility workspace is not trusted.")
        if workspace.exists():
            shutil.rmtree(workspace)
        project_id = self._assert_safe_project_id(str(payload["project_id"]))
        project = workspace / "review-projects" / project_id
        planning = project / "01_matrix_outline"
        sections = project / "02_section_drafting"
        figures = project / "03_figure_redraw"
        first = project / "04_first_draft"
        final = project / "05_final_audit"
        for directory in (planning, sections, figures, first, final):
            directory.mkdir(parents=True, exist_ok=True)

        matrix = dict(payload.get("matrix") or {})
        if not isinstance(matrix.get("papers"), list):
            matrix["papers"] = list(matrix.get("rows") or [])
        self._write_json(planning / "literature_matrix.json", matrix)
        self._write_json(
            planning / "section_blueprint.json", dict(payload.get("blueprint") or {})
        )
        section_index = dict(payload.get("section_index") or {})
        self._write_json(sections / "section_drafts.json", section_index)
        self._write_json(
            sections / "section_evidence.json",
            dict(payload.get("section_evidence") or {}),
        )
        self._write_json(
            sections / "writing_plan.json",
            dict(payload.get("writing_plan") or {}),
        )
        self._write_json(
            project / "project_config.json",
            {
                "schema_version": 1,
                "project_id": project_id,
                "taxonomy_profile": str(
                    payload.get("taxonomy_profile") or "general_academic"
                ),
            },
        )
        section_markdown = str(payload.get("section_drafts_md") or "").strip()
        if not section_markdown:
            section_markdown = str(payload.get(markdown_key) or "")
        (sections / "section_drafts.md").write_text(
            section_markdown.rstrip() + "\n", encoding="utf-8"
        )

        artifact_paths = dict(payload.get("figure_artifact_paths") or {})
        figure_manifest = json.loads(
            json.dumps(payload.get("figure_manifest") or {}, ensure_ascii=False)
        )
        for row in figure_manifest.get("figures") or []:
            if not isinstance(row, dict):
                continue
            artifact_id = str(row.get("output_artifact_id") or "")
            if artifact_id and artifact_id in artifact_paths:
                resolved = str(artifact_paths[artifact_id])
                row["redrawn_image"] = resolved
                row["output_path"] = resolved
                row["output_image_path"] = resolved
        self._write_json(figures / "redrawn_figure_manifest.json", figure_manifest)

        draft_text = self._replace_artifact_urls(
            str(payload.get(markdown_key) or ""), artifact_paths
        )
        citation_identity = (
            dict(payload.get("citation_identity") or {})
            if isinstance(payload.get("citation_identity"), dict)
            else {"entries": [], "unresolved_callouts": [], "conflicts": []}
        )
        reference_repair = {
            "status": "not_requested",
            "changed": False,
            "entries": list(citation_identity.get("entries") or []),
            "unresolved_callouts": list(
                citation_identity.get("unresolved_callouts") or []
            ),
            "conflicts": list(citation_identity.get("conflicts") or []),
        }
        if bool(payload.get("repair_references")):
            draft_text, reference_repair = repair_numbered_references(
                draft_text,
                citation_identity,
                matrix,
            )
        self._write_json(first / "reference_repair.json", reference_repair)
        (first / "deterministic_base_draft.md").write_text(
            draft_text.rstrip() + "\n", encoding="utf-8"
        )
        (first / "first_draft.md").write_text(
            draft_text.rstrip() + "\n", encoding="utf-8"
        )
        rewrite_overlays = payload.get("rewrite_overlays")
        if isinstance(rewrite_overlays, dict) and rewrite_overlays:
            self._write_json(first / "feedback_loop_rewrites.json", rewrite_overlays)
        citations = list(
            reference_repair.get("entries")
            or citation_identity.get("entries")
            or []
        )
        self._write_json(first / "citations.json", {"entries": citations})

        metadata_directory = workspace / "review-library" / "metadata" / "papers"
        sources_directory = workspace / "review-library" / "sources"
        user_root = self.workspaces.user_root(context.user_id)
        for paper_id, raw_metadata in (payload.get("library_metadata") or {}).items():
            safe_paper_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(paper_id))[:200]
            metadata = json.loads(json.dumps(raw_metadata or {}, ensure_ascii=False))
            source_paths = metadata.get("source_paths")
            source_paths = dict(source_paths) if isinstance(source_paths, dict) else {}
            copied_paths: dict[str, str] = {}
            for source_kind in ("content_list", "markdown"):
                source = self._trusted_user_file(user_root, source_paths.get(source_kind))
                if source is None:
                    continue
                destination = sources_directory / safe_paper_id / (
                    f"{source_kind}{source.suffix or '.txt'}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied_paths[source_kind] = str(destination)
            metadata["source_paths"] = copied_paths
            self._write_json(
                metadata_directory / f"{safe_paper_id}.metadata.json", metadata
            )
        return staging, workspace, project

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

    def discovery_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        project_slug = self.workspaces.project_path(
            context.user_id, str(payload["project_id"])
        ).name
        screening_cache_dir = self.workspaces.trusted_user_directory(
            context.user_id,
            ".review-writer",
            "cache",
            "discovery-screening",
        )
        query_plan_cache_file = screening_cache_dir / f"{project_slug}.query-plan.json"
        normal, secrets = self._text_gateway_environment(context)
        source_normal, source_secrets = self._paper_source_environment()
        normal.update(source_normal)
        secrets.update(source_secrets)
        source_status_file = staging / "source-search-status.json"
        report_progress = getattr(context, "report_progress", None)
        if callable(report_progress):
            report_progress(1, 6)
        command = [
            sys.executable,
            str(
                self.root
                / "skills"
                / "review-topic-paper-discovery"
                / "scripts"
                / "discover.py"
            ),
            "--review-root",
            str(self.workspaces.user_root(context.user_id)),
            "--project-id",
            str(payload["project_id"]),
            "--topic",
            str(payload["topic"]),
            "--keywords",
            str(payload.get("keywords") or ""),
            "--auto-query-plan",
            "--query-plan-cache",
            str(query_plan_cache_file),
            "--output-project-dir",
            str(staging),
            "--source-status-file",
            str(source_status_file),
        ]
        if payload.get("taxonomy_profile"):
            command.extend(["--taxonomy-profile", str(payload["taxonomy_profile"])])
        if payload.get("web_search"):
            command.append("--web-search")
            if str(
                os.environ.get("REVIEW_DISCOVERY_MULTI_SOURCE_ENABLED", "true")
            ).strip().casefold() in {"0", "false", "no", "off"}:
                command.extend(["--sources", "crossref"])
        if callable(report_progress):
            report_progress(2, 6)
        previous_source_progress = ""

        def publish_discovery_progress() -> None:
            nonlocal previous_source_progress
            if callable(report_progress):
                report_progress(2, 6)
            if not source_status_file.is_file():
                return
            try:
                source_progress = json.loads(source_status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if isinstance(source_progress, dict):
                fingerprint = json.dumps(
                    source_progress, ensure_ascii=False, sort_keys=True
                )
                if fingerprint == previous_source_progress:
                    return
                previous_source_progress = fingerprint
                context.report_partial_result({"source_progress": source_progress})

        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("00_discovery/combined_results_by_keyword.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=(
                publish_discovery_progress
                if callable(report_progress)
                else None
            ),
        )
        if callable(report_progress):
            report_progress(3, 6)
        return self._result(staging, "00_discovery/combined_results_by_keyword.json")

    @staticmethod
    def _section_progress_callback(
        context, status_file: Path, checkpoint_file: Path | None = None
    ):
        """Publish each completed chapter without exposing mutable artifacts."""

        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                current = max(0, int(status.get("current") or 0))
                total = max(0, int(status.get("total") or 0))
                if total:
                    current = min(current, total)
                fingerprint = json.dumps(status, ensure_ascii=False, sort_keys=True)
                if fingerprint == previous:
                    return
                previous = fingerprint
                if hasattr(context, "report_progress"):
                    context.report_progress(current, total)
                if hasattr(context, "report_partial_result"):
                    result = {"section_progress": status}
                    if checkpoint_file is not None and checkpoint_file.is_file():
                        checkpoint = json.loads(
                            checkpoint_file.read_text(encoding="utf-8")
                        )
                        if isinstance(checkpoint, dict):
                            result["section_checkpoint"] = checkpoint
                    context.report_partial_result(result)
            except Exception:
                # Progress reporting is observational and must never invalidate
                # a scientifically valid result that is ready to publish.
                return

        return callback

    @staticmethod
    def _matrix_progress_callback(context, status_file: Path, checkpoint_file: Path):
        """Publish live Matrix facts without exposing mutable staging paths."""

        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                checkpoint = {}
                if checkpoint_file.is_file():
                    loaded = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        checkpoint = loaded
                fingerprint = json.dumps(
                    {"status": status, "checkpoint": checkpoint},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if fingerprint == previous:
                    return
                previous = fingerprint
                current = max(0, int(status.get("current") or 0))
                total = max(0, int(status.get("total") or 0))
                if total:
                    current = min(current, total)
                if hasattr(context, "report_progress"):
                    context.report_progress(current, total)
                if hasattr(context, "report_partial_result"):
                    result = {
                        "matrix_enrichment_progress": status,
                        "matrix_enrichment_live": _matrix_live_payload(
                            status, checkpoint
                        ),
                    }
                    if checkpoint:
                        result["matrix_enrichment_checkpoint"] = checkpoint
                    context.report_partial_result(result)
            except Exception:
                # Live reporting is observational and must never fail extraction.
                return

        return callback

    def sections_generate(self, context, payload):
        """Run the established section writer in an isolated compatibility workspace."""

        staging = self._staging(context.user_id, context.job_id)
        workspace = staging / "section-workspace"
        project_id = str(payload["project_id"])
        project = workspace / "review-projects" / project_id
        planning = project / "01_matrix_outline"
        section_stage = project / "02_section_drafting"
        planning.mkdir(parents=True, exist_ok=True)
        section_stage.mkdir(parents=True, exist_ok=True)
        (planning / "literature_matrix.json").write_text(
            json.dumps(payload["matrix"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (planning / "section_blueprint.json").write_text(
            json.dumps(payload["blueprint"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (planning / "selected_outline.md").write_text(
            str(payload.get("outline_md") or ""), encoding="utf-8"
        )
        (section_stage / "section_tasks.json").write_text(
            json.dumps(payload["tasks"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (section_stage / "section_evidence.json").write_text(
            json.dumps(
                payload.get("evidence_package") or {"schema_version": 1, "sections": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata_directory = workspace / "review-library" / "metadata" / "papers"
        metadata_directory.mkdir(parents=True, exist_ok=True)
        for paper_id, metadata in (payload.get("library_metadata") or {}).items():
            (metadata_directory / f"{paper_id}.metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        discovery_stage = project / "00_discovery"
        discovery_stage.mkdir(parents=True, exist_ok=True)
        selected_ids = list(
            dict.fromkeys(
                str(paper_id)
                for task in payload.get("tasks") or []
                for paper_id in task.get("allowed_papers") or []
            )
        )
        (discovery_stage / "selected_discovery_results.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "local_papers": [
                        {"paper_id": paper_id, "keep": True}
                        for paper_id in selected_ids
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        normal, secrets = self._text_gateway_environment(context)
        relative_stage = Path("section-workspace") / "review-projects" / project_id / "02_section_drafting"
        progress_file = section_stage / "generation_progress.json"
        checkpoint_file = section_stage / "section_checkpoints.json"
        progress_file.unlink(missing_ok=True)
        resume_checkpoint = payload.get("resume_checkpoint")
        if isinstance(resume_checkpoint, dict):
            checkpoint_file.write_text(
                json.dumps(resume_checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            checkpoint_file.unlink(missing_ok=True)
        section_timeout_seconds = _section_generation_timeout_seconds(
            payload.get("tasks"), resume_checkpoint
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-section-drafting-figure-picking"
                    / "scripts"
                    / "generate_section_drafts.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_stage / "section_drafts.json").as_posix(),
                (relative_stage / "section_drafts.md").as_posix(),
                (relative_stage / "section_drafting_report.md").as_posix(),
                (relative_stage / "synthesis_state.json").as_posix(),
                (relative_stage / "writing_plan.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=self._section_progress_callback(
                context, progress_file, checkpoint_file
            ),
            timeout_seconds=section_timeout_seconds,
        )
        drafts = json.loads(
            (section_stage / "section_drafts.json").read_text(encoding="utf-8")
        )
        scripts = (
            self.root
            / "skills"
            / "review-section-drafting-figure-picking"
            / "scripts"
        )
        self.runner.run(
            [
                sys.executable,
                str(scripts / "build_paper_figure_inventory.py"),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=((relative_stage / "paper_figure_inventory.json").as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        self.runner.run(
            [
                sys.executable,
                str(scripts / "select_initial_figure_candidates.py"),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_stage / "paper_figure_candidates.json").as_posix(),
                (relative_stage / "figure_candidates.json").as_posix(),
                (relative_stage / "human_figure_review.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        return {
            "sections": drafts.get("sections") or [],
            "synthesis_state": json.loads(
                (section_stage / "synthesis_state.json").read_text(encoding="utf-8")
            ),
            "writing_plan": json.loads(
                (section_stage / "writing_plan.json").read_text(encoding="utf-8")
            ),
            "section_drafts_md": (section_stage / "section_drafts.md").read_text(
                encoding="utf-8"
            ),
            "report_md": (
                section_stage / "section_drafting_report.md"
            ).read_text(encoding="utf-8"),
            "paper_figure_candidates": json.loads(
                (section_stage / "paper_figure_candidates.json").read_text(
                    encoding="utf-8"
                )
            ),
            "figure_candidates": json.loads(
                (section_stage / "figure_candidates.json").read_text(
                    encoding="utf-8"
                )
            ),
            "default_figure_reviews": json.loads(
                (section_stage / "human_figure_review.json").read_text(
                    encoding="utf-8"
                )
            ),
        }

    def figures_redraw(self, context, payload):
        """Run one established figure redraw in an isolated user/job workspace."""

        staging = self._staging(context.user_id, context.job_id)
        workspace = staging / "figure-workspace"
        project_id = str(payload["project_id"])
        figure = dict(payload["figure"])
        figure_id = str(figure["figure_id"])
        source = Path(str(payload["source_path"])).resolve()
        user_root = self.workspaces.user_root(context.user_id)
        try:
            source.relative_to(user_root)
        except ValueError as exc:
            raise RuntimeError("Figure source escaped its user workspace.") from exc
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("Figure source is unavailable.")
        section_stage = (
            workspace / "review-projects" / project_id / "02_section_drafting"
        )
        source_directory = section_stage / "source"
        source_directory.mkdir(parents=True, exist_ok=True)
        copied_source = source_directory / f"{figure_id}{source.suffix or '.png'}"
        shutil.copy2(source, copied_source)
        figure["source_image_path"] = str(copied_source)
        (section_stage / "figure_candidates.json").write_text(
            json.dumps([figure], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        normal, secrets = self._image_gateway_environment(context)
        requested_figure_type = str(payload.get("figure_type") or "auto")
        image_model = str(
            normal.get("IMAGE_OPENAI_MODEL") or DEFAULT_IMAGE_MODEL
        ).strip() or DEFAULT_IMAGE_MODEL
        relative_manifest = (
            Path("figure-workspace")
            / "review-projects"
            / project_id
            / "03_figure_redraw"
            / "redrawn_figure_manifest.json"
        )
        command = [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-figure-style-redraw"
                    / "scripts"
                    / "redraw_figures.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--figure-id",
                figure_id,
                "--model",
                image_model,
                "--figure-type",
                requested_figure_type,
                "--render-mode",
                "ai-edit",
                "--require-redrawn",
            ]
        if requested_figure_type.strip().casefold() != "auto":
            command.append("--force-standard-ai-edit")
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_manifest.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=20 * 60,
        )
        manifest = json.loads(
            (staging / relative_manifest).read_text(encoding="utf-8")
        )
        row = next(
            (
                item
                for item in manifest.get("figures") or []
                if isinstance(item, dict)
                and str(item.get("figure_id") or "") == figure_id
            ),
            None,
        )
        if not isinstance(row, dict) or row.get("status") != "redrawn":
            raise RuntimeError("The figure redraw did not produce a usable output.")
        output = str(
            row.get("redrawn_image")
            or row.get("output_path")
            or row.get("output_image_path")
            or ""
        )
        if not output:
            raise RuntimeError("The figure redraw output path is missing.")
        return {**row, "output_path": output}

    @staticmethod
    def _restore_artifact_urls(markdown: str, paths: dict[str, Any]) -> str:
        restored = str(markdown or "")
        for artifact_id, raw_path in (paths or {}).items():
            path = str(raw_path or "")
            if path:
                restored = restored.replace(
                    path, f"/api/v1/artifacts/{artifact_id}/content"
                )
        return restored

    @staticmethod
    def _feedback_progress_callback(context, status_file: Path, *, optimize: bool):
        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                fingerprint = json.dumps(status, ensure_ascii=False, sort_keys=True)
                if fingerprint == previous:
                    return
                previous = fingerprint
                if hasattr(context, "report_partial_result"):
                    context.report_partial_result({"feedback_status": status})
                if hasattr(context, "report_progress"):
                    phase = str(status.get("phase") or "")
                    if optimize:
                        current = {
                            "preflight": 1,
                            "source_checking": 2,
                            "scoring": 2,
                            "evaluated": 3,
                            "rewriting": 3,
                            "plateau": 4,
                            "rewrite_blocked": 4,
                            "iteration_limit": 4,
                            "released": 4,
                        }.get(phase, 1)
                        context.report_progress(current, 5)
                    else:
                        current = 2 if phase in {"scoring", "evaluated", "released"} else 1
                        context.report_progress(current, 3)
            except Exception:
                # Progress reporting is observational and must never invalidate
                # a scientifically valid result that is ready to publish.
                return

        return callback

    def _draft_feedback(self, context, payload, *, evaluate_only: bool):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="draft-workspace"
        )
        project_id = str(payload["project_id"])
        relative_first = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
        )
        # Stage 6 children receive only a scoped task token and gateway URL.
        normal, secrets = self._text_gateway_environment(context)
        command = [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "feedback_loop.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--goal",
                str(float(payload.get("goal") or 90)),
                "--paragraph-goal",
                str(float(payload.get("paragraph_goal") or 85)),
                "--max-iterations",
                str(int(payload.get("max_iterations") or 2)),
                "--min-case-words",
                str(int(payload.get("min_case_words") or CASE_PARAGRAPH_MIN_WORDS)),
                "--max-case-words",
                str(int(payload.get("max_case_words") or CASE_PARAGRAPH_MAX_WORDS)),
            ]
        if evaluate_only:
            command.append("--evaluate-only")
        first = project / "04_first_draft"
        max_iterations = int(payload.get("max_iterations") or 2)
        timeout_seconds = (
            EVALUATE_DRAFT_TIMEOUT_SECONDS
            if evaluate_only
            else min(
                OPTIMIZE_DRAFT_MAX_TIMEOUT_SECONDS,
                max(
                    OPTIMIZE_DRAFT_MIN_TIMEOUT_SECONDS,
                    max_iterations * OPTIMIZE_DRAFT_TIMEOUT_PER_ITERATION_SECONDS,
                ),
            )
        )
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_first / "rubric_evaluation.json").as_posix(),
                (relative_first / "reviewer_findings.json").as_posix(),
                (relative_first / "first_draft_gate_status.json").as_posix(),
                (relative_first / "first_draft_preflight.json").as_posix(),
                (relative_first / "original_source_check.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=self._feedback_progress_callback(
                context,
                first / "feedback_loop_status.json",
                optimize=not evaluate_only,
            ),
            timeout_seconds=timeout_seconds,
        )
        # The optimizer retains the best safe candidate per paragraph across
        # iterations.  Their combined manuscript is a new composition, so the
        # incremental candidate score is not authoritative.  Evaluate those
        # exact bytes once as a complete draft before the API can auto-apply
        # the proposal.
        batch_review_file = first / "batch_review_candidates.json"
        if not evaluate_only and batch_review_file.is_file():
            batch_review = json.loads(
                batch_review_file.read_text(encoding="utf-8")
            )
            review_changes = (
                list(batch_review.get("changes") or [])
                if isinstance(batch_review, dict)
                else []
            )
            review_candidate = (
                str(batch_review.get("candidate_draft_text") or "")
                if isinstance(batch_review, dict)
                else ""
            )
            if review_changes and review_candidate.strip():
                status_path = first / "feedback_loop_status.json"
                saved_status = (
                    status_path.read_bytes() if status_path.is_file() else b""
                )
                (first / "first_draft.md").write_text(
                    review_candidate.rstrip() + "\n", encoding="utf-8"
                )
                if hasattr(context, "report_partial_result"):
                    context.report_partial_result(
                        {
                            "feedback_status": {
                                "phase": "validating_full_draft",
                                "review_candidate_count": len(review_changes),
                            }
                        }
                    )
                self.runner.run(
                    [*command, "--evaluate-only"],
                    cwd=self.root,
                    staging_directory=staging,
                    expected_outputs=(
                        (relative_first / "rubric_evaluation.json").as_posix(),
                        (relative_first / "reviewer_findings.json").as_posix(),
                        (relative_first / "first_draft_gate_status.json").as_posix(),
                        (relative_first / "first_draft_preflight.json").as_posix(),
                        (relative_first / "original_source_check.json").as_posix(),
                    ),
                    env=normal,
                    secret_env=secrets,
                    cancel_requested=context.cancellation_requested,
                    progress_callback=self._feedback_progress_callback(
                        context,
                        status_path,
                        optimize=True,
                    ),
                    timeout_seconds=EVALUATE_DRAFT_TIMEOUT_SECONDS,
                )
                exact_evaluation = json.loads(
                    (first / "rubric_evaluation.json").read_text(
                        encoding="utf-8"
                    )
                )
                batch_review["candidate_score"] = round(
                    float(exact_evaluation.get("total_score") or 0), 2
                )
                batch_review["full_draft_evaluated"] = True
                batch_review["full_draft_evaluated_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                self._write_json(batch_review_file, batch_review)
                if saved_status:
                    status_path.write_bytes(saved_status)
        evaluation = json.loads(
            (first / "rubric_evaluation.json").read_text(encoding="utf-8")
        )
        findings = json.loads(
            (first / "reviewer_findings.json").read_text(encoding="utf-8")
        )
        gate = json.loads(
            (first / "first_draft_gate_status.json").read_text(encoding="utf-8")
        )
        preflight = json.loads(
            (first / "first_draft_preflight.json").read_text(encoding="utf-8")
        )
        source_check = json.loads(
            (first / "original_source_check.json").read_text(encoding="utf-8")
        )
        paragraph_scores = {
            str(item.get("paragraph_id") or ""): item
            for item in evaluation.get("paragraph_scores") or []
            if isinstance(item, dict)
        }
        issues = []
        for index, finding in enumerate(findings if isinstance(findings, list) else [], 1):
            if not isinstance(finding, dict):
                continue
            paragraph_id = str(finding.get("paragraph_id") or "")
            paragraph_score = paragraph_scores.get(paragraph_id, {})
            issues.append(
                {
                    **finding,
                    "score": paragraph_score.get("score"),
                    "route": str(
                        paragraph_score.get("route") or finding.get("route") or ""
                    ),
                    "failed_dimensions": list(
                        paragraph_score.get("failed_dimensions") or []
                    ),
                    "issue_id": str(finding.get("id") or f"PAR-{index:03d}"),
                    "message": str(
                        finding.get("diagnosis")
                        or finding.get("recommended_direction")
                        or "Review this paragraph."
                    ),
                }
            )
        status_file = first / "feedback_loop_status.json"
        feedback_status = (
            json.loads(status_file.read_text(encoding="utf-8"))
            if status_file.is_file()
            else {}
        )
        reference_repair_file = first / "reference_repair.json"
        reference_repair = (
            json.loads(reference_repair_file.read_text(encoding="utf-8"))
            if reference_repair_file.is_file()
            else {"status": "not_requested", "changed": False}
        )
        deterministic_base_file = first / "deterministic_base_draft.md"
        deterministic_base_draft_text = (
            self._restore_artifact_urls(
                deterministic_base_file.read_text(encoding="utf-8"),
                dict(payload.get("figure_artifact_paths") or {}),
            )
            if deterministic_base_file.is_file()
            else str(payload.get("draft_text") or "")
        )
        overlay_file = first / "feedback_loop_rewrites.json"
        rewrite_overlays = (
            json.loads(overlay_file.read_text(encoding="utf-8"))
            if overlay_file.is_file()
            else dict(payload.get("rewrite_overlays") or {})
        )
        batch_review_file = first / "batch_review_candidates.json"
        batch_review = (
            json.loads(batch_review_file.read_text(encoding="utf-8"))
            if batch_review_file.is_file()
            else {}
        )
        result = {
            **evaluation,
            "score": float(evaluation.get("total_score") or 0),
            "goal": float(
                evaluation.get("pass_threshold") or payload.get("goal") or 90
            ),
            "issues": issues,
            "hard_gate_failures": list(
                gate.get("hard_gate_failures")
                or evaluation.get("hard_gate_failures")
                or []
            ),
            "preflight": preflight,
            "source_check": source_check,
            "gate": gate,
            "feedback_status": feedback_status,
            "reference_repair": reference_repair,
            "deterministic_base_draft_text": deterministic_base_draft_text,
        }
        if not evaluate_only:
            result["draft_text"] = self._restore_artifact_urls(
                (first / "first_draft.md").read_text(encoding="utf-8"),
                dict(payload.get("figure_artifact_paths") or {}),
            )
            result["rewrite_overlays"] = rewrite_overlays
            if isinstance(batch_review, dict):
                result["review_candidate_draft_text"] = self._restore_artifact_urls(
                    str(batch_review.get("candidate_draft_text") or ""),
                    dict(payload.get("figure_artifact_paths") or {}),
                )
                result["review_candidate_score"] = batch_review.get(
                    "candidate_score"
                )
                result["review_changes"] = list(
                    batch_review.get("changes") or []
                )
                result["review_excluded"] = list(
                    batch_review.get("excluded") or []
                )
                result["review_candidate_full_draft_evaluated"] = bool(
                    batch_review.get("full_draft_evaluated")
                )
        return result

    def draft_evaluate(self, context, payload):
        return self._draft_feedback(context, payload, evaluate_only=True)

    def draft_optimize(self, context, payload):
        return self._draft_feedback(context, payload, evaluate_only=False)

    @staticmethod
    def _retain_manual_confirmation_route(
        candidate_evaluation: dict[str, Any],
        generation_entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Prevent a style-only rewrite from clearing an evidence conflict."""

        if not bool(generation_entry.get("requires_manual_confirmation")):
            return candidate_evaluation
        result = dict(candidate_evaluation)
        paragraph_score = dict(result.get("paragraph_score") or {})
        paragraph_score["score"] = min(
            float(paragraph_score.get("score") or 0),
            79.0,
        )
        paragraph_score["severity"] = "major"
        paragraph_score["route"] = "human_confirmation"
        failed = [
            str(value)
            for value in paragraph_score.get("failed_dimensions") or []
            if str(value).strip()
        ]
        if "manual_source_confirmation" not in failed:
            failed.append("manual_source_confirmation")
        paragraph_score["failed_dimensions"] = failed
        paragraph_score["diagnosis"] = str(
            generation_entry.get("diagnosis")
            or paragraph_score.get("diagnosis")
            or "Manual source or figure-identity confirmation remains required."
        )
        result.update(
            {
                "paragraph_score": paragraph_score,
                "requires_manual_confirmation": True,
                "manual_confirmation_reason": paragraph_score["diagnosis"],
            }
        )
        return result

    def draft_rewrite(self, context, payload):
        """Generate one paragraph candidate, then score only that candidate."""

        if not str(payload.get("draft_text") or "").strip():
            raise WorkflowValidationError(
                "The rewrite task did not receive the current Draft content. "
                "Refresh the Draft and generate the rewrite candidate again."
            )
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="draft-workspace"
        )
        project_id = str(payload["project_id"])
        paragraph_id = str(payload["paragraph_id"])
        first = project / "04_first_draft"
        quality = dict(payload.get("quality") or {})
        goal = float(payload.get("goal") or quality.get("goal") or 90)
        paragraph_goal = float(
            payload.get("paragraph_goal")
            or quality.get("paragraph_pass_threshold")
            or quality.get("paragraph_goal")
            or 85
        )
        min_case_words = int(
            payload.get("min_case_words") or CASE_PARAGRAPH_MIN_WORDS
        )
        max_case_words = int(
            payload.get("max_case_words") or CASE_PARAGRAPH_MAX_WORDS
        )
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, list):
            raw_issues = quality.get("issues") or []
        issues = [
            item
            for item in raw_issues
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ]
        normal, secrets = self._text_gateway_environment(context)
        paragraph_evaluation_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "paragraph_candidate_evaluation.json"
        )
        report_progress = getattr(context, "report_progress", None)
        finding = next(
            (
                dict(item)
                for item in quality.get("paragraph_scores") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        prior_issue = dict(issues[0]) if issues else {}
        if not finding:
            finding = dict(prior_issue)
        if not finding:
            raise RuntimeError(
                "The selected paragraph has no stored score or issue context. "
                "Evaluate the current Draft before generating a candidate."
            )
        finding = {
            **prior_issue,
            **finding,
            "paragraph_id": paragraph_id,
            "severity": str(
                finding.get("severity")
                or prior_issue.get("severity")
                or "minor"
            ),
            "route": str(
                finding.get("route")
                or prior_issue.get("route")
                or "section_rewrite"
            ),
            "diagnosis": str(
                finding.get("diagnosis")
                or prior_issue.get("diagnosis")
                or prior_issue.get("message")
                or "Polish this paragraph while preserving all protected facts and citations."
            ),
            "failed_dimensions": list(
                finding.get("failed_dimensions")
                or prior_issue.get("failed_dimensions")
                or []
            ),
        }

        source_paragraph_evaluation = {
            "evaluation_scope": "single_paragraph",
            "evaluation_mode": "stored_source_score",
            "paragraph_id": paragraph_id,
            "paragraph_score": finding,
        }

        evaluation = {
            **quality,
            "goal": goal,
            "paragraph_pass_threshold": paragraph_goal,
            "paragraph_scores": [finding],
            "paragraph_failures": [finding],
            "blocking_paragraph_failures": [finding],
        }
        self._write_json(first / "rubric_evaluation.json", evaluation)
        source_check = quality.get("source_check")
        source_check = source_check if isinstance(source_check, dict) else {}
        source_entry = next(
            (
                item
                for item in source_check.get("entries") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            None,
        )
        self._write_json(
            first / "original_source_check.json",
            {"entries": [source_entry]} if isinstance(source_entry, dict) and source_entry else {"entries": []},
        )
        preflight = quality.get("preflight")
        self._write_json(
            first / "first_draft_preflight.json",
            preflight if isinstance(preflight, dict) else {"paragraph_checks": []},
        )
        draft_path = first / "first_draft.md"
        digest = hashlib.sha256(draft_path.read_bytes()).hexdigest()
        self._write_json(
            first / "feedback_loop_status.json",
            {
                "status": "completed",
                "phase": "evaluated",
                "goal": goal,
                "paragraph_goal": paragraph_goal,
                "min_case_words": min_case_words,
                "max_case_words": max_case_words,
                "source_draft_sha256": digest,
                "output_draft_sha256": digest,
            },
        )
        relative_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "feedback_rewrite_candidates.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "propose_paragraph_rewrite.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--min-case-words",
                str(min_case_words),
                "--max-case-words",
                str(max_case_words),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        candidates = json.loads(
            (first / "feedback_rewrite_candidates.json").read_text(encoding="utf-8")
        )
        entry = (candidates.get("entries") or {}).get(paragraph_id)
        if not isinstance(entry, dict) or not str(entry.get("candidate_text") or "").strip():
            raise RuntimeError("The paragraph rewrite produced no candidate.")
        candidate_text = str(entry["candidate_text"]).strip()
        if callable(report_progress):
            report_progress(2, 4)

        original_draft = draft_path.read_text(encoding="utf-8")
        requested_paragraph = str(payload["paragraph_text"]).strip()
        # The proposal script is the final reader of the materialized Markdown.
        # Use the exact source span it rewrote for both replacement and the
        # second integrity check.  Mixing this value with an API/UI paragraph
        # projection previously compared a figure-bearing candidate against a
        # prose-only source, causing false image/metadata/number failures and
        # even duplicating the adjacent figure in the temporary scoring draft.
        original_paragraph = str(
            entry.get("original_text") or requested_paragraph
        ).strip()
        normalize = lambda value: " ".join(str(value or "").split())
        if normalize(original_paragraph) != normalize(requested_paragraph):
            raise RuntimeError(
                "The paragraph rewrite source boundary did not match the selected "
                "paragraph; the candidate was discarded before scoring."
            )
        if original_paragraph not in original_draft:
            raise RuntimeError(
                "The selected paragraph changed before candidate scoring."
            )
        candidate_draft = original_draft.replace(
            original_paragraph, candidate_text, 1
        )
        draft_path.write_text(candidate_draft, encoding="utf-8")
        self._write_json(
            first / "paragraph_candidate_evaluation_request.json",
            {
                "evaluation_mode": "accepted_candidate",
                "paragraph_id": paragraph_id,
                "original_text": original_paragraph,
                "candidate_text": candidate_text,
                "allowed_unsupported_claims": list(
                    finding.get("unsupported_claims") or []
                ),
                "word_range_applicable": bool(
                    payload.get("word_range_applicable", True)
                ),
            },
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "evaluate_paragraph_candidate.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--goal",
                str(goal),
                "--paragraph-goal",
                str(paragraph_goal),
                "--min-case-words",
                str(min_case_words),
                "--max-case-words",
                str(max_case_words),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(paragraph_evaluation_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        candidate_evaluation = json.loads(
            (first / "paragraph_candidate_evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        candidate_evaluation = self._retain_manual_confirmation_route(
            candidate_evaluation,
            entry,
        )
        if callable(report_progress):
            report_progress(3, 4)
        return {
            "candidate_text": candidate_text,
            "resolved_issue_ids": [
                str(item.get("issue_id") or item.get("id") or "")
                for item in issues
                if str(item.get("issue_id") or item.get("id") or "")
            ],
            "report": entry,
            "source_paragraph_evaluation": source_paragraph_evaluation,
            "candidate_evaluation": candidate_evaluation,
        }

    def matrix_enrich(self, context, payload):
        """Extract per-paper Matrix facts through the scoped text gateway."""

        staging = self._staging(context.user_id, context.job_id)
        input_path = staging / "matrix-enrichment-input.json"
        output_path = staging / "matrix-enrichment-output.json"
        progress_path = staging / "matrix-enrichment-progress.json"
        checkpoint_path = staging / "matrix-enrichment-checkpoint.json"
        self._write_json(input_path, payload)
        resume_checkpoint = payload.get("resume_checkpoint")
        if isinstance(resume_checkpoint, dict):
            self._write_json(checkpoint_path, resume_checkpoint)
        normal, secrets = self._text_gateway_environment(context)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-literature-matrix-outline"
                    / "scripts"
                    / "enrich_matrix_facts.py"
                ),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--progress",
                str(progress_path),
                "--checkpoint",
                str(checkpoint_path),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("matrix-enrichment-output.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=self._matrix_progress_callback(
                context, progress_path, checkpoint_path
            ),
            timeout_seconds=max(
                30 * 60,
                min(
                    8 * 60 * 60,
                    int(payload.get("pending_paper_count") or 1) * 8 * 60,
                ),
            ),
        )
        result = self._result(staging, "matrix-enrichment-output.json")
        if checkpoint_path.is_file():
            result["matrix_enrichment_checkpoint"] = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
        return result

    def draft_accept_rewrite(self, context, payload):
        """Evaluate only the accepted candidate paragraph in an isolated workspace."""

        staging, workspace, project = self._compatibility_workspace(
            context,
            payload,
            name="draft-workspace",
            markdown_key="candidate_draft_text",
        )
        project_id = str(payload["project_id"])
        paragraph_id = str(payload["paragraph_id"])
        first = project / "04_first_draft"
        self._write_json(
            first / "paragraph_candidate_evaluation_request.json",
            {
                "evaluation_mode": "accepted_candidate",
                "paragraph_id": paragraph_id,
                "original_text": str(payload["paragraph_text"]),
                "candidate_text": str(payload["candidate_text"]),
                "allowed_unsupported_claims": list(
                    payload.get("allowed_unsupported_claims") or []
                ),
                "word_range_applicable": bool(
                    payload.get("word_range_applicable", True)
                ),
            },
        )
        normal, secrets = self._text_gateway_environment(context)
        relative_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "paragraph_candidate_evaluation.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "evaluate_paragraph_candidate.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--goal",
                str(float(payload.get("goal") or 90)),
                "--paragraph-goal",
                str(float(payload.get("paragraph_goal") or 85)),
                "--min-case-words",
                str(int(payload.get("min_case_words") or CASE_PARAGRAPH_MIN_WORDS)),
                "--max-case-words",
                str(int(payload.get("max_case_words") or CASE_PARAGRAPH_MAX_WORDS)),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        return json.loads(
            (first / "paragraph_candidate_evaluation.json").read_text(
                encoding="utf-8"
            )
        )

    def final_conclusion(self, context, payload):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="final-conclusion-workspace"
        )
        project_id = str(payload["project_id"])
        normal, secrets = self._text_gateway_environment(context)
        relative_first = (
            Path("final-conclusion-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-conclusion-generator"
                    / "scripts"
                    / "generate_conclusion1.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--mode",
                "orchestrated",
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_first / "conclusion_generated.md").as_posix(),
                (relative_first / "conclusion_quality_report.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        first = project / "04_first_draft"
        report = json.loads(
            (first / "conclusion_quality_report.json").read_text(encoding="utf-8")
        )
        return {
            "markdown": (first / "conclusion_generated.md").read_text(encoding="utf-8"),
            "report": report,
        }

    def final_front_matter(self, context, payload):
        """Generate only missing/stale machine-owned abstract and keywords."""

        staging = self._staging(context.user_id, context.job_id)
        input_path = staging / "final-front-matter-input.json"
        output_path = staging / "final-front-matter-output.json"
        self._write_json(input_path, payload)
        normal, secrets = self._text_gateway_environment(context)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-final-audit-release"
                    / "scripts"
                    / "generate_front_matter.py"
                ),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("final-front-matter-output.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=10 * 60,
        )
        return self._result(staging, "final-front-matter-output.json")

    def final_overview(self, context, payload):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="final-overview-workspace"
        )
        project_id = str(payload["project_id"])
        output = project / "03_figure_redraw" / "overview_figure.png"
        report_path = project / "03_figure_redraw" / "overview_template_match.json"
        relative_stage = (
            Path("final-overview-workspace")
            / "review-projects"
            / project_id
            / "03_figure_redraw"
        )
        normal, secrets = self._image_gateway_environment(context)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-figure-style-redraw"
                    / "scripts"
                    / "generate_overview_figure.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--output",
                str(output),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_stage / "overview_figure.png").as_posix(),
                (relative_stage / "overview_template_match.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        features = report.get("features") if isinstance(report, dict) else {}
        features = features if isinstance(features, dict) else {}
        blueprint = payload.get("blueprint")
        blueprint = blueprint if isinstance(blueprint, dict) else {}
        raw_topic = str(
            blueprint.get("review_topic")
            or blueprint.get("topic")
            or blueprint.get("review_question")
            or features.get("review_title")
            or project_id
        )
        basis = blueprint.get("classification_basis")
        basis = basis if isinstance(basis, dict) else {}
        editable_text = build_publication_overview_text(
            raw_topic,
            manuscript_title=features.get("display_title") or features.get("manuscript_title"),
            group_by=features.get("group_by") or [],
            classification_rule=(
                features.get("classification_rule")
                or basis.get("description")
                or basis.get("primary_axis")
                or basis.get("overview_axis")
            ),
            has_chirality=bool(features.get("has_chirality")),
            has_reaction_focus=bool(features.get("has_reaction_focus")),
        )
        return {
            "output_path": str(output),
            "editable_text": editable_text,
            "report": report,
        }

    def final_export(self, context, payload):
        staging, _workspace, project = self._compatibility_workspace(
            context,
            payload,
            name="final-export-workspace",
            markdown_key="final_markdown",
        )
        markdown_path = project / "05_final_audit" / "final_draft.md"
        markdown_path.write_text(
            self._replace_artifact_urls(
                str(payload.get("final_markdown") or ""),
                dict(payload.get("figure_artifact_paths") or {}),
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        output = project / "05_final_audit" / "final_draft.docx"
        relative_output = (
            Path("final-export-workspace")
            / "review-projects"
            / str(payload["project_id"])
            / "05_final_audit"
            / "final_draft.docx"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-export-docx"
                    / "scripts"
                    / "run_md2docx.py"
                ),
                "--input",
                str(markdown_path),
                "--output",
                str(output),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env={},
            secret_env={},
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        return {"output_path": str(output), "download_name": "final_draft.docx"}

    def final_pdf(self, context, payload):
        """Render the released manuscript through the locked journal-style LuaLaTeX path."""

        staging = self._staging(context.user_id, context.job_id)
        bundle = staging / "pdf-render-bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        input_path = staging / "pdf-render-input.json"
        self._write_json(
            input_path,
            {
                "final_markdown": str(payload.get("final_markdown") or ""),
                "artifact_paths": dict(payload.get("figure_artifact_paths") or {}),
                "language_profile": str(payload.get("language_profile") or "en"),
                "source_final_artifact_id": payload.get("source_final_artifact_id"),
                "source_release_artifact_id": payload.get("source_release_artifact_id"),
            },
        )
        renderer_url = str(
            os.environ.get("REVIEW_WRITER_PDF_RENDERER_URL") or ""
        ).strip()
        if renderer_url:
            user_root = self.workspaces.user_root(context.user_id)
            assets = []
            for artifact_id, raw_path in sorted(
                dict(payload.get("figure_artifact_paths") or {}).items()
            ):
                source = self._trusted_user_file(user_root, raw_path)
                if source is None:
                    raise RuntimeError("A PDF asset is outside the trusted user workspace.")
                raw = source.read_bytes()
                assets.append(
                    {
                        "artifact_id": str(artifact_id),
                        "filename": source.name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "data_base64": base64.b64encode(raw).decode("ascii"),
                    }
                )
            request = urllib.request.Request(
                renderer_url,
                data=json.dumps(
                    {
                        "final_markdown": str(payload.get("final_markdown") or ""),
                        "language_profile": str(payload.get("language_profile") or "en"),
                        "source_final_artifact_id": payload.get("source_final_artifact_id"),
                        "source_release_artifact_id": payload.get("source_release_artifact_id"),
                        "assets": assets,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": "Bearer "
                    + str(os.environ.get("REVIEW_WRITER_PDF_RENDERER_TOKEN") or ""),
                    "Content-Type": "application/json",
                    "Accept": "application/zip",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=15 * 60) as response:
                    archive_bytes = response.read(200 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as exc:
                renderer_body = exc.read(4000).decode("utf-8", "replace")
                if exc.code == 422:
                    try:
                        renderer_payload = json.loads(renderer_body)
                    except json.JSONDecodeError:
                        renderer_payload = {}
                    renderer_detail = str(
                        renderer_payload.get("detail")
                        if isinstance(renderer_payload, dict)
                        else ""
                    )
                    blocking_checks = sorted(
                        set(
                            re.findall(
                                r'"type"\s*:\s*"([a-z0-9_\-]+)"',
                                renderer_detail,
                                flags=re.IGNORECASE,
                            )
                        )
                    )
                    check_suffix = (
                        " Blocking checks: " + ", ".join(blocking_checks) + "."
                        if blocking_checks
                        else ""
                    )
                    raise WorkflowValidationError(
                        "PDF publication checks failed. Rebuild the current Final "
                        "manuscript to remove unsupported markup or unresolved "
                        "placeholders, then generate the PDF again."
                        + check_suffix,
                        details={
                            "renderer_status": 422,
                            "blocking_checks": blocking_checks,
                        },
                    ) from exc
                raise RuntimeError(
                    f"The isolated PDF renderer rejected the job (HTTP {exc.code})."
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"The isolated PDF renderer is unavailable: {exc}") from exc
            if len(archive_bytes) > 200 * 1024 * 1024:
                raise RuntimeError("The isolated PDF renderer response exceeded 200 MiB.")
            expected = {
                "manuscript.pdf",
                "manuscript.tex",
                "manuscript_state.json",
                "render_manifest.json",
                "pdf_qa.json",
                "compile.log",
            }
            try:
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    names = set(archive.namelist())
                    if names != expected:
                        raise RuntimeError(
                            "The isolated PDF renderer returned an unexpected file set."
                        )
                    total_output_size = sum(
                        archive.getinfo(name).file_size for name in expected
                    )
                    if total_output_size > 200 * 1024 * 1024:
                        raise RuntimeError(
                            "The PDF renderer output bundle exceeded 200 MiB."
                        )
                    for name in sorted(expected):
                        info = archive.getinfo(name)
                        if info.file_size > 150 * 1024 * 1024:
                            raise RuntimeError("A PDF renderer output exceeded 150 MiB.")
                        (bundle / name).write_bytes(archive.read(info))
            except zipfile.BadZipFile as exc:
                raise RuntimeError("The isolated PDF renderer returned an invalid archive.") from exc
        else:
            relative = Path("pdf-render-bundle")
            self.runner.run(
                [
                    sys.executable,
                    str(
                        self.root
                        / "skills"
                        / "review-final-audit-release"
                        / "scripts"
                        / "render_modern_survey_pdf.py"
                    ),
                    "--input-json",
                    str(input_path),
                    "--output-dir",
                    str(bundle),
                ],
                cwd=self.root,
                staging_directory=staging,
                expected_outputs=(
                    (relative / "manuscript.pdf").as_posix(),
                    (relative / "manuscript.tex").as_posix(),
                    (relative / "manuscript_state.json").as_posix(),
                    (relative / "render_manifest.json").as_posix(),
                    (relative / "pdf_qa.json").as_posix(),
                    (relative / "compile.log").as_posix(),
                ),
                cancel_requested=context.cancellation_requested,
                timeout_seconds=12 * 60,
            )
        profile = str(payload.get("language_profile") or "en")
        return {
            "output_path": str(bundle / "manuscript.pdf"),
            "tex_path": str(bundle / "manuscript.tex"),
            "compile_log_path": str(bundle / "compile.log"),
            "manuscript_state": json.loads(
                (bundle / "manuscript_state.json").read_text(encoding="utf-8")
            ),
            "render_manifest": json.loads(
                (bundle / "render_manifest.json").read_text(encoding="utf-8")
            ),
            "pdf_qa": json.loads(
                (bundle / "pdf_qa.json").read_text(encoding="utf-8")
            ),
            "download_name": f"final_draft.{profile}.pdf",
        }
