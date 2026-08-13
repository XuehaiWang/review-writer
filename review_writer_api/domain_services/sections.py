"""PostgreSQL-native section tasks, generation, reports, and handoff."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import database_session, utc_now
from review_writer_api.domain_services.planning import (
    BLUEPRINT_LOGICAL_NAME,
    MATRIX_LOGICAL_NAME,
    OUTLINE_LOGICAL_NAME,
)
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowError,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord, JobRecord, WorkflowRepository


SECTION_INDEX_LOGICAL_NAME = "sections/section_drafts.json"


class BlueprintPapersMissing(WorkflowConflict):
    code = "BLUEPRINT_PAPERS_MISSING"


class SectionOutputsMissing(WorkflowConflict):
    code = "SECTION_OUTPUTS_NOT_CURRENT"


class SectionProviderUnavailable(WorkflowError):
    code = "SECTION_PROVIDER_UNAVAILABLE"
    status_code = 503
    retryable = True


def _job_payload(job: JobRecord) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "job_type": job.job_type,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class SectionsService:
    def __init__(self, repository: WorkflowRepository, artifacts: ArtifactService):
        self.repository = repository
        self.artifacts = artifacts
        self._write_lock = threading.RLock()

    def _owned_project(self, principal: Principal, project_id: str):
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

    def _read_json_artifact(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[dict[str, Any] | None, ArtifactRecord | None]:
        self._owned_project(principal, project_id)
        artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )
        if artifact is None:
            if required:
                raise WorkflowNotFound("Current workflow artifact not found.")
            return None, None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict("The current workflow artifact is unreadable.") from exc
        if not isinstance(payload, dict):
            raise WorkflowConflict("The current workflow artifact is invalid.")
        return payload, artifact

    @staticmethod
    def tasks_from_blueprint(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
        sections = blueprint.get("sections")
        if not isinstance(sections, list) or not sections:
            raise WorkflowConflict(
                "No Blueprint sections are available. Generate Blueprint first."
            )
        tasks: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict) or not str(section.get("section_id") or ""):
                continue
            claims = section.get("review_claims") or []
            tasks.append(
                {
                    "section_id": str(section["section_id"]),
                    "heading": str(section.get("title") or section["section_id"]),
                    "core_argument": str(
                        section.get("section_thesis")
                        or section.get("review_problem")
                        or ""
                    ),
                    "allowed_papers": [
                        str(paper_id)
                        for paper_id in section.get("major_papers") or []
                        if str(paper_id or "").strip()
                    ],
                    "must_cover_points": [
                        str(claim.get("claim") or "")
                        for claim in claims
                        if isinstance(claim, dict) and claim.get("claim")
                    ],
                    "avoid_points": [
                        str(item) for item in section.get("avoid_patterns") or []
                    ],
                    "figure_need": section.get("figure_or_table_needs") or [],
                }
            )
        if not tasks:
            raise WorkflowConflict("Blueprint contains no usable section tasks.")
        return tasks

    def _catalog(
        self, principal: Principal, paper_ids: list[str]
    ) -> dict[str, LibraryPaper]:
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.repository.session_factory) as session:
            papers = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == user_uuid,
                    LibraryPaper.paper_id.in_(paper_ids),
                    LibraryPaper.deleted_at.is_(None),
                    LibraryPaper.status == "active",
                )
            ).all()
        return {paper.paper_id: paper for paper in papers}

    def generation_payload(
        self, principal: Principal, project_id: str
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        blueprint, blueprint_artifact = self._read_json_artifact(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        blueprint_state = self.repository.get_stage_state(
            principal.user_id, project_id, "blueprint"
        )
        if blueprint_state is None or blueprint_state.status != "approved":
            raise WorkflowConflict(
                "Confirm the current Blueprint before generating section drafts."
            )
        matrix, matrix_artifact = self._read_json_artifact(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        outline, outline_artifact = self._read_json_artifact(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        tasks = self.tasks_from_blueprint(blueprint)
        matrix_rows = matrix.get("rows") if isinstance(matrix, dict) else None
        if not isinstance(matrix_rows, list):
            raise WorkflowConflict("The current Matrix is invalid.")
        matrix_ids = {
            str(row.get("paper_id"))
            for row in matrix_rows
            if isinstance(row, dict) and row.get("paper_id")
        }
        assigned = list(
            dict.fromkeys(
                paper_id for task in tasks for paper_id in task["allowed_papers"]
            )
        )
        catalog = self._catalog(principal, assigned)
        missing = sorted(
            paper_id
            for paper_id in assigned
            if paper_id not in matrix_ids or paper_id not in catalog
        )
        if missing:
            raise BlueprintPapersMissing(
                "Blueprint contains papers that are missing from the current Matrix or active Library.",
                details={"paper_ids": missing},
            )
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "sections"
        )
        return {
            "project_id": project_id,
            "source_blueprint_artifact_id": blueprint_artifact.id,
            "source_matrix_artifact_id": matrix_artifact.id,
            "source_outline_artifact_id": outline_artifact.id,
            "expected_sections_revision": state.revision if state else 0,
            "blueprint": blueprint,
            "matrix": matrix,
            "outline_md": str(outline.get("outline_md") or ""),
            "tasks": tasks,
        }

    def publish_generation(
        self,
        principal: Principal,
        project_id: str,
        payload: dict[str, Any],
        built: dict[str, Any],
        *,
        attempts: int,
    ) -> dict[str, Any]:
        current_blueprint = self.repository.get_current_artifact(
            principal.user_id, project_id, BLUEPRINT_LOGICAL_NAME
        )
        current_matrix = self.repository.get_current_artifact(
            principal.user_id, project_id, MATRIX_LOGICAL_NAME
        )
        if (
            current_blueprint is None
            or current_matrix is None
            or current_blueprint.id != payload["source_blueprint_artifact_id"]
            or current_matrix.id != payload["source_matrix_artifact_id"]
        ):
            raise WorkflowConflict(
                "Planning changed while sections were being generated. Run section generation again."
            )
        expected_tasks = {task["section_id"]: task for task in payload["tasks"]}
        generated = built.get("sections") if isinstance(built, dict) else None
        if not isinstance(generated, list) or not generated:
            raise WorkflowValidationError("Section generation returned no usable sections.")
        by_id = {
            str(section.get("section_id")): section
            for section in generated
            if isinstance(section, dict) and section.get("section_id")
        }
        if set(by_id) != set(expected_tasks):
            raise WorkflowValidationError(
                "Section generation did not return the current Blueprint section set.",
                details={
                    "expected": sorted(expected_tasks),
                    "actual": sorted(by_id),
                },
            )
        index_sections: list[dict[str, Any]] = []
        files: dict[str, tuple[bytes, str]] = {}
        for section_id, task in expected_tasks.items():
            section = by_id[section_id]
            markdown = str(section.get("draft_md") or "").strip()
            if not markdown:
                raise WorkflowValidationError(
                    "A generated section is missing Markdown content.",
                    details={"section_id": section_id},
                )
            cited = {
                str(paper_id)
                for paragraph in section.get("paragraphs") or []
                if isinstance(paragraph, dict)
                for paper_id in (
                    paragraph.get("cited_paper_ids")
                    or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                )
            }
            unknown = sorted(cited - set(task["allowed_papers"]))
            if unknown:
                raise WorkflowValidationError(
                    "A generated section cited papers outside its Blueprint task.",
                    details={"section_id": section_id, "paper_ids": unknown},
                )
            logical = f"sections/{section_id}.md"
            files[logical] = ((markdown + "\n").encode("utf-8"), "markdown")
            index_sections.append(
                {
                    **section,
                    "draft_md": markdown + "\n",
                    "logical_name": logical,
                }
            )
        merged = str(built.get("section_drafts_md") or "").strip()
        if not merged:
            merged = "\n\n".join(section["draft_md"] for section in index_sections)
        report_md = str(built.get("report_md") or "").strip()
        index = {
            "project_id": project_id,
            "source_blueprint_artifact_id": payload["source_blueprint_artifact_id"],
            "source_matrix_artifact_id": payload["source_matrix_artifact_id"],
            "generated_at": utc_now().isoformat(),
            "sections": index_sections,
            "section_drafts_md": merged + "\n",
            "report_md": report_md + "\n" if report_md else "",
        }
        files[SECTION_INDEX_LOGICAL_NAME] = (
            (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "json",
        )
        with self._write_lock:
            run = self.repository.create_stage_run(
                principal.user_id,
                project_id,
                "sections",
                status="succeeded",
                attempt=max(1, int(attempts)),
                input_snapshot={
                    "blueprint_artifact_id": payload["source_blueprint_artifact_id"],
                    "matrix_artifact_id": payload["source_matrix_artifact_id"],
                    "section_ids": list(expected_tasks),
                },
            )
            staging = self.artifacts.stage_run_directory(
                principal.user_id, project_id, run.id
            )
            published: dict[str, ArtifactRecord] = {}
            for index_number, (logical_name, (content, artifact_type)) in enumerate(
                files.items()
            ):
                filename = f"{index_number:03d}-{Path(logical_name).name}"
                (staging / filename).write_bytes(content)
                published[logical_name] = self.artifacts.publish(
                    principal.user_id,
                    project_id,
                    run.id,
                    filename,
                    logical_name=logical_name,
                    artifact_type=artifact_type,
                    producer_stage="sections",
                    make_current=False,
                    metadata={
                        "source_blueprint_artifact_id": payload[
                            "source_blueprint_artifact_id"
                        ]
                    },
                )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "sections",
                artifact_ids={
                    logical_name: artifact.id
                    for logical_name, artifact in published.items()
                },
                run_id=run.id,
                expected_revision=int(payload["expected_sections_revision"]),
                status="review",
                invalidate_stages=(
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "section_count": len(index_sections),
            "section_ids": list(expected_tasks),
            "section_index_artifact_id": published[SECTION_INDEX_LOGICAL_NAME].id,
            "revision": state.revision,
            "attempts": max(1, int(attempts)),
        }

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        blueprint, blueprint_artifact = self._read_json_artifact(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        matrix, _matrix_artifact = self._read_json_artifact(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        tasks = self.tasks_from_blueprint(blueprint)
        assigned = list(
            dict.fromkeys(
                paper_id for task in tasks for paper_id in task["allowed_papers"]
            )
        )
        catalog = self._catalog(principal, assigned)
        papers = [
            {
                "paper_id": paper_id,
                "title": catalog[paper_id].title,
                "authors": list(catalog[paper_id].authors_json or []),
                "keywords": list(catalog[paper_id].keywords_json or []),
            }
            for paper_id in assigned
            if paper_id in catalog
        ]
        index, index_artifact = self._read_json_artifact(
            principal, project_id, SECTION_INDEX_LOGICAL_NAME, required=False
        )
        current = bool(
            index
            and index.get("source_blueprint_artifact_id") == blueprint_artifact.id
        )
        section_files: list[dict[str, Any]] = []
        if current:
            for section in index.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                logical = str(section.get("logical_name") or "")
                artifact = self.repository.get_current_artifact(
                    principal.user_id, project_id, logical
                )
                if artifact is None:
                    current = False
                    section_files = []
                    break
                resolved = self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact.id
                )
                section_files.append(
                    {
                        "section_id": str(section.get("section_id") or ""),
                        "name": f"{section.get('section_id')}.md",
                        "logical_name": logical,
                        "artifact_id": artifact.id,
                        "content": resolved.path.read_text(encoding="utf-8"),
                    }
                )
        jobs = self.repository.list_project_jobs(
            principal.user_id, project_id, job_type="sections.generate"
        )
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "sections"
        )
        return {
            "project_id": project_id,
            "section_blueprint": blueprint,
            "blueprint_artifact_id": blueprint_artifact.id,
            "section_tasks": tasks,
            "papers": papers,
            "section_drafts": index if current else None,
            "section_drafts_md": str((index or {}).get("section_drafts_md") or "")
            if current
            else "",
            "section_files": section_files,
            "section_drafting_report_md": str((index or {}).get("report_md") or "")
            if current
            else "",
            "revision": state.revision if state else 0,
            "handoff": {
                "drafts_stale": bool(index_artifact and not current),
                "has_existing_drafts": bool(index_artifact),
                "current": current,
            },
            "report": {
                "current_task_count": len(tasks),
                "current_output_count": len(section_files),
                "jobs": [_job_payload(job) for job in jobs],
            },
            "workspace": {
                "active_stage": "sections",
                "tabs": [
                    {
                        "id": "section",
                        "labels": {"en": "Section Draft", "zh": "章节草稿"},
                    },
                    {
                        "id": "tasks",
                        "labels": {"en": "Writing Requirements", "zh": "写作要求"},
                    },
                    {
                        "id": "report",
                        "labels": {"en": "Generation Report", "zh": "生成报告"},
                    },
                ],
            },
        }

    def confirm(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        payload = self.get(principal, project_id)
        if not payload["handoff"]["current"]:
            raise SectionOutputsMissing(
                "Generate every section from the current Blueprint before entering Image Processing."
            )
        if len(payload["section_files"]) != len(payload["section_tasks"]):
            raise SectionOutputsMissing(
                "One or more current Blueprint sections have no current draft output."
            )
        state = self.repository.compare_and_set_stage(
            principal.user_id,
            project_id,
            "sections",
            int(revision),
            status="approved",
        )
        return {
            "project_id": project_id,
            "revision": state.revision,
            "status": state.status,
            "next_stage": "images",
        }
