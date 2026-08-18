"""PostgreSQL-native section tasks, generation, reports, and handoff."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import UnidentifiedImageError
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
from review_writer_api.figure_rules import image_size
from review_writer_api.mineru_artifacts import mineru_storage_paths
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord, JobRecord, WorkflowRepository
from review_writer_core.review_structure import (
    assign_primary_paper_sections,
    infer_section_role,
)


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
        policy = blueprint.get("paper_assignment_policy")
        policy_mode = (
            str(policy.get("mode") or "") if isinstance(policy, dict) else ""
        )
        normalized_sections = sections
        if policy_mode != "single_primary_section_with_supporting_cross_references":
            legacy_input: list[dict[str, Any]] = []
            legacy_order: list[str] = []
            for index, section in enumerate(sections, start=1):
                if not isinstance(section, dict):
                    continue
                assigned = [
                    str(paper_id)
                    for paper_id in section.get("major_papers") or []
                    if str(paper_id or "").strip()
                ]
                legacy_order.extend(assigned)
                explicit_role = str(section.get("section_role") or "")
                # Older generated Blueprints labelled every section as body,
                # including headings named Introduction and Conclusion.
                if explicit_role == "body":
                    explicit_role = ""
                legacy_input.append(
                    {
                        **section,
                        "section_id": str(
                            section.get("section_id") or f"S{index:02d}"
                        ),
                        "section_role": infer_section_role(
                            section.get("title"), explicit_role
                        ),
                        "paper_ids": assigned,
                    }
                )
            normalized_sections, _owners = assign_primary_paper_sections(
                legacy_input, legacy_order
            )

        tasks: list[dict[str, Any]] = []
        for section in normalized_sections:
            if not isinstance(section, dict) or not str(section.get("section_id") or ""):
                continue
            claims = section.get("review_claims") or []
            if policy_mode == "single_primary_section_with_supporting_cross_references":
                primary_source = [
                    *(section.get("primary_papers") or []),
                    *(section.get("major_papers") or []),
                ]
            else:
                primary_source = (
                    section.get("primary_papers")
                    if "primary_papers" in section
                    else section.get("major_papers")
                )
            primary_papers = list(
                dict.fromkeys(
                    str(paper_id)
                    for paper_id in primary_source or []
                    if str(paper_id or "").strip()
                )
            )
            supporting_papers = list(
                dict.fromkeys(
                    str(paper_id)
                    for paper_id in section.get("supporting_papers") or []
                    if str(paper_id or "").strip()
                    and str(paper_id) not in primary_papers
                )
            )
            tasks.append(
                {
                    "section_id": str(section["section_id"]),
                    "heading": str(section.get("title") or section["section_id"]),
                    "section_role": str(section.get("section_role") or "body"),
                    "core_argument": str(
                        section.get("section_thesis")
                        or section.get("review_problem")
                        or ""
                    ),
                    "primary_papers": primary_papers,
                    "supporting_papers": supporting_papers,
                    "allowed_papers": [*primary_papers, *supporting_papers],
                    "writing_mode": (
                        "framing_synthesis"
                        if str(section.get("section_role") or "body")
                        == "introduction"
                        else "cross_section_synthesis"
                        if str(section.get("section_role") or "body")
                        == "conclusion"
                        else "primary_evidence_synthesis"
                        if primary_papers
                        else "cross_section_synthesis"
                    ),
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
            "library_metadata": {
                paper_id: dict(catalog[paper_id].metadata_json or {})
                for paper_id in assigned
            },
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
        current_outline = self.repository.get_current_artifact(
            principal.user_id, project_id, OUTLINE_LOGICAL_NAME
        )
        if (
            current_blueprint is None
            or current_matrix is None
            or current_outline is None
            or current_blueprint.id != payload["source_blueprint_artifact_id"]
            or current_matrix.id != payload["source_matrix_artifact_id"]
            or current_outline.id != payload["source_outline_artifact_id"]
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
            "source_outline_artifact_id": payload["source_outline_artifact_id"],
            "generated_at": utc_now().isoformat(),
            "sections": index_sections,
            "section_drafts_md": merged + "\n",
            "report_md": report_md + "\n" if report_md else "",
        }
        files[SECTION_INDEX_LOGICAL_NAME] = (
            (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "json",
        )
        paper_candidates = deepcopy(built.get("paper_figure_candidates")) if isinstance(built, dict) else None
        figure_candidates = deepcopy(built.get("figure_candidates")) if isinstance(built, dict) else None
        default_reviews = deepcopy(built.get("default_figure_reviews")) if isinstance(built, dict) else None
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
                    "outline_artifact_id": payload["source_outline_artifact_id"],
                    "section_ids": list(expected_tasks),
                },
            )
            staging = self.artifacts.stage_run_directory(
                principal.user_id, project_id, run.id
            )
            published: dict[str, ArtifactRecord] = {}
            source_artifacts: dict[str, ArtifactRecord] = {}
            if isinstance(paper_candidates, dict) and isinstance(figure_candidates, list):
                valid_anchors = {
                    str(paragraph.get("paragraph_id") or "")
                    for section in index_sections
                    for paragraph in section.get("paragraphs") or []
                    if isinstance(paragraph, dict) and paragraph.get("paragraph_id")
                }
                user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
                figure_counts: dict[str, int] = {}
                mineru_ids: dict[str, uuid.UUID] = {}
                for paper_id, metadata in (payload.get("library_metadata") or {}).items():
                    raw_id = str(
                        ((metadata or {}).get("_artifact_ids") or {}).get("mineru")
                        or ""
                    ).strip()
                    if not raw_id:
                        continue
                    try:
                        mineru_ids[str(paper_id)] = uuid.UUID(raw_id)
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A paper has an invalid registered MinerU artifact."
                        ) from exc
                registered_mineru: dict[uuid.UUID, LibraryArtifact] = {}
                if mineru_ids:
                    with database_session(self.repository.session_factory) as session:
                        rows = session.scalars(
                            select(LibraryArtifact).where(
                                LibraryArtifact.id.in_(tuple(mineru_ids.values())),
                                LibraryArtifact.user_id == uuid.UUID(principal.user_id),
                                LibraryArtifact.kind == "mineru",
                                LibraryArtifact.availability == "available",
                            )
                        ).all()
                        registered_mineru = {row.id: row for row in rows}

                def trusted_extracted_root(paper_id: str) -> Path | None:
                    artifact_id = mineru_ids.get(paper_id)
                    artifact = registered_mineru.get(artifact_id) if artifact_id else None
                    if artifact is None or artifact.paper_id != paper_id:
                        return None
                    try:
                        lexical_content, version_root, lexical_root = (
                            mineru_storage_paths(
                                user_root, paper_id, artifact.relative_path
                            )
                        )
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A paper's registered MinerU artifact path is not trusted."
                        ) from exc
                    try:
                        lexical_content.relative_to(lexical_root)
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A paper's registered MinerU artifact is outside its immutable version."
                        ) from exc
                    current = user_root
                    for part in lexical_content.relative_to(user_root).parts:
                        current = current / part
                        if current.is_symlink() or (
                            hasattr(current, "is_junction") and current.is_junction()
                        ):
                            raise WorkflowValidationError(
                                "A paper's MinerU extraction directory is not trusted."
                            )
                    resolved_root = lexical_root.resolve()
                    try:
                        resolved_root.relative_to(user_root)
                        lexical_content.resolve().relative_to(resolved_root)
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A paper's registered MinerU artifact escaped its immutable version."
                        ) from exc
                    return (
                        resolved_root
                        if resolved_root.is_dir() and lexical_content.is_file()
                        else None
                    )

                def prepare_candidate(candidate: dict[str, Any]) -> None:
                    paper_id = str(candidate.get("paper_id") or "")
                    raw_path = str(candidate.get("source_image_path") or "").strip()
                    for path_field in (
                        "source_image_path",
                        "source_pdf",
                        "source_content_list",
                        "image_path",
                        "path",
                    ):
                        candidate.pop(path_field, None)
                    if not re.fullmatch(r"P[0-9]+", paper_id):
                        return
                    anchor = str(
                        candidate.get("target_paragraph_id")
                        or candidate.get("paragraph_id")
                        or ""
                    )
                    if anchor and anchor not in valid_anchors:
                        raise WorkflowValidationError(
                            "A generated source figure references an unknown manuscript paragraph.",
                            details={"paper_id": paper_id, "paragraph_id": anchor},
                        )
                    if not raw_path:
                        return
                    allowed_root = trusted_extracted_root(paper_id)
                    if allowed_root is None:
                        raise WorkflowValidationError(
                            "A generated source figure has no current MinerU extraction root."
                        )
                    raw_source = Path(raw_path)
                    lexical_source = (
                        raw_source if raw_source.is_absolute() else user_root / raw_source
                    )
                    try:
                        lexical_relative = lexical_source.relative_to(user_root)
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A generated source figure escaped the user workspace."
                        ) from exc
                    if any(part in {"", ".", ".."} for part in lexical_relative.parts):
                        raise WorkflowValidationError(
                            "A generated source figure path is not trusted."
                        )
                    current = user_root
                    for part in lexical_relative.parts:
                        current = current / part
                        if current.is_symlink() or (
                            hasattr(current, "is_junction") and current.is_junction()
                        ):
                            raise WorkflowValidationError(
                                "A generated source figure path is not trusted."
                            )
                    source = lexical_source.resolve()
                    try:
                        source.relative_to(user_root)
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A generated source figure escaped the user workspace."
                        ) from exc
                    try:
                        relative_source = source.relative_to(allowed_root)
                    except ValueError as exc:
                        raise WorkflowValidationError(
                            "A generated source figure does not belong to its paper's MinerU extraction."
                        ) from exc
                    current = allowed_root
                    for part in relative_source.parts:
                        current = current / part
                        if current.is_symlink() or (
                            hasattr(current, "is_junction") and current.is_junction()
                        ):
                            raise WorkflowValidationError(
                                "A generated source figure path is not trusted."
                            )
                    if not source.is_file():
                        return
                    try:
                        image_size(source)
                    except (OSError, UnidentifiedImageError):
                        return
                    source_key = str(source)
                    artifact = source_artifacts.get(source_key)
                    if artifact is None:
                        candidate_index = candidate.get("candidate_index")
                        safe_index = (
                            int(candidate_index)
                            if isinstance(candidate_index, int)
                            and not isinstance(candidate_index, bool)
                            else len(source_artifacts)
                        )
                        suffix = source.suffix.casefold() or ".png"
                        staged_name = f"source-{len(source_artifacts):04d}{suffix}"
                        shutil.copy2(source, staging / staged_name)
                        artifact = self.artifacts.publish(
                            principal.user_id,
                            project_id,
                            run.id,
                            staged_name,
                            logical_name=f"sections/source-images/{paper_id}/{safe_index}{suffix}",
                            artifact_type=suffix.lstrip("."),
                            producer_stage="sections",
                            make_current=False,
                            metadata={"paper_id": paper_id, "candidate_index": safe_index},
                        )
                        source_artifacts[source_key] = artifact
                    candidate["source_image_artifact_id"] = artifact.id

                for paper in paper_candidates.get("papers") or []:
                    if not isinstance(paper, dict):
                        continue
                    paper_id = str(paper.get("paper_id") or "")
                    for candidate in paper.get("candidates") or []:
                        if not isinstance(candidate, dict):
                            continue
                        figure_counts[paper_id] = figure_counts.get(paper_id, 0) + 1
                        candidate.setdefault(
                            "figure_id",
                            f"{paper_id}-F{figure_counts[paper_id]:02d}",
                        )
                        prepare_candidate(candidate)
                lookup = {
                    (
                        str(candidate.get("paper_id") or ""),
                        candidate.get("candidate_index"),
                    ): candidate
                    for paper in paper_candidates.get("papers") or []
                    if isinstance(paper, dict)
                    for candidate in paper.get("candidates") or []
                    if isinstance(candidate, dict)
                }
                lookup_by_label = {
                    (
                        str(candidate.get("paper_id") or ""),
                        str(candidate.get("source_label") or ""),
                    ): candidate
                    for paper in paper_candidates.get("papers") or []
                    if isinstance(paper, dict)
                    for candidate in paper.get("candidates") or []
                    if isinstance(candidate, dict)
                }
                for index, candidate in enumerate(figure_candidates):
                    if not isinstance(candidate, dict):
                        continue
                    matching = lookup.get(
                        (
                            str(candidate.get("paper_id") or ""),
                            candidate.get("candidate_index"),
                        )
                    )
                    if matching is None:
                        matching = lookup_by_label.get(
                            (
                                str(candidate.get("paper_id") or ""),
                                str(candidate.get("source_label") or ""),
                            )
                        )
                    if matching:
                        candidate.update(
                            {
                                key: value
                                for key, value in matching.items()
                                if key in {
                                    "figure_id",
                                    "source_image_artifact_id",
                                    "target_paragraph_id",
                                    "paragraph_id",
                                }
                            }
                        )
                    candidate.setdefault(
                        "figure_id", f"FIG-{index + 1:03d}"
                    )
                    prepare_candidate(candidate)
                reviews_payload = (
                    default_reviews
                    if isinstance(default_reviews, dict)
                    else {"papers": {}}
                )
                review_rows = reviews_payload.get("papers")
                if isinstance(review_rows, dict):
                    for paper_id, review in review_rows.items():
                        if not isinstance(review, dict):
                            continue
                        review.pop("selected_source_image_path", None)
                        selected_candidate = lookup.get(
                            (str(paper_id), review.get("selected_candidate_index"))
                        )
                        if selected_candidate and selected_candidate.get(
                            "source_image_artifact_id"
                        ):
                            review["selected_source_artifact_id"] = selected_candidate[
                                "source_image_artifact_id"
                            ]
                files["sections/paper_figure_candidates.json"] = (
                    (json.dumps(paper_candidates, ensure_ascii=False, indent=2) + "\n").encode(),
                    "json",
                )
                files["sections/figure_candidates.json"] = (
                    (json.dumps(figure_candidates, ensure_ascii=False, indent=2) + "\n").encode(),
                    "json",
                )
                files["sections/default_figure_reviews.json"] = (
                    (
                        json.dumps(
                            reviews_payload,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode(),
                    "json",
                )
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
                    artifact.logical_name: artifact.id
                    for artifact in [*source_artifacts.values(), *published.values()]
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
        matrix, matrix_artifact = self._read_json_artifact(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        _outline, outline_artifact = self._read_json_artifact(
            principal, project_id, OUTLINE_LOGICAL_NAME
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
        matrix_rows = matrix.get("rows") if isinstance(matrix, dict) else []
        matrix_order = [
            str(row.get("paper_id"))
            for row in matrix_rows
            if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
        ]
        label_width = max(3, len(str(len(matrix_order))))
        paper_display_labels = {
            paper_id: f"P{index:0{label_width}d}"
            for index, paper_id in enumerate(dict.fromkeys(matrix_order), start=1)
        }
        index, index_artifact = self._read_json_artifact(
            principal, project_id, SECTION_INDEX_LOGICAL_NAME, required=False
        )
        current = bool(
            index
            and index.get("source_blueprint_artifact_id") == blueprint_artifact.id
            and index.get("source_matrix_artifact_id") == matrix_artifact.id
            and (
                index.get("source_outline_artifact_id")
                or blueprint.get("source_outline_artifact_id")
            )
            == outline_artifact.id
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
            "paper_display_labels": paper_display_labels,
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
