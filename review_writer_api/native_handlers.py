"""Shared runtime and registry for stage-specific native job handlers."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from review_writer_api.model_gateway import ModelGatewayService
from review_writer_api.job_handlers.final import FinalJobHandlers
from review_writer_api.job_handlers.discovery import DiscoveryJobHandlers
from review_writer_api.job_handlers.draft import DraftJobHandlers
from review_writer_api.job_handlers.figures import FigureJobHandlers
from review_writer_api.job_handlers.library import LibraryJobHandlers
from review_writer_api.job_handlers.planning import PlanningJobHandlers
from review_writer_api.job_handlers.sections import SectionJobHandlers
from review_writer_api.job_handlers.model_dispatch import ModelDispatchJobHandlers
from review_writer_api.scientific_runner import ScientificRunner
from review_writer_api.security import Principal, Role
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.atomic_io import atomic_write_json
from review_writer_core.draft_bibliography import repair_numbered_references
from review_writer_core.draft_quality import full_draft_quality_mismatch_reasons, quality_input_artifact_ids
from review_writer_core.paragraph_markers import parse_marked_paragraphs


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


class NativeWorkflowHandlers(
    DiscoveryJobHandlers,
    DraftJobHandlers,
    FigureJobHandlers,
    LibraryJobHandlers,
    PlanningJobHandlers,
    SectionJobHandlers,
    ModelDispatchJobHandlers,
    FinalJobHandlers,
):
    def __init__(
        self,
        runner: ScientificRunner,
        workspaces: HostedWorkspaceManager,
        provider_settings: Any | None,
        model_gateway: ModelGatewayService | None = None,
        planning_service: Any | None = None,
        sections_service: Any | None = None,
    ):
        self.runner = runner
        self.workspaces = workspaces
        self.provider_settings = provider_settings
        self.model_gateway = model_gateway
        self.planning_service = planning_service
        self.sections_service = sections_service
        self.root = Path(__file__).resolve().parents[1]

    def mapping(self) -> dict[str, Any]:
        return {
            "model.dispatch": self.model_dispatch,
            "library.search": self.library_search,
            "library.download": self.library_download,
            "library.bibliography-audit": self.library_bibliography_audit,
            "discovery.search": self.discovery_search,
            "matrix.enrich": self.matrix_enrich,
            "planning.blueprint": self.blueprint_plan,
            "planning.topic-outline": self.topic_outline,
            "sections.generate": self.sections_generate,
            "figures.redraw": self.figures_redraw,
            "draft.rewrite": self.draft_rewrite,
            "draft.synthesis": self.draft_synthesis,
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
        atomic_write_json(path, payload)

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
        if payload.get("baseline_writing_plan") is not None:
            # Task-local audit input, not a new published business artifact.
            self._write_json(sections / "baseline_writing_plan.json", payload["baseline_writing_plan"])
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
        prior_quality_context = dict(payload.get("prior_quality_context") or {})
        if payload.get("draft_source_check"):
            prior_quality_context["source_check"] = payload["draft_source_check"]
        if isinstance(prior_quality_context, dict) and prior_quality_context:
            self._write_json(
                first / "prior_quality_context.json", prior_quality_context
            )
        baseline_quality = payload.get("baseline_quality")
        if isinstance(baseline_quality, dict) and baseline_quality:
            original = str(payload.get(markdown_key) or "")
            restored = self._restore_artifact_urls(draft_text, artifact_paths)
            if (not reference_repair.get("changed")
                    and restored.rstrip() == original.rstrip()
                    and not full_draft_quality_mismatch_reasons(
                        baseline_quality, draft_text=original,
                        paragraphs=parse_marked_paragraphs(original),
                        input_artifact_ids=quality_input_artifact_ids(payload))):
                # Only this task-local copy covers the reversible image-path
                # projection. Keep the persisted quality and payload immutable.
                baseline_quality = {**baseline_quality,
                    "source_evaluation_input_sha256": baseline_quality["evaluation_input_sha256"],
                    "evaluation_input_sha256": hashlib.sha256((first / "first_draft.md").read_bytes()).hexdigest()}
            self._write_json(first / "baseline_quality.json", baseline_quality)
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
