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
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_core.academic_contracts import (
    ACADEMIC_SCHEMA_VERSION,
    evidence_key as academic_evidence_key,
    evidence_level,
)


SECTION_INDEX_LOGICAL_NAME = "sections/section_drafts.json"
EVIDENCE_PACKAGE_LOGICAL_NAME = "sections/evidence_package.json"
SYNTHESIS_STATE_LOGICAL_NAME = "sections/synthesis_state.json"
WRITING_PLAN_LOGICAL_NAME = "sections/writing_plan.json"


class BlueprintPapersMissing(WorkflowConflict):
    code = "BLUEPRINT_PAPERS_MISSING"


class SectionOutputsMissing(WorkflowConflict):
    code = "SECTION_OUTPUTS_NOT_CURRENT"


class SectionProviderUnavailable(WorkflowError):
    code = "SECTION_PROVIDER_UNAVAILABLE"
    status_code = 503
    retryable = True


def _job_payload(job: JobRecord) -> dict[str, Any]:
    actions: list[str] = []
    if job.status in {"queued", "running", "cancel_requested"}:
        actions.append("cancel")
    if job.status in {"failed", "cancelled", "interrupted"}:
        actions.append("retry")
    return {
        "id": job.id,
        "project_id": job.project_id,
        "scope": job.scope,
        "status": job.status,
        "job_type": job.job_type,
        "result": job.result,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "cancellation_requested": job.cancellation_requested,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "retry_of_job_id": job.retry_of_job_id,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "available_actions": actions,
    }


class SectionsService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        library_index: LibraryIndexService | None = None,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.library_index = library_index
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

    @staticmethod
    def _evidence_query(task: dict[str, Any], *, broad: bool = False) -> str:
        parts = [
            str(task.get("heading") or ""),
            str(task.get("core_argument") or ""),
        ]
        if not broad:
            parts.extend(str(item) for item in task.get("must_cover_points") or [])
        return " ".join(part.strip() for part in parts if part.strip())[:4000]

    def _evidence_package(
        self,
        principal: Principal,
        project_id: str,
        tasks: list[dict[str, Any]],
        catalog: dict[str, LibraryPaper],
    ) -> dict[str, Any]:
        sections: list[dict[str, Any]] = []
        for task in tasks:
            allowed = [str(item) for item in task.get("allowed_papers") or []]
            primary = list(
                dict.fromkeys(
                    str(item)
                    for item in task.get("primary_papers") or []
                    if str(item).strip() and str(item) in allowed
                )
            )
            query = self._evidence_query(task)
            hits = []
            if (
                self.library_index is not None
                and self.library_index.enabled
                and query
                and allowed
            ):
                hits = self.library_index.retrieve(
                    principal,
                    query,
                    allowed_papers=allowed,
                    top_k=self.library_index.tuning.subsection_top_k,
                    include_neighbors=True,
                )
                if len([hit for hit in hits if not hit.is_neighbor]) < 2:
                    broad_query = self._evidence_query(task, broad=True)
                    if broad_query and broad_query != query:
                        broadened = self.library_index.retrieve(
                            principal,
                            broad_query,
                            allowed_papers=allowed,
                            top_k=self.library_index.tuning.subsection_top_k,
                            include_neighbors=True,
                        )
                        by_chunk = {hit.chunk_id: hit for hit in hits}
                        for hit in broadened:
                            by_chunk.setdefault(hit.chunk_id, hit)
                        hits = list(by_chunk.values())
                covered_primary = {
                    hit.paper_id for hit in hits if not hit.is_neighbor
                }
                missing_primary = [
                    paper_id
                    for paper_id in primary
                    if paper_id not in covered_primary
                ]
                if missing_primary:
                    coverage_hits = self.library_index.primary_coverage_hits(
                        principal,
                        allowed_papers=missing_primary,
                        per_paper_limit=1,
                    )
                    by_chunk = {hit.chunk_id: hit for hit in hits}
                    for hit in coverage_hits:
                        by_chunk.setdefault(hit.chunk_id, hit)
                    hits = list(by_chunk.values())
            hit_rows = []
            for hit in hits:
                stable_key = academic_evidence_key(
                    hit.paper_id, hit.chunk_id, hit.source_lineage_hash
                )
                hit_rows.append({
                    "evidence_id": f"EV-{stable_key.removeprefix('sha256:')[:12].upper()}",
                    "evidence_key": stable_key,
                    "paper_id": hit.paper_id,
                    "paper_title": catalog[hit.paper_id].title
                    if hit.paper_id in catalog
                    else hit.paper_id,
                    "chunk_id": hit.chunk_id,
                    "content": hit.content,
                    "page_start": hit.page_start,
                    "page_end": hit.page_end,
                    "section_path": list(hit.section_path),
                    "content_type": hit.content_type,
                    "asset_refs": list(hit.asset_refs),
                    "score": hit.score,
                    "match_reason": hit.match_reason,
                    "is_neighbor": hit.is_neighbor,
                    "index_id": hit.index_id,
                    "source_lineage_hash": hit.source_lineage_hash,
                    "previous_chunk_id": hit.previous_chunk_id,
                    "next_chunk_id": hit.next_chunk_id,
                    "evidence_level": evidence_level(hit.content),
                })
            evidence_papers = sorted({row["paper_id"] for row in hit_rows})
            missing_primary = [
                paper_id for paper_id in primary if paper_id not in evidence_papers
            ]
            sections.append(
                {
                    "section_id": str(task.get("section_id") or ""),
                    "heading": str(task.get("heading") or ""),
                    "query": query,
                    "allowed_papers": allowed,
                    "retrieval_mode": (
                        "lexical" if hit_rows else "fixed_prefix_fallback"
                    ),
                    "status": (
                        "ready"
                        if hit_rows and not missing_primary
                        else "insufficient_evidence"
                    ),
                    "hit_count": len(hit_rows),
                    "paper_count": len(evidence_papers),
                    "primary_paper_count": len(primary),
                    "covered_primary_paper_count": len(primary)
                    - len(missing_primary),
                    "missing_primary_papers": missing_primary,
                    "hits": hit_rows,
                }
            )
        registry: dict[str, dict[str, Any]] = {}
        for section in sections:
            for hit in section.get("hits") or []:
                if isinstance(hit, dict) and str(hit.get("evidence_key") or ""):
                    registry.setdefault(str(hit["evidence_key"]), dict(hit))
        return {
            "schema_version": ACADEMIC_SCHEMA_VERSION + 1,
            "project_id": project_id,
            "retrieval_engine": "normalized_exact_phrase+postgresql_fulltext",
            "evidence_registry": list(registry.values()),
            "sections": sections,
        }

    @staticmethod
    def _dirty_section_ids(
        tasks: list[dict[str, Any]],
        current_snapshot: dict[str, Any],
        previous_snapshot: Any,
    ) -> list[str]:
        section_ids = [str(task.get("section_id") or "") for task in tasks]
        if not isinstance(previous_snapshot, dict) or not previous_snapshot:
            return section_ids
        for key in (
            "blueprint_artifact_id",
            "matrix_artifact_id",
            "outline_artifact_id",
        ):
            if str(previous_snapshot.get(key) or "") != str(current_snapshot.get(key) or ""):
                return section_ids
        previous_ids = {str(item) for item in previous_snapshot.get("section_ids") or []}
        current_ids = set(section_ids)
        dirty = current_ids.symmetric_difference(previous_ids)
        previous_keys = previous_snapshot.get("evidence_keys_by_section") or {}
        current_keys = current_snapshot.get("evidence_keys_by_section") or {}
        for section_id in current_ids & previous_ids:
            if set(previous_keys.get(section_id) or []) != set(
                current_keys.get(section_id) or []
            ):
                dirty.add(section_id)
        return [section_id for section_id in section_ids if section_id in dirty]

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
        evidence_package = self._evidence_package(
            principal, project_id, tasks, catalog
        )
        evidence_by_section = {
            str(item.get("section_id") or ""): item
            for item in evidence_package["sections"]
        }
        tasks = [
            {
                **task,
                "evidence_status": {
                    key: evidence_by_section.get(task["section_id"], {}).get(key)
                    for key in (
                        "retrieval_mode",
                        "status",
                        "hit_count",
                        "paper_count",
                    )
                },
            }
            for task in tasks
        ]
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "sections"
        )
        input_snapshot = {
            "blueprint_artifact_id": blueprint_artifact.id,
            "matrix_artifact_id": matrix_artifact.id,
            "outline_artifact_id": outline_artifact.id,
            "section_ids": [str(task.get("section_id") or "") for task in tasks],
            "evidence_keys_by_section": {
                str(section.get("section_id") or ""): sorted(
                    str(hit.get("evidence_key") or "")
                    for hit in section.get("hits") or []
                    if isinstance(hit, dict) and hit.get("evidence_key")
                )
                for section in evidence_package.get("sections") or []
                if isinstance(section, dict)
            },
        }
        previous_run = self.repository.get_latest_stage_run(
            principal.user_id, project_id, "sections"
        )
        dirty_object_ids = self._dirty_section_ids(
            tasks,
            input_snapshot,
            previous_run.input_snapshot if previous_run is not None else None,
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
            "run_input_snapshot": {
                **input_snapshot,
                "dirty_object_ids": dirty_object_ids,
            },
            "evidence_package": {
                **evidence_package,
                "source_blueprint_artifact_id": blueprint_artifact.id,
                "source_matrix_artifact_id": matrix_artifact.id,
                "source_outline_artifact_id": outline_artifact.id,
            },
            "library_metadata": {
                paper_id: dict(catalog[paper_id].metadata_json or {})
                for paper_id in assigned
            },
        }

    @staticmethod
    def _fallback_synthesis_state(
        payload: dict[str, Any],
        built: dict[str, Any],
        evidence_package: dict[str, Any],
    ) -> dict[str, Any]:
        """Adapt legacy section writers without inventing synthesis content."""

        evidence_by_section = {
            str(item.get("section_id") or ""): item
            for item in evidence_package.get("sections") or []
            if isinstance(item, dict)
        }
        built_by_section = {
            str(item.get("section_id") or ""): item
            for item in built.get("sections") or []
            if isinstance(item, dict)
        }
        blueprint_by_section = {
            str(item.get("section_id") or ""): item
            for item in (payload.get("blueprint") or {}).get("sections") or []
            if isinstance(item, dict)
        }
        sections = []
        for task in payload.get("tasks") or []:
            section_id = str(task.get("section_id") or "")
            evidence = evidence_by_section.get(section_id, {})
            evidence_keys = list(
                dict.fromkeys(
                    str(hit.get("evidence_key") or "")
                    for hit in evidence.get("hits") or []
                    if isinstance(hit, dict) and hit.get("evidence_key")
                )
            )
            blueprint_section = blueprint_by_section.get(section_id, {})
            generated_section = built_by_section.get(section_id, {})
            components = []
            for index, requirement in enumerate(
                blueprint_section.get("synthesis_requirements") or [], start=1
            ):
                if not isinstance(requirement, dict):
                    continue
                component_type = str(requirement.get("component") or "").strip()
                if not component_type:
                    continue
                components.append(
                    {
                        "component_id": f"{section_id}-{component_type}-{index:02d}",
                        "component_type": component_type,
                        "necessity": str(requirement.get("necessity") or "recommended"),
                        "purpose": str(requirement.get("reason") or ""),
                        "status": "evidence_ready" if evidence_keys else "insufficient_evidence",
                        "evidence_keys": evidence_keys,
                        "summary": str(generated_section.get("overview") or ""),
                        "provenance": "legacy_adapter",
                    }
                )
            sections.append(
                {
                    "section_id": section_id,
                    "components": components,
                }
            )
        return {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "project_id": payload.get("project_id"),
            "planning_mode": "legacy_adapter",
            "source_blueprint_artifact_id": payload.get("source_blueprint_artifact_id"),
            "source_evidence_registry": EVIDENCE_PACKAGE_LOGICAL_NAME,
            "sections": sections,
        }

    @staticmethod
    def _fallback_writing_plan(
        payload: dict[str, Any],
        built: dict[str, Any],
        evidence_package: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive a compatibility plan for legacy/test writers.

        The current scientific writer emits an evidence-first plan directly.
        This adapter only keeps old artifacts readable and is explicitly
        labelled so it cannot be mistaken for a pre-draft planning result.
        """

        registry = {
            (str(item.get("paper_id") or ""), str(item.get("chunk_id") or "")): item
            for item in evidence_package.get("evidence_registry") or []
            if isinstance(item, dict)
        }
        built_by_section = {
            str(item.get("section_id") or ""): item
            for item in built.get("sections") or []
            if isinstance(item, dict)
        }
        output_sections = []
        for task in payload.get("tasks") or []:
            section_id = str(task.get("section_id") or "")
            generated = built_by_section.get(section_id, {})
            paragraph_plans: list[dict[str, Any]] = []
            claim_plans: list[dict[str, Any]] = []
            for paragraph_index, paragraph in enumerate(
                generated.get("paragraphs") or [], start=1
            ):
                if not isinstance(paragraph, dict):
                    continue
                paragraph_id = str(
                    paragraph.get("paragraph_id")
                    or f"{section_id}-p{paragraph_index}"
                )
                paragraph_claim_ids: list[str] = []
                evidence_claims = [
                    item
                    for item in paragraph.get("evidence") or []
                    if isinstance(item, dict)
                ]
                for claim_index, evidence in enumerate(evidence_claims, start=1):
                    claim_id = f"{paragraph_id}-C{claim_index:02d}"
                    paper_id = str(evidence.get("paper_id") or "")
                    refs = []
                    levels = []
                    for chunk_id in evidence.get("chunk_ids") or []:
                        hit = registry.get((paper_id, str(chunk_id)))
                        if not hit:
                            continue
                        refs.append(
                            {
                                "evidence_id": hit.get("evidence_id"),
                                "evidence_key": hit.get("evidence_key"),
                            }
                        )
                        levels.append(str(hit.get("evidence_level") or "reported_result"))
                    claim_plans.append(
                        {
                            "claim_id": claim_id,
                            "paragraph_id": paragraph_id,
                            "sequence": claim_index,
                            "claim": str(evidence.get("claim") or "").strip(),
                            "claim_kind": "reported_finding",
                            "epistemic_status": "direct_source_report",
                            "support_status": "supported" if refs else "partially_supported",
                            "citation_group": [paper_id] if paper_id else [],
                            "evidence_refs": refs,
                            "evidence_ceiling": (
                                "Do not generalize beyond the selected source passage."
                                if levels
                                else "Legacy source mode: use only bounded attribution."
                            ),
                        }
                    )
                    paragraph_claim_ids.append(claim_id)
                cited = list(
                    dict.fromkeys(
                        str(item)
                        for item in paragraph.get("cited_paper_ids")
                        or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                        if str(item or "").strip()
                    )
                )
                if not paragraph_claim_ids and cited:
                    claim_id = f"{paragraph_id}-C01"
                    claim_plans.append(
                        {
                            "claim_id": claim_id,
                            "paragraph_id": paragraph_id,
                            "sequence": 1,
                            "claim": "Bounded source-attributed statement from the legacy section writer.",
                            "claim_kind": "reported_finding",
                            "epistemic_status": "direct_source_report",
                            "support_status": "partially_supported",
                            "citation_group": cited,
                            "evidence_refs": [],
                            "evidence_ceiling": "Do not make quantitative, causal or mechanistic claims without chunk evidence.",
                        }
                    )
                    paragraph_claim_ids.append(claim_id)
                takeaway = next(
                    (
                        str(item.get("claim") or "").strip()
                        for item in evidence_claims
                        if str(item.get("claim") or "").strip()
                    ),
                    str(task.get("core_argument") or "").strip(),
                )
                paragraph_plans.append(
                    {
                        "paragraph_id": paragraph_id,
                        "theme": takeaway or str(task.get("heading") or section_id),
                        "argument_role": "synthesis" if len(cited) > 1 else "reported_evidence",
                        "objective": takeaway or "Realize the section's evidence-backed argument.",
                        "target_words": {"min": 120, "max": 320},
                        "primary_papers": [item for item in cited if item in task.get("primary_papers", [])],
                        "supporting_papers": [item for item in cited if item in task.get("supporting_papers", [])],
                        "reader_takeaway": takeaway or "A bounded source-attributed finding.",
                        "positive_synthesis": "State the supported finding before its evidence boundary.",
                        "caveat_policy": "diagnostic_only",
                        "claim_ids": paragraph_claim_ids,
                    }
                )
            output_sections.append(
                {
                    "section_id": section_id,
                    "route": "B" if len(paragraph_plans) > 1 else "A",
                    "paragraphs": paragraph_plans,
                    "claims": claim_plans,
                }
            )
        return {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "project_id": payload.get("project_id"),
            "planning_mode": "legacy_derived_after_generation",
            "source_blueprint_artifact_id": payload.get("source_blueprint_artifact_id"),
            "source_evidence_registry": EVIDENCE_PACKAGE_LOGICAL_NAME,
            "sections": output_sections,
        }

    @staticmethod
    def _validate_academic_bundle(
        payload: dict[str, Any],
        built: dict[str, Any],
        synthesis_state: dict[str, Any],
        writing_plan: dict[str, Any],
        evidence_package: dict[str, Any],
    ) -> None:
        expected_sections = {
            str(task.get("section_id") or "") for task in payload.get("tasks") or []
        }
        synthesis_sections = {
            str(item.get("section_id") or "")
            for item in synthesis_state.get("sections") or []
            if isinstance(item, dict)
        }
        writing_sections = {
            str(item.get("section_id") or "")
            for item in writing_plan.get("sections") or []
            if isinstance(item, dict)
        }
        if synthesis_sections != expected_sections or writing_sections != expected_sections:
            raise WorkflowValidationError(
                "Academic bundle does not match the current Blueprint section set.",
                details={
                    "expected": sorted(expected_sections),
                    "synthesis": sorted(synthesis_sections),
                    "writing": sorted(writing_sections),
                },
            )
        evidence_registry = {
            str(item.get("evidence_key") or ""): item
            for item in evidence_package.get("evidence_registry") or []
            if isinstance(item, dict) and item.get("evidence_key")
        }
        evidence_sections = {
            str(item.get("section_id") or ""): item
            for item in evidence_package.get("sections") or []
            if isinstance(item, dict)
        }
        generated_by_section = {
            str(section.get("section_id") or ""): section
            for section in built.get("sections") or []
            if isinstance(section, dict)
        }
        planned_paragraphs: set[str] = set()
        generated_paragraphs: set[str] = set()
        claim_ids: set[str] = set()
        for section in writing_plan.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            section_paragraphs: set[str] = set()
            for paragraph in section.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                paragraph_id = str(paragraph.get("paragraph_id") or "")
                if not paragraph_id or paragraph_id in planned_paragraphs:
                    raise WorkflowValidationError("Writing Plan contains a missing or duplicate paragraph ID.")
                planned_paragraphs.add(paragraph_id)
                section_paragraphs.add(paragraph_id)
            generated_section = generated_by_section.get(section_id, {})
            generated_section_paragraphs = {
                str(paragraph.get("paragraph_id") or "")
                for paragraph in generated_section.get("paragraphs") or []
                if isinstance(paragraph, dict) and paragraph.get("paragraph_id")
            }
            generated_paragraphs.update(generated_section_paragraphs)
            if section_paragraphs != generated_section_paragraphs:
                raise WorkflowValidationError(
                    "A Writing Plan section does not match its generated paragraphs.",
                    details={
                        "section_id": section_id,
                        "planned_only": sorted(section_paragraphs - generated_section_paragraphs),
                        "generated_only": sorted(generated_section_paragraphs - section_paragraphs),
                    },
                )
            section_claim_ids: set[str] = set()
            lexical = str(
                evidence_sections.get(section_id, {}).get("retrieval_mode") or ""
            ) == "lexical"
            for claim in section.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                claim_id = str(claim.get("claim_id") or "")
                if not claim_id or claim_id in claim_ids:
                    raise WorkflowValidationError("Writing Plan contains a missing or duplicate Claim ID.")
                claim_ids.add(claim_id)
                section_claim_ids.add(claim_id)
                if str(claim.get("paragraph_id") or "") not in section_paragraphs:
                    raise WorkflowValidationError("A Claim references an unknown planned paragraph.")
                if str(claim.get("support_status") or "") == "blocked":
                    raise WorkflowValidationError(
                        "A blocked Claim cannot enter a publishable section draft.",
                        details={"claim_id": claim_id},
                    )
                ref_papers: set[str] = set()
                refs = [ref for ref in claim.get("evidence_refs") or [] if isinstance(ref, dict)]
                for ref in claim.get("evidence_refs") or []:
                    key = str(ref.get("evidence_key") or "") if isinstance(ref, dict) else ""
                    if key not in evidence_registry:
                        raise WorkflowValidationError(
                            "A Claim references evidence outside the current Evidence Package.",
                            details={"claim_id": claim_id, "evidence_key": key},
                        )
                    ref_papers.add(str(evidence_registry[key].get("paper_id") or ""))
                if lexical and not refs:
                    raise WorkflowValidationError(
                        "An indexed-evidence Claim must reference at least one current evidence key.",
                        details={"claim_id": claim_id},
                    )
                citation_group = {
                    str(paper_id)
                    for paper_id in claim.get("citation_group") or []
                    if str(paper_id or "")
                }
                if refs and citation_group != ref_papers:
                    raise WorkflowValidationError(
                        "A Claim citation group does not match its evidence sources.",
                        details={
                            "claim_id": claim_id,
                            "citation_group": sorted(citation_group),
                            "evidence_papers": sorted(ref_papers),
                        },
                    )
            if str(writing_plan.get("planning_mode") or "").startswith("evidence_first"):
                realized_claim_ids = {
                    str(realization.get("claim_id") or "")
                    for paragraph in generated_section.get("paragraphs") or []
                    if isinstance(paragraph, dict)
                    for realization in paragraph.get("claim_realizations") or []
                    if isinstance(realization, dict) and realization.get("claim_id")
                }
                if realized_claim_ids != section_claim_ids:
                    raise WorkflowValidationError(
                        "Generated prose does not realize the current Claim Plan exactly.",
                        details={
                            "section_id": section_id,
                            "planned_only": sorted(section_claim_ids - realized_claim_ids),
                            "generated_only": sorted(realized_claim_ids - section_claim_ids),
                        },
                    )
        for section in synthesis_state.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for component in section.get("components") or []:
                if not isinstance(component, dict):
                    continue
                for key in component.get("evidence_keys") or []:
                    if str(key) not in evidence_registry:
                        raise WorkflowValidationError(
                            "A Synthesis component references evidence outside the current Evidence Package.",
                            details={
                                "component_id": component.get("component_id"),
                                "evidence_key": str(key),
                            },
                        )
        if planned_paragraphs != generated_paragraphs:
            raise WorkflowValidationError(
                "Writing Plan paragraph IDs do not match generated section paragraphs.",
                details={
                    "planned_only": sorted(planned_paragraphs - generated_paragraphs),
                    "generated_only": sorted(generated_paragraphs - planned_paragraphs),
                },
            )

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
        evidence_package = payload.get("evidence_package")
        if not isinstance(evidence_package, dict):
            evidence_package = {"schema_version": 1, "sections": []}
        evidence_sections = {
            str(item.get("section_id") or ""): item
            for item in evidence_package.get("sections") or []
            if isinstance(item, dict)
        }
        synthesis_state = deepcopy(built.get("synthesis_state"))
        if not isinstance(synthesis_state, dict):
            synthesis_state = self._fallback_synthesis_state(
                payload, built, evidence_package
            )
        writing_plan = deepcopy(built.get("writing_plan"))
        if not isinstance(writing_plan, dict):
            writing_plan = self._fallback_writing_plan(
                payload, built, evidence_package
            )
        self._validate_academic_bundle(
            payload,
            built,
            synthesis_state,
            writing_plan,
            evidence_package,
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
            package_section = evidence_sections.get(section_id, {})
            package_hits = [
                hit
                for hit in package_section.get("hits") or []
                if isinstance(hit, dict)
            ]
            valid_chunks = {
                (str(hit.get("paper_id") or ""), str(hit.get("chunk_id") or ""))
                for hit in package_hits
                if hit.get("paper_id") and hit.get("chunk_id")
            }
            lexical_contract = (
                str(package_section.get("retrieval_mode") or "") == "lexical"
            )
            cited: set[str] = set()
            for paragraph in section.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                paragraph_evidence = paragraph.get("evidence")
                if lexical_contract and not isinstance(paragraph_evidence, list):
                    raise WorkflowValidationError(
                        "A generated paragraph is missing chunk-level evidence.",
                        details={
                            "section_id": section_id,
                            "paragraph_id": paragraph.get("paragraph_id"),
                        },
                    )
                evidence_papers: list[str] = []
                for evidence in paragraph_evidence or []:
                    if not isinstance(evidence, dict):
                        raise WorkflowValidationError(
                            "A generated paragraph has invalid evidence metadata."
                        )
                    evidence_paper = str(evidence.get("paper_id") or "")
                    chunk_ids = [
                        str(chunk_id)
                        for chunk_id in evidence.get("chunk_ids") or []
                        if str(chunk_id)
                    ]
                    if (
                        evidence_paper not in task["allowed_papers"]
                        or not chunk_ids
                        or any(
                            (evidence_paper, chunk_id) not in valid_chunks
                            for chunk_id in chunk_ids
                        )
                    ):
                        raise WorkflowValidationError(
                            "A generated paragraph cites evidence outside its allowed retrieval package.",
                            details={
                                "section_id": section_id,
                                "paragraph_id": paragraph.get("paragraph_id"),
                                "paper_id": evidence_paper,
                                "chunk_ids": chunk_ids,
                            },
                        )
                    if not str(evidence.get("claim") or "").strip():
                        raise WorkflowValidationError(
                            "A generated paragraph evidence item is missing its supported claim."
                        )
                    evidence_papers.append(evidence_paper)
                declared = [
                    str(paper_id)
                    for paper_id in (
                        paragraph.get("cited_paper_ids")
                        or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                    )
                    if str(paper_id)
                ]
                paragraph_papers = list(dict.fromkeys([*evidence_papers, *declared]))
                cited.update(paragraph_papers)
                if evidence_papers:
                    paragraph["cited_paper_ids"] = list(
                        dict.fromkeys(evidence_papers)
                    )
                    paragraph["paper_id"] = evidence_papers[0]
            unknown = sorted(cited - set(task["allowed_papers"]))
            if unknown:
                raise WorkflowValidationError(
                    "A generated section cited papers outside its Blueprint task.",
                    details={"section_id": section_id, "paper_ids": unknown},
                )
            missing_primary = sorted(set(task.get("primary_papers") or []) - cited)
            if missing_primary:
                raise WorkflowValidationError(
                    "A generated section does not cover every primary paper with validated evidence.",
                    details={
                        "section_id": section_id,
                        "paper_ids": missing_primary,
                    },
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
            "evidence_package_logical_name": EVIDENCE_PACKAGE_LOGICAL_NAME,
            "synthesis_state_logical_name": SYNTHESIS_STATE_LOGICAL_NAME,
            "writing_plan_logical_name": WRITING_PLAN_LOGICAL_NAME,
        }
        stored_evidence_package = {
            **evidence_package,
            "source_blueprint_artifact_id": payload["source_blueprint_artifact_id"],
            "source_matrix_artifact_id": payload["source_matrix_artifact_id"],
            "source_outline_artifact_id": payload["source_outline_artifact_id"],
            "published_at": utc_now().isoformat(),
        }
        files[EVIDENCE_PACKAGE_LOGICAL_NAME] = (
            (
                json.dumps(stored_evidence_package, ensure_ascii=False, indent=2)
                + "\n"
            ).encode("utf-8"),
            "json",
        )
        files[SYNTHESIS_STATE_LOGICAL_NAME] = (
            (json.dumps(synthesis_state, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
            "json",
        )
        files[WRITING_PLAN_LOGICAL_NAME] = (
            (json.dumps(writing_plan, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
            "json",
        )
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
                input_snapshot=dict(
                    payload.get("run_input_snapshot")
                    or {
                        "blueprint_artifact_id": payload["source_blueprint_artifact_id"],
                        "matrix_artifact_id": payload["source_matrix_artifact_id"],
                        "outline_artifact_id": payload["source_outline_artifact_id"],
                        "section_ids": list(expected_tasks),
                        "dirty_object_ids": list(expected_tasks),
                    }
                ),
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
                run_output_snapshot={
                    "artifact_ids": {
                        artifact.logical_name: artifact.id
                        for artifact in [*source_artifacts.values(), *published.values()]
                    },
                    "recomputed_object_ids": list(expected_tasks),
                    "academic_bundle": [
                        SYNTHESIS_STATE_LOGICAL_NAME,
                        WRITING_PLAN_LOGICAL_NAME,
                        EVIDENCE_PACKAGE_LOGICAL_NAME,
                        SECTION_INDEX_LOGICAL_NAME,
                    ],
                },
            )
        return {
            "section_count": len(index_sections),
            "section_ids": list(expected_tasks),
            "section_index_artifact_id": published[SECTION_INDEX_LOGICAL_NAME].id,
            "evidence_package_artifact_id": published[
                EVIDENCE_PACKAGE_LOGICAL_NAME
            ].id,
            "synthesis_state_artifact_id": published[
                SYNTHESIS_STATE_LOGICAL_NAME
            ].id,
            "writing_plan_artifact_id": published[WRITING_PLAN_LOGICAL_NAME].id,
            "revision": state.revision,
            "attempts": max(1, int(attempts)),
            "dirty_object_ids": list(
                (payload.get("run_input_snapshot") or {}).get("dirty_object_ids")
                or []
            ),
            "recomputed_object_ids": list(expected_tasks),
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
        evidence_package: dict[str, Any] | None = None
        synthesis_state: dict[str, Any] | None = None
        writing_plan: dict[str, Any] | None = None
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
            if current:
                evidence_artifact = self.repository.get_current_artifact(
                    principal.user_id, project_id, EVIDENCE_PACKAGE_LOGICAL_NAME
                )
                if evidence_artifact is not None:
                    resolved_evidence = self.artifacts.resolve_owned_artifact(
                        principal.user_id, evidence_artifact.id
                    )
                    try:
                        parsed_evidence = json.loads(
                            resolved_evidence.path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        parsed_evidence = None
                    if isinstance(parsed_evidence, dict):
                        evidence_package = parsed_evidence
                synthesis_state, synthesis_artifact = self._read_json_artifact(
                    principal,
                    project_id,
                    SYNTHESIS_STATE_LOGICAL_NAME,
                    required=False,
                )
                writing_plan, writing_artifact = self._read_json_artifact(
                    principal,
                    project_id,
                    WRITING_PLAN_LOGICAL_NAME,
                    required=False,
                )
                if (
                    index.get("synthesis_state_logical_name")
                    and synthesis_artifact is None
                ) or (
                    index.get("writing_plan_logical_name")
                    and writing_artifact is None
                ):
                    current = False
                    synthesis_state = None
                    writing_plan = None
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
            "evidence_package": evidence_package if current else None,
            "synthesis_state": synthesis_state if current else None,
            "writing_plan": writing_plan if current else None,
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
