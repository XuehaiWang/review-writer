"""PostgreSQL-native Matrix, outline, and Blueprint workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.credentials import (
    ProviderKind,
    ProviderSettingsError,
    ProviderSettingsService,
)
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.scientific_runner import (
    SENSITIVE_ENVIRONMENT_KEY,
    ScientificRunner,
)
from review_writer_api.workflow_models import LibraryPaper, WorkflowJob
from review_writer_api.workflow_repository import ArtifactRecord, JobRecord, WorkflowRepository
from review_writer_core.taxonomy import TaxonomyConfigurationError, load_taxonomy_rules
from review_writer_core.metadata_tags import verified_structured_tags
from review_writer_core.academic_contracts import (
    ACADEMIC_SCHEMA_VERSION,
    classification_basis,
    coverage_diagnostics,
    derive_scope_contract,
    section_academic_contract,
    scope_diagnostics,
    synthesis_requirements,
    taxonomy_diagnostics,
    evidence_key as academic_evidence_key,
)
from review_writer_core.evidence_queries import build_question_query_plans
from review_writer_core.review_structure import (
    assign_primary_paper_sections,
    infer_section_role,
)


MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
OUTLINE_LOGICAL_NAME = "planning/selected_outline.json"
REFERENCE_INDEX_LOGICAL_NAME = "planning/reference_outlines.json"
BLUEPRINT_LOGICAL_NAME = "blueprint/section_blueprint.json"
DISCOVERY_LOGICAL_NAME = "discovery/review.json"
ROUTING_REQUIRED_LABEL = "Routing required — reassign these papers"
CROSS_CATEGORY_BOUNDARY_LABEL = "Cross-category evidence and boundary cases"


def _planning_job_payload(job: JobRecord) -> dict[str, Any]:
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

OUTLINE_STYLES: dict[str, dict[str, str]] = {
    "substrate": {
        "en": "Substrate-classified",
        "zh": "按底物分类",
        "axis": "substrate classes and scope",
        "tag_key": "substrate",
        "introduction": "define the review scope and explain why substrate class is the primary comparison axis",
    },
    "catalyst": {
        "en": "Catalyst and method-classified",
        "zh": "按催化剂与方法分类",
        "axis": "catalysts, methods, and operating principles",
        "tag_key": "catalyst_or_method",
        "introduction": "compare how catalysts or methods shape outcomes, evidence quality, and applicability",
    },
    "reaction": {
        "en": "Reaction-type-classified",
        "zh": "按反应类型分类",
        "axis": "transformation and mechanistic strategy",
        "tag_key": "reaction_type",
        "introduction": "organize the literature by transformation logic and mechanistic strategy",
    },
}


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _paper_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("paper_id")) for row in rows if str(row.get("paper_id") or "").strip()]


def _outline_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in str(markdown or "").replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^##\s+(?:\d+[.)]\s*)?(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip()
            current = {
                "title": title,
                "paper_ids": [],
                "context_paper_ids": [],
                "section_role": infer_section_role(title),
                "purpose": "",
                "notes": "",
            }
            sections.append(current)
            continue
        if current is not None and line.casefold().startswith("section role:"):
            role = line.split(":", 1)[1].strip().casefold()
            if role in {"introduction", "body", "conclusion", "references"}:
                current["section_role"] = role
            continue
        if current is not None and line.casefold().startswith("assigned papers:"):
            assigned = line.split(":", 1)[1].strip().rstrip(".。")
            current["paper_ids"] = list(
                dict.fromkeys(
                    paper_id.strip()
                    for paper_id in re.split(r"[,，;；]", assigned)
                    if paper_id.strip()
                )
            )
            continue
        if current is not None and re.match(
            r"^(?:context|contextual) papers:", line, re.I
        ):
            assigned = line.split(":", 1)[1].strip().rstrip(".。")
            current["context_paper_ids"] = list(
                dict.fromkeys(
                    paper_id.strip()
                    for paper_id in re.split(r"[,，;；]", assigned)
                    if paper_id.strip()
                )
            )
            continue
        if current is not None and line.casefold().startswith("purpose:"):
            current["purpose"] = line.split(":", 1)[1].strip()
            continue
        if current is not None and line.casefold().startswith("notes:"):
            current["notes"] = line.split(":", 1)[1].strip()
    return sections


def _outline_markdown_from_sections(
    sections: list[dict[str, Any]],
    *,
    outline_style: str,
    automatically_adjusted: bool = False,
) -> str:
    """Render parsed sections back to beginner-readable outline Markdown.

    This renderer is used only for an automatically repaired system outline.
    It preserves the section roles, paper assignments, purposes, and notes
    understood by ``_outline_sections`` while removing temporary routing
    placeholders from the Blueprint-facing outline snapshot.
    """

    definition = OUTLINE_STYLES.get(str(outline_style or "").casefold())
    lines = ["# Selected Outline", ""]
    if definition:
        lines.extend([f"Primary structure: {definition['en']}.", ""])
    if automatically_adjusted:
        lines.extend(
            [
                "The system automatically routed previously unclassified papers using the current taxonomy and paper evidence.",
                "",
            ]
        )
    body_number = 0
    for section in sections:
        role = str(section.get("section_role") or "body").casefold()
        title = str(section.get("title") or "").strip()
        if not title:
            continue
        if role == "body":
            body_number += 1
            heading = f"## {body_number}. {title}"
        else:
            heading = f"## {title}"
        lines.extend([heading, f"Section role: {role}"])
        paper_ids = list(dict.fromkeys(section.get("paper_ids") or []))
        if paper_ids:
            lines.append(f"Assigned papers: {', '.join(paper_ids)}.")
        context_ids = list(dict.fromkeys(section.get("context_paper_ids") or []))
        if context_ids:
            lines.append(f"Context papers: {', '.join(context_ids)}.")
        purpose = str(section.get("purpose") or "").strip()
        if purpose:
            lines.append(f"Purpose: {purpose}")
        notes = str(section.get("notes") or "").strip()
        if notes:
            lines.append(f"Notes: {notes}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


class PlanningService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        *,
        scientific_runner: ScientificRunner | None = None,
        provider_settings: ProviderSettingsService | None = None,
        model_gateway: Any | None = None,
        library_index: LibraryIndexService | None = None,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.scientific_runner = scientific_runner
        self.provider_settings = provider_settings
        self.model_gateway = model_gateway
        self.library_index = library_index
        self.root = Path(__file__).resolve().parents[2]
        self._write_lock = threading.RLock()

    def _begin_reference_gateway_job(
        self, principal: Principal, project_id: str, candidate_id: str
    ) -> SimpleNamespace:
        if self.model_gateway is None:
            raise RuntimeError("The internal model gateway is unavailable.")
        job_id = uuid.uuid4()
        now = utc_now()
        with database_session(self.model_gateway.session_factory) as session:
            session.add(
                WorkflowJob(
                    id=job_id,
                    user_id=uuid.UUID(principal.user_id),
                    project_id=uuid.UUID(project_id),
                    scope="project",
                    job_type="planning.reference-analyze",
                    status="running",
                    idempotency_scope_key=f"project:{project_id}",
                    idempotency_key=candidate_id,
                    payload_json={"candidate_id": candidate_id},
                    started_at=now,
                )
            )
        return SimpleNamespace(
            job_id=str(job_id),
            user_id=principal.user_id,
            project_id=project_id,
            job_type="planning.reference-analyze",
        )

    def _finish_reference_gateway_job(
        self, job_id: str, *, succeeded: bool, error_message: str = ""
    ) -> None:
        if self.model_gateway is None:
            return
        with database_session(self.model_gateway.session_factory) as session:
            row = session.get(WorkflowJob, uuid.UUID(job_id))
            if row is None:
                return
            row.status = "succeeded" if succeeded else "failed"
            row.error_code = "" if succeeded else "REFERENCE_ANALYSIS_FAILED"
            row.error_message = "" if succeeded else error_message[:2000]
            row.finished_at = utc_now()

    def _owned_project(self, principal: Principal, project_id: str):
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

    @staticmethod
    def _reference_candidate_is_isolated(candidate: Any) -> bool:
        if not isinstance(candidate, dict):
            return False
        firewall = candidate.get("content_firewall")
        return bool(
            candidate.get("analysis_mode") == "ai_style_only_transfer_v2"
            and candidate.get("content_source") == "current_matrix_only"
            and candidate.get("reference_content_reused") is False
            and isinstance(firewall, dict)
            and firewall.get("transfer_received_reference_text") is False
            and firewall.get("all_heading_levels_content_source")
            == "current_matrix_only"
        )

    def _read_json(
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
                raise WorkflowNotFound("Planning artifact not found.")
            return None, None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict("The current Planning artifact is unreadable.") from exc
        if not isinstance(payload, dict):
            raise WorkflowConflict("The current Planning artifact is invalid.")
        return payload, artifact

    def _publish_files(
        self,
        principal: Principal,
        project_id: str,
        *,
        stage_id: str,
        files: dict[str, tuple[bytes, str]],
        input_snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            stage_id,
            status="succeeded",
            input_snapshot=input_snapshot or {},
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        published: dict[str, ArtifactRecord] = {}
        for index, (logical_name, (content, artifact_type)) in enumerate(files.items()):
            filename = f"{index:03d}-{Path(logical_name).name}"
            (staging / filename).write_bytes(content)
            published[logical_name] = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                filename,
                logical_name=logical_name,
                artifact_type=artifact_type,
                producer_stage=stage_id,
                make_current=False,
            )
        return published, run

    def _matrix(self, principal: Principal, project_id: str):
        matrix, artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        rows = matrix.get("rows") if isinstance(matrix, dict) else None
        if not isinstance(rows, list) or not rows:
            raise WorkflowConflict(
                "No literature matrix is available. Confirm Discovery first."
            )
        return matrix, artifact

    @staticmethod
    def _matrix_abstract(row: dict[str, Any]) -> str:
        abstract = row.get("abstract")
        if isinstance(abstract, dict):
            abstract = abstract.get("value")
        normalized = " ".join(str(abstract or "").split()).strip()
        return "" if "unavailable or unreliable" in normalized.casefold() else normalized

    def matrix_enrichment_payload(
        self, principal: Principal, project_id: str
    ) -> dict[str, Any]:
        """Prepare source-addressable fact candidates for an asynchronous job."""

        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        paper_ids = _paper_ids(rows)
        summaries = (
            self.library_index.summaries(principal, paper_ids)
            if self.library_index is not None and self.library_index.enabled
            else {}
        )
        topic = str(matrix.get("review_topic") or "")
        papers: list[dict[str, Any]] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "")
            summary = dict(summaries.get(paper_id) or {})
            lineage = str(summary.get("source_lineage_hash") or "")
            source_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "schema_version": 1,
                        "topic": " ".join(topic.casefold().split()),
                        "paper_id": paper_id,
                        "source_lineage_hash": lineage,
                        "chunker_version": summary.get("chunker_version"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            existing = dict(row.get("fact_enrichment") or {})
            if (
                existing.get("source_fingerprint") == source_fingerprint
                and existing.get("status") in {"complete", "partial", "limited"}
            ):
                continue
            plans = build_question_query_plans(
                review_topic=topic,
                heading="",
                core_argument=topic,
                section_role="body",
            )
            candidates: dict[str, dict[str, Any]] = {}
            if (
                self.library_index is not None
                and self.library_index.enabled
                and summary.get("fulltext") == "ready"
            ):
                for plan in plans:
                    question_id = str(plan.get("question_id") or "")
                    if question_id == "section_focus":
                        continue
                    hits = self.library_index.retrieve(
                        principal,
                        str(plan.get("websearch_query") or ""),
                        allowed_papers=[paper_id],
                        top_k=2,
                        per_paper_limit=2,
                        include_neighbors=False,
                        term_groups=list(plan.get("term_groups") or []),
                        exact_phrases=list(plan.get("exact_phrases") or []),
                    )
                    for hit in hits:
                        if hit.is_neighbor:
                            continue
                        key = academic_evidence_key(
                            hit.paper_id, hit.chunk_id, hit.source_lineage_hash
                        )
                        candidate = candidates.setdefault(
                            key,
                            {
                                "evidence_key": key,
                                "paper_id": hit.paper_id,
                                "chunk_id": hit.chunk_id,
                                "page_start": hit.page_start,
                                "page_end": hit.page_end,
                                "section_path": list(hit.section_path),
                                "content_type": hit.content_type,
                                "content": hit.content,
                                "source_lineage_hash": hit.source_lineage_hash,
                                "question_ids": [],
                            },
                        )
                        if question_id not in candidate["question_ids"]:
                            candidate["question_ids"].append(question_id)
            abstract = self._matrix_abstract(row)
            if abstract:
                abstract_lineage = lineage or hashlib.sha256(
                    abstract.encode("utf-8")
                ).hexdigest()
                abstract_key = academic_evidence_key(
                    paper_id, "abstract", abstract_lineage
                )
                candidates.setdefault(
                    abstract_key,
                    {
                        "evidence_key": abstract_key,
                        "paper_id": paper_id,
                        "chunk_id": "abstract",
                        "page_start": None,
                        "page_end": None,
                        "section_path": ["Abstract"],
                        "content_type": "abstract",
                        "content": abstract,
                        "source_lineage_hash": abstract_lineage,
                        "question_ids": ["abstract_summary"],
                        "match_type": "abstract_only",
                    },
                )
            papers.append(
                {
                    "paper_id": paper_id,
                    "title": str(row.get("title") or paper_id),
                    "abstract": abstract,
                    "index_summary": summary,
                    "source_fingerprint": source_fingerprint,
                    "evidence_candidates": list(candidates.values()),
                }
            )
        return {
            "schema_version": 1,
            "project_id": project_id,
            "review_topic": topic,
            "source_matrix_artifact_id": matrix_artifact.id,
            "expected_matrix_revision": state.revision,
            "paper_count": len(rows),
            "pending_paper_count": len(papers),
            "fulltext_candidate_paper_count": sum(
                1
                for paper in papers
                if any(
                    str(item.get("content_type") or "") != "abstract"
                    for item in paper.get("evidence_candidates") or []
                    if isinstance(item, dict)
                )
            ),
            "papers": papers,
        }

    def publish_matrix_enrichment(
        self,
        principal: Principal,
        project_id: str,
        payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish per-paper facts only if the Matrix and source lineage stayed current."""

        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        if matrix_artifact.id != str(payload.get("source_matrix_artifact_id") or ""):
            raise WorkflowConflict(
                "Matrix changed while scientific facts were being extracted. Run enrichment again."
            )
        input_by_paper = {
            str(item.get("paper_id") or ""): item
            for item in payload.get("papers") or []
            if isinstance(item, dict) and item.get("paper_id")
        }
        built_by_paper = {
            str(item.get("paper_id") or ""): item
            for item in built.get("papers") or []
            if isinstance(item, dict) and item.get("paper_id")
        }
        if set(built_by_paper) != set(input_by_paper):
            raise WorkflowValidationError(
                "Matrix enrichment result does not match the pending paper set."
            )
        updated = deepcopy(matrix)
        for row in updated.get("rows") or []:
            if not isinstance(row, dict):
                continue
            paper_id = str(row.get("paper_id") or "")
            source = input_by_paper.get(paper_id)
            result = built_by_paper.get(paper_id)
            if source is None or result is None:
                continue
            candidates = {
                str(item.get("evidence_key") or ""): item
                for item in source.get("evidence_candidates") or []
                if isinstance(item, dict) and item.get("evidence_key")
            }
            facts = []
            for fact in result.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                raw_refs = [
                    ref for ref in fact.get("evidence_refs") or []
                    if isinstance(ref, dict)
                ]
                if not raw_refs or any(
                    str(ref.get("evidence_key") or "") not in candidates
                    for ref in raw_refs
                ):
                    continue
                excerpt = " ".join(
                    str(fact.get("support_excerpt") or "").split()
                ).casefold()
                if not excerpt or not str(fact.get("value") or "").strip():
                    continue
                if not str(fact.get("evidence_ceiling") or "").strip():
                    continue
                if not all(
                    excerpt
                    in " ".join(
                        str(
                            candidates[str(ref.get("evidence_key") or "")].get(
                                "content"
                            )
                            or ""
                        ).split()
                    ).casefold()
                    for ref in raw_refs
                ):
                    continue
                field_id = str(fact.get("field_id") or "").casefold()
                if not field_id or any(
                    field_id
                    not in {
                        str(value).casefold()
                        for value in candidates[
                            str(ref.get("evidence_key") or "")
                        ].get("question_ids") or []
                    }
                    for ref in raw_refs
                ):
                    continue
                facts.append({**fact, "evidence_refs": raw_refs})
            status = str(result.get("status") or "failed")
            if result.get("facts") and len(facts) < len(result.get("facts") or []):
                status = "partial" if facts else "failed"
            row["scientific_facts"] = facts
            review_status = str(result.get("review_status") or "needs_review")
            if review_status not in {"not_required", "needs_review", "human_checked"}:
                review_status = "needs_review"
            row["fact_enrichment"] = {
                "schema_version": 2,
                "status": status,
                "review_status": review_status,
                "source_fingerprint": str(source.get("source_fingerprint") or ""),
                "source_lineage_hash": str(
                    (source.get("index_summary") or {}).get("source_lineage_hash") or ""
                ),
                "fact_count": len(facts),
                "failed_fields": list(result.get("failed_fields") or []),
                "error": str(result.get("error") or "")[:1000],
                "updated_at": utc_now().isoformat(),
            }
        published_statuses = [
            str((row.get("fact_enrichment") or {}).get("status") or "pending")
            for row in updated.get("rows") or []
            if isinstance(row, dict)
        ]
        updated["fact_enrichment_summary"] = {
            "schema_version": 2,
            "source_matrix_artifact_id": matrix_artifact.id,
            "complete_count": published_statuses.count("complete"),
            "partial_count": published_statuses.count("partial"),
            "limited_count": published_statuses.count("limited"),
            "failed_count": published_statuses.count("failed"),
            "pending_count": published_statuses.count("pending"),
            "needs_review_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict)
                and str((row.get("fact_enrichment") or {}).get("review_status") or "")
                == "needs_review"
            ),
            "updated_at": utc_now().isoformat(),
        }
        outline_compatible_ids = [
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        ]
        if matrix_artifact.id not in outline_compatible_ids:
            outline_compatible_ids.append(matrix_artifact.id)
        updated["outline_compatible_matrix_artifact_ids"] = outline_compatible_ids[-20:]
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={
                    "source_matrix_artifact_id": matrix_artifact.id,
                    "source_fingerprints": {
                        paper_id: item.get("source_fingerprint")
                        for paper_id, item in input_by_paper.items()
                    },
                },
            )
            state = None
            for attempt in range(3):
                current_matrix, current_matrix_artifact = self._matrix(
                    principal, project_id
                )
                if current_matrix_artifact.id != matrix_artifact.id:
                    raise WorkflowConflict(
                        "Matrix content changed while scientific facts were being published. Run enrichment again."
                    )
                current_state = self.repository.get_stage_state(
                    principal.user_id, project_id, "matrix"
                )
                expected_revision = current_state.revision if current_state else 0
                try:
                    state = self.repository.promote_stage_artifacts_atomically(
                        principal.user_id,
                        project_id,
                        "matrix",
                        artifact_ids={
                            MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id
                        },
                        run_id=run.id,
                        expected_revision=expected_revision,
                        status="review",
                        invalidate_stages=(
                            "blueprint",
                            "sections",
                            "figure-review",
                            "figures",
                            "draft",
                            "final",
                        ),
                        expected_current_artifacts={
                            MATRIX_LOGICAL_NAME: matrix_artifact.id
                        },
                    )
                    break
                except WorkflowConflict:
                    if attempt == 2:
                        raise
            if state is None:  # pragma: no cover - defensive invariant
                raise WorkflowConflict("Scientific facts could not be published.")
        return {
            "project_id": project_id,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
            "fact_enrichment_summary": updated["fact_enrichment_summary"],
        }

    def confirm_matrix_limited_mode(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Let the user continue only after every automatic fact extraction failed."""

        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        statuses = [
            str((row.get("fact_enrichment") or {}).get("status") or "pending")
            for row in rows
        ]
        if not rows or any(status != "failed" for status in statuses):
            raise WorkflowConflict(
                "Limited mode is available only when every Matrix fact extraction failed."
            )
        summary = {
            **dict(matrix.get("fact_enrichment_summary") or {}),
            "limited_mode_confirmed": True,
            "limited_mode_confirmed_at": utc_now().isoformat(),
            "limited_mode_reason": "all_scientific_fact_extractions_failed",
        }
        updated = {**deepcopy(matrix), "fact_enrichment_summary": summary}
        outline_compatible_ids = [
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        ]
        if matrix_artifact.id not in outline_compatible_ids:
            outline_compatible_ids.append(matrix_artifact.id)
        updated["outline_compatible_matrix_artifact_ids"] = outline_compatible_ids[-20:]
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={
                    "operation": "confirm-limited-mode",
                    "source_matrix_artifact_id": matrix_artifact.id,
                },
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint", "sections", "figure-review", "figures", "draft", "final"
                ),
                expected_current_artifacts={MATRIX_LOGICAL_NAME: matrix_artifact.id},
            )
        return {
            "project_id": project_id,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
            "limited_mode_confirmed": True,
        }

    @staticmethod
    def _tag_value(value: Any) -> str:
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        if isinstance(value, (list, tuple)):
            value = next(
                (
                    item
                    for item in value
                    if str(item or "").strip()
                    and str(item).strip().casefold()
                    not in {"not specified", "none", "unknown"}
                ),
                "",
            )
        normalized = str(value or "").strip()
        return (
            ""
            if normalized.casefold()
            in {"not specified", "none", "unknown", "n/a"}
            else normalized
        )

    def _outline_sources(
        self,
        principal: Principal,
        rows: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        paper_ids = _paper_ids(rows)
        if not paper_ids:
            return {}, {}
        with database_session(self.repository.session_factory) as session:
            records = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(tuple(paper_ids)),
                    LibraryPaper.status == "active",
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
        tags_by_paper: dict[str, dict[str, Any]] = {}
        text_by_paper: dict[str, str] = {}
        rows_by_id = {str(row.get("paper_id") or ""): row for row in rows}
        for record in records:
            metadata = (
                record.metadata_json if isinstance(record.metadata_json, dict) else {}
            )
            tags = verified_structured_tags(metadata)
            row = rows_by_id.get(record.paper_id) or {}
            # Project Tags are scoped to the active project. Explicit legacy
            # confirmations and the current automatic Discovery assessment
            # both take precedence over reusable Library metadata without
            # mutating the Library record.
            project_tags = row.get("project_tags")
            if (
                row.get("project_tag_review_status") in {"confirmed", "automatic"}
                and isinstance(project_tags, dict)
            ):
                tags.update(project_tags)
            tags_by_paper[record.paper_id] = tags
            scientific_facts = [
                item
                for item in row.get("scientific_facts") or []
                if isinstance(item, dict)
                and str(item.get("field_id") or "") != "abstract_summary"
            ]
            # Route from the paper's extracted scientific object before title
            # words.  Product names in titles are otherwise easily mistaken
            # for the substrate/precursor used by the study.
            fact_priority = {
                "object_input": 0,
                "document_scope": 1,
                "transformation": 2,
                "method_family": 3,
            }
            fact_parts = [
                str(item.get("value") or "").strip()
                for item in sorted(
                    scientific_facts,
                    key=lambda item: fact_priority.get(
                        str(item.get("field_id") or ""), 10
                    ),
                )
                if str(item.get("value") or "").strip()
            ]
            parts = [
                " ".join(fact_parts),
                row.get("title"),
                " ".join(str(item) for item in (row.get("keywords") or [])),
                record.title,
                " ".join(str(item) for item in (record.keywords_json or [])),
                row.get("abstract"),
                row.get("main_content"),
            ]
            text_by_paper[record.paper_id] = " ".join(
                str(part) for part in parts if str(part or "").strip()
            ).casefold()
        return tags_by_paper, text_by_paper

    @staticmethod
    def _taxonomy_match_text(value: Any) -> str:
        """Normalize harmless typesetting punctuation before phrase matching.

        Chemical titles often wrap a substituent name in parentheses, as in
        ``(allenylmethyl)silanes``.  Removing grouping brackets from both the
        evidence text and taxonomy terms keeps those typography variants from
        becoming artificial routing failures while preserving other chemical
        punctuation used by the rules.
        """

        normalized = str(value or "").strip().casefold()
        return re.sub(r"[()\[\]{}]", "", normalized)

    @staticmethod
    def _semantic_outline_groups(
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        try:
            topic_text = " ".join(
                text for text in text_by_paper.values() if str(text or "").strip()
            )
            rules = [
                (label, aliases)
                for label, category, aliases in load_taxonomy_rules(
                    Path.cwd(),
                    profile=taxonomy_profile,
                    topic_text=topic_text,
                )
                if category == tag_key
            ]
        except TaxonomyConfigurationError:
            return {}
        groups: dict[str, list[str]] = {}
        other: list[str] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            text = PlanningService._taxonomy_match_text(
                text_by_paper.get(paper_id, "")
            )
            ranked: list[tuple[int, int, str]] = []
            for index, (label, aliases) in enumerate(rules):
                score = 0
                for term in (label, *aliases):
                    normalized = PlanningService._taxonomy_match_text(term)
                    if not normalized:
                        continue
                    pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
                    match = re.search(pattern, text)
                    if match:
                        # _outline_sources puts source-addressable scientific
                        # facts first, followed by title/keywords and abstract.
                        # Prefer the extracted study object over product words
                        # and related-work mentions later in the source.
                        score = max(
                            score,
                            100_000
                            - min(match.start(), 99_999)
                            + len(normalized.split()) * 10
                            + len(normalized),
                        )
                ranked.append((score, -index, label))
            best = max(ranked, default=(0, 0, ""))
            if best[0] > 0:
                groups.setdefault(best[2], []).append(paper_id)
            else:
                other.append(paper_id)
        if other:
            groups[ROUTING_REQUIRED_LABEL] = other
        return groups

    def _outline_groups(
        self,
        rows: list[dict[str, Any]],
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        other: list[str] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            label = self._tag_value(
                (tags_by_paper.get(paper_id) or {}).get(tag_key)
            )
            if label:
                groups.setdefault(label, []).append(paper_id)
            else:
                other.append(paper_id)
        if other:
            groups[ROUTING_REQUIRED_LABEL] = other
        if not other:
            return groups

        unresolved_set = set(other)
        unresolved_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() in unresolved_set
        ]
        semantic = self._semantic_outline_groups(
            unresolved_rows,
            text_by_paper,
            tag_key=tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        repaired = {
            label: list(paper_ids)
            for label, paper_ids in groups.items()
            if label != ROUTING_REQUIRED_LABEL and paper_ids
        }
        for label, paper_ids in semantic.items():
            if label == ROUTING_REQUIRED_LABEL or not paper_ids:
                continue
            bucket = repaired.setdefault(label, [])
            bucket.extend(paper_id for paper_id in paper_ids if paper_id not in bucket)
        still_unresolved = list(semantic.get(ROUTING_REQUIRED_LABEL) or [])
        if still_unresolved:
            # A built-in outline must remain actionable.  When the configured
            # taxonomy cannot confidently name a narrower category, keep the
            # papers in an explicit analytical boundary section instead of a
            # workflow-only placeholder that blocks Blueprint confirmation.
            repaired[CROSS_CATEGORY_BOUNDARY_LABEL] = still_unresolved
        return repaired or groups

    def _auto_repair_generated_routing_sections(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve system-created routing placeholders before Blueprint build.

        User-authored catch-all sections remain subject to the normal academic
        gate.  Only the exact placeholder emitted by older built-in outline
        versions is repaired automatically.  Taxonomy matches are merged into
        an existing same-title section when possible; otherwise a defensible
        category is inserted before the conclusion.  Truly unresolved sources
        enter a named cross-category boundary analysis rather than ``Other``.
        """

        style = str(outline_style or "").casefold()
        definition = OUTLINE_STYLES.get(style)
        if definition is None:
            return sections, []

        repaired = deepcopy(sections)
        repairable_titles = {
            ROUTING_REQUIRED_LABEL.casefold(),
            CROSS_CATEGORY_BOUNDARY_LABEL.casefold(),
        }
        placeholder_indexes = [
            index
            for index, section in enumerate(repaired)
            if str(section.get("section_role") or "body").casefold() == "body"
            and str(section.get("title") or "").strip().casefold()
            in repairable_titles
        ]
        if not placeholder_indexes:
            return repaired, []

        unresolved_ids = list(
            dict.fromkeys(
                paper_id
                for index in placeholder_indexes
                for paper_id in (repaired[index].get("paper_ids") or [])
            )
        )
        unresolved_set = set(unresolved_ids)
        unresolved_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() in unresolved_set
        ]
        semantic = self._semantic_outline_groups(
            unresolved_rows,
            text_by_paper,
            tag_key=definition["tag_key"],
            taxonomy_profile=taxonomy_profile,
        )
        grouped: dict[str, list[str]] = {
            label: list(dict.fromkeys(paper_ids))
            for label, paper_ids in semantic.items()
            if label != ROUTING_REQUIRED_LABEL and paper_ids
        }
        routed = {paper_id for paper_ids in grouped.values() for paper_id in paper_ids}
        still_unresolved = [paper_id for paper_id in unresolved_ids if paper_id not in routed]
        if still_unresolved:
            grouped[CROSS_CATEGORY_BOUNDARY_LABEL] = still_unresolved

        repaired = [
            section
            for index, section in enumerate(repaired)
            if index not in placeholder_indexes
        ]
        existing_by_title = {
            str(section.get("title") or "").strip().casefold(): section
            for section in repaired
            if str(section.get("section_role") or "body").casefold() == "body"
        }
        insert_at = next(
            (
                index
                for index, section in enumerate(repaired)
                if str(section.get("section_role") or "").casefold() == "conclusion"
            ),
            len(repaired),
        )
        adjustments: list[dict[str, Any]] = []
        source_titles = list(
            dict.fromkeys(
                str(sections[index].get("title") or ROUTING_REQUIRED_LABEL)
                for index in placeholder_indexes
            )
        )
        for label, paper_ids in grouped.items():
            target = existing_by_title.get(label.casefold())
            created = target is None
            if target is None:
                target = {
                    "title": label,
                    "paper_ids": [],
                    "context_paper_ids": [],
                    "section_role": "body",
                    "purpose": (
                        f"compare the selected papers within this {definition['axis']} "
                        "category and state its evidence boundaries."
                    ),
                    "notes": "Automatically routed from a system-generated placeholder.",
                }
                repaired.insert(insert_at, target)
                insert_at += 1
                existing_by_title[label.casefold()] = target
            bucket = target.setdefault("paper_ids", [])
            bucket.extend(paper_id for paper_id in paper_ids if paper_id not in bucket)
            adjustments.append(
                {
                    "source_section": ", ".join(source_titles),
                    "target_section": label,
                    "paper_ids": list(paper_ids),
                    "method": (
                        "taxonomy_evidence_match"
                        if label != CROSS_CATEGORY_BOUNDARY_LABEL
                        else "cross_category_boundary_fallback"
                    ),
                    "created_section": created,
                }
            )
        return repaired, adjustments

    @classmethod
    def _contextual_outline_paper_ids(
        cls,
        rows: list[dict[str, Any]],
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
    ) -> list[str]:
        """Identify sources that frame the field but are not primary studies.

        These papers remain available to the introduction as contextual
        evidence.  They are not forced into a body taxonomy where a review or
        perspective would create an artificial catch-all category.
        """

        contextual: list[str] = []
        scope_terms = (
            "review",
            "comprehensive review",
            "account",
            "perspective",
            "book",
            "book chapter",
        )
        context_pattern = re.compile(
            r"\b(?:this|the present) review\b|\bwe review\b|"
            r"\breview (?:will|article|paper)\b|\bcomprehensive review\b|"
            r"\bperspective (?:on|article)\b|"
            r"\b(?:this|the|an) account\b|"
            r"\baccount (?:of|on|surveys|reviews|concerns|summarizes)\b",
            re.I,
        )
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            document_scope = cls._tag_value(
                (tags_by_paper.get(paper_id) or {}).get("document_scope")
            ).casefold()
            text = text_by_paper.get(paper_id, "")
            if any(term in document_scope for term in scope_terms) or context_pattern.search(text):
                contextual.append(paper_id)
        return contextual

    def _realign_generated_body_sections(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Realign a system outline when scientific facts contradict old routing.

        Saved generated outlines can predate fact extraction or taxonomy fixes.
        This pass is intentionally disabled for manually edited outlines.  It
        preserves roles and ordering while moving each paper to the category
        supported by its source-addressable study object.
        """

        definition = OUTLINE_STYLES.get(str(outline_style or "").casefold())
        if definition is None:
            return deepcopy(sections), []
        body_paper_ids = list(
            dict.fromkeys(
                str(paper_id)
                for section in sections
                if str(section.get("section_role") or "body").casefold() == "body"
                for paper_id in section.get("paper_ids") or []
                if str(paper_id or "").strip()
            )
        )
        body_set = set(body_paper_ids)
        fact_evidence_ids = {
            str(row.get("paper_id") or "")
            for row in rows
            if str(row.get("paper_id") or "") in body_set
            and any(
                isinstance(fact, dict)
                and str(fact.get("field_id") or "") != "abstract_summary"
                and str(fact.get("value") or "").strip()
                for fact in row.get("scientific_facts") or []
            )
        }
        if not fact_evidence_ids:
            return deepcopy(sections), []
        semantic = self._semantic_outline_groups(
            [
                row
                for row in rows
                if str(row.get("paper_id") or "").strip() in fact_evidence_ids
            ],
            text_by_paper,
            tag_key=definition["tag_key"],
            taxonomy_profile=taxonomy_profile,
        )
        target_by_paper: dict[str, str] = {}
        for label, paper_ids in semantic.items():
            target = (
                CROSS_CATEGORY_BOUNDARY_LABEL
                if label == ROUTING_REQUIRED_LABEL
                else label
            )
            for paper_id in paper_ids:
                target_by_paper.setdefault(str(paper_id), target)

        repaired = deepcopy(sections)
        body_snapshot = [
            (section, list(section.get("paper_ids") or []))
            for section in repaired
            if str(section.get("section_role") or "body").casefold() == "body"
        ]
        existing_by_title = {
            str(section.get("title") or "").strip().casefold(): section
            for section, _paper_ids_snapshot in body_snapshot
        }
        insert_at = next(
            (
                index
                for index, section in enumerate(repaired)
                if infer_section_role(
                    section.get("title"), section.get("section_role")
                )
                == "conclusion"
            ),
            len(repaired),
        )
        adjustments: list[dict[str, Any]] = []
        for source, source_papers in body_snapshot:
            source_title = str(source.get("title") or "").strip()
            for paper_id in source_papers:
                target_title = target_by_paper.get(str(paper_id))
                if not target_title or target_title.casefold() == source_title.casefold():
                    continue
                source["paper_ids"] = [
                    current
                    for current in source.get("paper_ids") or []
                    if str(current) != str(paper_id)
                ]
                target = existing_by_title.get(target_title.casefold())
                created = target is None
                if target is None:
                    target = {
                        "title": target_title,
                        "paper_ids": [],
                        "context_paper_ids": [],
                        "section_role": "body",
                        "purpose": (
                            f"compare the selected papers within this {definition['axis']} "
                            "category and state its evidence boundaries."
                        ),
                        "notes": "Automatically realigned from source-addressable scientific facts.",
                    }
                    repaired.insert(insert_at, target)
                    insert_at += 1
                    existing_by_title[target_title.casefold()] = target
                if paper_id not in target["paper_ids"]:
                    target["paper_ids"].append(paper_id)
                adjustments.append(
                    {
                        "source_section": source_title,
                        "target_section": target_title,
                        "paper_ids": [paper_id],
                        "method": "scientific_object_reassignment",
                        "created_section": created,
                    }
                )
        repaired = [
            section
            for section in repaired
            if str(section.get("section_role") or "body").casefold() != "body"
            or bool(section.get("paper_ids"))
        ]
        return repaired, adjustments

    def _outline_document(
        self,
        style: str,
        rows: list[dict[str, Any]],
        *,
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        taxonomy_profile: str,
    ) -> str:
        definition = OUTLINE_STYLES[style]
        contextual_paper_ids = self._contextual_outline_paper_ids(
            rows,
            tags_by_paper,
            text_by_paper,
        )
        contextual_set = set(contextual_paper_ids)
        analytical_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() not in contextual_set
        ]
        groups = self._outline_groups(
            analytical_rows,
            tags_by_paper,
            text_by_paper,
            tag_key=definition["tag_key"],
            taxonomy_profile=taxonomy_profile,
        )
        lines = [
            "# Selected Outline",
            "",
            f"Primary structure: {definition['en']}.",
            "This working outline remains fully editable before Blueprint generation.",
            "",
            "## Introduction",
            "Section role: introduction",
            f"Purpose: {definition['introduction']}.",
        ]
        if contextual_paper_ids:
            lines.extend(
                [
                    f"Context papers: {', '.join(contextual_paper_ids)}.",
                    "Notes: Use these field-level sources for scope and historical framing; do not treat them as primary body evidence.",
                ]
            )
        lines.append("")
        for index, (label, paper_ids) in enumerate(groups.items(), start=1):
            lines.extend(
                [
                    f"## {index}. {label}",
                    "Section role: body",
                    f"Assigned papers: {', '.join(paper_ids)}.",
                    f"Purpose: compare the selected papers within this {definition['axis']} category.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Cross-category comparison and conclusion",
                "Section role: conclusion",
                "Purpose: compare the main systems, outcomes, evidence boundaries, limitations, and future directions.",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _validate_outline(markdown: str, matrix_ids: set[str]) -> str:
        text = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise WorkflowValidationError("Outline Markdown must not be empty.")
        if len(text) > 250_000:
            raise WorkflowValidationError("Outline Markdown exceeds 250,000 characters.")
        sections = _outline_sections(text)
        if not sections:
            raise WorkflowValidationError(
                "Outline Markdown needs at least one level-2 heading (##)."
            )
        missing = [
            section["title"]
            for section in sections
            if not section["paper_ids"]
            and section.get("section_role")
            not in {"introduction", "conclusion", "references"}
        ]
        if missing:
            raise WorkflowValidationError(
                "Every major section must assign at least one paper.",
                details={"sections": missing},
            )
        unknown = sorted(
            {
                paper_id
                for section in sections
                for paper_id in [
                    *section["paper_ids"],
                    *section.get("context_paper_ids", []),
                ]
                if paper_id not in matrix_ids
            }
        )
        if unknown:
            raise WorkflowValidationError(
                "Outline paper assignments must resolve to the current Matrix.",
                details={"paper_ids": unknown},
            )
        return text + "\n"

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        matrix, matrix_artifact = self._matrix(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME, required=False
        )
        references, _references_artifact = self._read_json(
            principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
        )
        blueprint, blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        blueprint_state = self.repository.get_stage_state(
            principal.user_id, project_id, "blueprint"
        )
        rows = matrix["rows"]
        project = self._owned_project(principal, project_id)
        tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
        selected_ids: list[str] = []
        for group in (discovery or {}).get("results") or []:
            if not isinstance(group, dict) or group.get("keep") is False:
                continue
            for row in group.get("local_results") or []:
                if (
                    isinstance(row, dict)
                    and row.get("selected_for_matrix")
                    and str(row.get("role") or "") != "excluded"
                ):
                    paper_id = str(row.get("paper_id") or "")
                    if paper_id and paper_id not in selected_ids:
                        selected_ids.append(paper_id)
        matrix_sync = dict(matrix.get("sync") or {})
        selection_current = bool(
            selected_ids
            and set(_paper_ids(rows)) == set(selected_ids)
        )
        generated = [
            {
                "candidate_id": style,
                "outline_style": style,
                "labels": {"en": definition["en"], "zh": definition["zh"]},
                "outline_md": self._outline_document(
                    style,
                    rows,
                    tags_by_paper=tags_by_paper,
                    text_by_paper=text_by_paper,
                    taxonomy_profile=project.taxonomy_profile,
                ),
                "source": "builtin",
            }
            for style, definition in OUTLINE_STYLES.items()
        ]
        if outline_artifact is not None and outline is not None:
            generated.append(
                {
                    "candidate_id": "saved-current",
                    "outline_style": outline.get("outline_style", "custom"),
                    "labels": {"en": "Saved outline", "zh": "已保存大纲"},
                    "outline_md": str(outline.get("outline_md") or ""),
                    "source": "saved",
                    "artifact_id": outline_artifact.id,
                }
            )
        all_reference_candidates = list((references or {}).get("candidates") or [])
        reference_candidates = [
            candidate
            for candidate in all_reference_candidates
            if self._reference_candidate_is_isolated(candidate)
        ]
        outline_compatible_ids = {
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        }
        # Backward compatibility for enrichment artifacts created before the
        # explicit compatibility lineage was introduced.
        enrichment_source_id = str(
            (matrix.get("fact_enrichment_summary") or {}).get(
                "source_matrix_artifact_id"
            )
            or ""
        )
        if enrichment_source_id:
            outline_compatible_ids.add(enrichment_source_id)
        outline_source_id = str((outline or {}).get("source_matrix_artifact_id") or "")
        outline_current = bool(
            outline is not None
            and outline_artifact is not None
            and (
                outline_source_id == matrix_artifact.id
                or outline_source_id in outline_compatible_ids
            )
        )
        blueprint_current = bool(
            blueprint is not None
            and blueprint_artifact is not None
            and outline_artifact is not None
            and outline_current
            and str(blueprint.get("source_matrix_artifact_id") or "")
            == matrix_artifact.id
            and str(blueprint.get("source_outline_artifact_id") or "")
            == outline_artifact.id
            and blueprint_state is not None
            and blueprint_state.status != "stale"
        )
        scope_contract = dict((outline or {}).get("scope_contract") or {})
        scope_report = dict((outline or {}).get("scope_diagnostics") or {})
        coverage_report = dict((outline or {}).get("coverage_diagnostics") or {})
        basis = dict((outline or {}).get("classification_basis") or {})
        outline_diagnostics = dict((outline or {}).get("taxonomy_diagnostics") or {})
        enrichment_jobs = self.repository.list_project_jobs(
            principal.user_id, project_id, job_type="matrix.enrich", limit=20
        )
        enrichment_counts = {
            status: sum(
                1
                for row in rows
                if str((row.get("fact_enrichment") or {}).get("status") or "pending")
                == status
            )
            for status in ("pending", "complete", "partial", "limited", "failed")
        }
        enrichment_summary = dict(matrix.get("fact_enrichment_summary") or {})
        all_enrichment_failed = bool(rows) and enrichment_counts["failed"] == len(rows)
        latest_enrichment_job = enrichment_jobs[0] if enrichment_jobs else None
        failed_publish_with_pending_rows = bool(
            enrichment_counts["pending"]
            and latest_enrichment_job is not None
            and latest_enrichment_job.status in {"failed", "cancelled", "interrupted"}
        )
        return {
            "project_id": project_id,
            "topic": str(matrix.get("review_topic") or (discovery or {}).get("topic") or ""),
            "literature_matrix": matrix,
            "matrix_artifact_id": matrix_artifact.id,
            "matrix_revision": matrix_state.revision if matrix_state else 0,
            "matrix_sync": {**matrix_sync, "selection_current": selection_current},
            "matrix_enrichment": {
                "summary": enrichment_summary,
                "counts": enrichment_counts,
                "jobs": [_planning_job_payload(job) for job in enrichment_jobs],
                "all_failed": all_enrichment_failed,
                "failed_publish_with_pending_rows": failed_publish_with_pending_rows,
                "limited_mode_confirmed": bool(
                    enrichment_summary.get("limited_mode_confirmed")
                ),
                "planning_blocked": bool(
                    (
                        all_enrichment_failed
                        and not enrichment_summary.get("limited_mode_confirmed")
                    )
                    or failed_publish_with_pending_rows
                ),
            },
            "discovery_selection": {
                "selected_paper_count": len(selected_ids),
                "selected_paper_ids": selected_ids,
                "selection_current": selection_current,
            },
            "selected_outline_md": str((outline or {}).get("outline_md") or ""),
            "outline_selection": (
                {**outline, "artifact_id": outline_artifact.id}
                if outline is not None and outline_artifact is not None
                else None
            ),
            "outline_current": outline_current,
            "scope_contract": scope_contract,
            "scope_diagnostics": dict(
                (blueprint or {}).get("scope_diagnostics")
                or scope_report
            ),
            "coverage_diagnostics": dict(
                (blueprint or {}).get("coverage_diagnostics")
                or coverage_report
            ),
            "classification_basis": basis,
            "taxonomy_diagnostics": dict(
                (blueprint or {}).get("taxonomy_diagnostics")
                or outline_diagnostics
            ),
            "outline_candidates": generated + reference_candidates,
            "reference_outline_candidates": reference_candidates,
            "legacy_reference_outline_count": len(all_reference_candidates)
            - len(reference_candidates),
            "section_blueprint": blueprint,
            "blueprint_artifact_id": blueprint_artifact.id if blueprint_artifact else None,
            "blueprint_revision": blueprint_state.revision if blueprint_state else 0,
            "blueprint_current": blueprint_current,
            "section_writing_plan_md": str(
                (blueprint or {}).get("section_writing_plan_md") or ""
            ),
            "workspace": {
                "active_stage": "planning",
                "tabs": [
                    {
                        "id": "matrix",
                        "labels": {"en": "Literature Matrix", "zh": "文献矩阵"},
                    },
                    {
                        "id": "blueprint",
                        "labels": {"en": "Blueprint", "zh": "章节蓝图"},
                    },
                ],
            },
        }

    def update_matrix_row(
        self,
        principal: Principal,
        project_id: str,
        paper_id: str,
        *,
        revision: int,
        main_content: str | None,
        most_relevant_figure: dict[str, Any] | None,
        scientific_facts: list[dict[str, Any]] | None,
        mark_complete: bool,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        updated = deepcopy(matrix)
        row = next(
            (
                item
                for item in updated["rows"]
                if isinstance(item, dict) and str(item.get("paper_id")) == paper_id
            ),
            None,
        )
        if row is None:
            raise WorkflowNotFound("Matrix paper was not found.")
        if main_content is not None:
            row["main_content"] = str(main_content).strip()
        if most_relevant_figure is not None:
            row["most_relevant_figure"] = dict(most_relevant_figure)
        if scientific_facts is not None:
            existing_facts = {
                str(item.get("fact_id") or ""): item
                for item in row.get("scientific_facts") or []
                if isinstance(item, dict) and item.get("fact_id")
            }
            submitted_ids = {
                str(item.get("fact_id") or "")
                for item in scientific_facts
                if isinstance(item, dict) and item.get("fact_id")
            }
            if submitted_ids != set(existing_facts):
                raise WorkflowValidationError(
                    "Matrix fact edits must preserve the current source-addressable fact set."
                )
            revised_facts = []
            for submitted in scientific_facts:
                fact_id = str(submitted.get("fact_id") or "")
                current = existing_facts[fact_id]
                value = " ".join(str(submitted.get("value") or "").split()).strip()
                ceiling = " ".join(
                    str(submitted.get("evidence_ceiling") or "").split()
                ).strip()
                if not value or len(value) > 4000 or len(ceiling) > 2000:
                    raise WorkflowValidationError(
                        "A Matrix fact edit has an invalid value or evidence ceiling."
                    )
                revised_facts.append(
                    {
                        **current,
                        "value": value,
                        "evidence_ceiling": ceiling
                        or str(current.get("evidence_ceiling") or ""),
                        "human_checked": True,
                        "review_status": "human_edited",
                        "human_edited_at": utc_now().isoformat(),
                    }
                )
            row["scientific_facts"] = revised_facts
        if mark_complete and len(re.sub(r"\s+", "", str(row.get("main_content") or ""))) < 300:
            raise WorkflowConflict(
                "Add at least 300 characters of full-paper reading notes before marking this paper complete."
            )
        row["matrix_status"] = (
            "full_reading_complete" if mark_complete else "needs_full_reading"
        )
        updated.pop("outline_compatible_matrix_artifact_ids", None)
        updated["updated_at"] = utc_now().isoformat()
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={"paper_id": paper_id},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint",
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "project_id": project_id,
            "paper_id": paper_id,
            "row": row,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
        }

    def save_outline(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        outline_style: str,
        outline_md: str | None,
        manual: bool,
        scope_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        project = self._owned_project(principal, project_id)
        rows = matrix["rows"]
        matrix_ids = set(_paper_ids(rows))
        style = str(outline_style or "").strip().casefold()
        if style == "custom" and not manual:
            markdown = ""
            complete = False
        elif style.startswith("reference:") and not manual:
            references, _artifact = self._read_json(
                principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
            )
            candidate_id = style.removeprefix("reference:")
            candidate = next(
                (
                    item
                    for item in (references or {}).get("candidates") or []
                    if str(item.get("candidate_id")) == candidate_id
                ),
                None,
            )
            if not isinstance(candidate, dict):
                raise WorkflowNotFound("Reference outline candidate was not found.")
            if not self._reference_candidate_is_isolated(candidate):
                raise WorkflowConflict(
                    "This legacy reference outline did not pass content isolation. Upload the reference again to learn format only."
                )
            markdown = self._validate_outline(
                str(candidate.get("outline_md") or ""), matrix_ids
            )
            complete = True
        elif manual:
            if style != "custom" and style not in OUTLINE_STYLES and not style.startswith("reference:"):
                raise WorkflowValidationError("Unknown outline style.")
            markdown = self._validate_outline(str(outline_md or ""), matrix_ids)
            complete = True
        else:
            if style not in OUTLINE_STYLES:
                raise WorkflowValidationError("Unknown outline style.")
            tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
            markdown = self._outline_document(
                style,
                rows,
                tags_by_paper=tags_by_paper,
                text_by_paper=text_by_paper,
                taxonomy_profile=project.taxonomy_profile,
            )
            complete = True
        current_outline, current_outline_artifact = self._read_json(
            principal,
            project_id,
            OUTLINE_LOGICAL_NAME,
            required=False,
        )
        parsed_sections = _outline_sections(markdown) if complete else []
        diagnostics = taxonomy_diagnostics(parsed_sections, _paper_ids(rows))
        previous_scope = (
            (current_outline or {}).get("scope_contract")
            if isinstance(current_outline, dict)
            else None
        )
        previous_style = str((current_outline or {}).get("outline_style") or "")
        scope_input: dict[str, Any] | None = None
        if isinstance(scope_contract, dict):
            scope_input = {**scope_contract, "source": "user_edited"}
        elif (
            isinstance(previous_scope, dict)
            and previous_scope.get("source") == "user_edited"
            and previous_style == style
        ):
            scope_input = previous_scope
        scope = derive_scope_contract(
            matrix.get("review_topic"),
            style,
            rows,
            current=scope_input,
        )
        scope_report = scope_diagnostics(scope)
        coverage_report = coverage_diagnostics(scope, rows)
        basis = classification_basis(style)
        payload = {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "outline_style": style,
            "outline_md": markdown,
            "outline_complete": complete,
            "selection_source": "manual" if manual else "custom_draft" if not complete else "template",
            "manually_edited": bool(manual),
            "source_matrix_artifact_id": matrix_artifact.id,
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "taxonomy_diagnostics": diagnostics,
            "saved_at": utc_now().isoformat(),
        }
        current_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if (
            current_outline_artifact is not None
            and isinstance(current_outline, dict)
            and current_state is not None
            and current_state.revision == int(revision)
            and str(current_outline.get("outline_style") or "") == style
            and str(current_outline.get("outline_md") or "") == markdown
            and bool(current_outline.get("outline_complete")) == complete
            and str(current_outline.get("source_matrix_artifact_id") or "")
            == matrix_artifact.id
            and current_outline.get("scope_contract") == scope
            and current_outline.get("scope_diagnostics") == scope_report
            and current_outline.get("coverage_diagnostics") == coverage_report
            and current_outline.get("classification_basis") == basis
            and current_outline.get("taxonomy_diagnostics") == diagnostics
        ):
            return {
                "project_id": project_id,
                "outline_style": style,
                "selected_outline_md": markdown,
                "outline_complete": complete,
                "blueprint_pending": complete,
                "scope_contract": scope,
                "scope_diagnostics": scope_report,
                "coverage_diagnostics": coverage_report,
                "classification_basis": basis,
                "taxonomy_diagnostics": diagnostics,
                "outline_artifact_id": current_outline_artifact.id,
                "matrix_revision": current_state.revision,
                "unchanged": True,
            }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={OUTLINE_LOGICAL_NAME: (_json_bytes(payload), "json")},
                input_snapshot={"outline_style": style, "manual": manual},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={OUTLINE_LOGICAL_NAME: published[OUTLINE_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint",
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "project_id": project_id,
            "outline_style": style,
            "selected_outline_md": markdown,
            "outline_complete": complete,
            "blueprint_pending": complete,
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "taxonomy_diagnostics": diagnostics,
            "outline_artifact_id": published[OUTLINE_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
        }

    def _analyze_reference_document(
        self,
        principal: Principal,
        project_id: str,
        *,
        candidate_id: str,
        safe_name: str,
        raw: bytes,
        matrix: dict[str, Any],
    ) -> dict[str, Any]:
        if self.scientific_runner is None:
            raise WorkflowValidationError(
                "Reference-format analysis is unavailable in this deployment."
            )
        environment: dict[str, str] = {}
        gateway_context: SimpleNamespace | None = None
        if self.model_gateway is not None:
            gateway_context = self._begin_reference_gateway_job(
                principal, project_id, candidate_id
            )
            gateway_normal, gateway_secrets = self.model_gateway.environment_for_job(
                gateway_context
            )
            environment = {**gateway_normal, **gateway_secrets}
        elif self.provider_settings is not None:
            try:
                environment = self.provider_settings.runtime_environment(
                    principal,
                    provider_kinds=(ProviderKind.TEXT,),
                )
            except ProviderSettingsError as exc:
                raise WorkflowValidationError(
                    "Configure and enable the text provider before analyzing a reference review."
                ) from exc
            if not environment.get("OPENAI_API_KEY"):
                raise WorkflowValidationError(
                    "Configure and enable the text provider before analyzing a reference review."
                )
        script = (
            self.root
            / "skills"
            / "review-reference-outline-template"
            / "scripts"
            / "analyze_reference_review.py"
        )
        if not script.is_file():
            if gateway_context is not None:
                self._finish_reference_gateway_job(
                    gateway_context.job_id,
                    succeeded=False,
                    error_message="Reference analysis skill is not installed.",
                )
            raise WorkflowValidationError(
                "The reference-format analysis skill is not installed."
            )
        staging_parent = self.artifacts.workspace_manager.trusted_user_directory(
            principal.user_id,
            ".review-writer",
            "reference-outline-analysis",
        )
        with tempfile.TemporaryDirectory(
            prefix=f"{candidate_id}-", dir=staging_parent
        ) as temporary:
            staging = Path(temporary).resolve()
            source = staging / safe_name
            matrix_path = staging / "literature_matrix.json"
            output_path = staging / "candidate.json"
            source.write_bytes(raw)
            matrix_path.write_text(
                json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            normal_environment = {
                key: value
                for key, value in environment.items()
                if not SENSITIVE_ENVIRONMENT_KEY.search(key)
            }
            secret_environment = {
                key: value
                for key, value in environment.items()
                if SENSITIVE_ENVIRONMENT_KEY.search(key)
            }
            try:
                self.scientific_runner.run(
                    (
                        sys.executable,
                        str(script),
                        "--input",
                        str(source),
                        "--matrix",
                        str(matrix_path),
                        "--output",
                        str(output_path),
                        "--project-id",
                        project_id,
                        "--candidate-id",
                        candidate_id,
                    ),
                    cwd=self.root,
                    staging_directory=staging,
                    expected_outputs=("candidate.json",),
                    env=normal_environment,
                    secret_env=secret_environment,
                    timeout_seconds=900,
                )
                result = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if gateway_context is not None:
                    self._finish_reference_gateway_job(
                        gateway_context.job_id,
                        succeeded=False,
                        error_message=str(exc),
                    )
                raise WorkflowConflict(
                    "Reference-format analysis returned an unreadable result."
                ) from exc
            except Exception as exc:
                if gateway_context is not None:
                    self._finish_reference_gateway_job(
                        gateway_context.job_id,
                        succeeded=False,
                        error_message=str(exc),
                    )
                raise
        if not isinstance(result, dict):
            if gateway_context is not None:
                self._finish_reference_gateway_job(
                    gateway_context.job_id,
                    succeeded=False,
                    error_message="Reference analysis returned a non-object result.",
                )
            raise WorkflowConflict(
                "Reference-format analysis returned an invalid result."
            )
        if not self._reference_candidate_is_isolated(result):
            if gateway_context is not None:
                self._finish_reference_gateway_job(
                    gateway_context.job_id,
                    succeeded=False,
                    error_message="Reference analysis failed the content-isolation gate.",
                )
            raise WorkflowConflict(
                "Reference analysis failed the content-isolation gate; the uploaded review was not added."
            )
        if gateway_context is not None:
            self._finish_reference_gateway_job(
                gateway_context.job_id,
                succeeded=True,
            )
        return result

    def register_reference(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        filename: str,
        content_base64: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        self._matrix(principal, project_id)
        safe_name = Path(str(filename or "")).name
        suffix = Path(safe_name).suffix.casefold()
        if suffix not in {".pdf", ".docx", ".md", ".txt"}:
            raise WorkflowValidationError(
                "Upload a PDF, DOCX, Markdown, or text review document."
            )
        try:
            raw = base64.b64decode(str(content_base64 or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkflowValidationError("Reference content is not valid base64.") from exc
        if not raw:
            raise WorkflowValidationError("Uploaded reference file is empty.")
        if len(raw) > 30 * 1024 * 1024:
            raise WorkflowValidationError("Uploaded reference file exceeds 30 MB.")
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        matrix_ids = _paper_ids(matrix["rows"])
        candidate_id = f"reference-{uuid.uuid4().hex[:12]}"
        analysis = self._analyze_reference_document(
            principal,
            project_id,
            candidate_id=candidate_id,
            safe_name=safe_name,
            raw=raw,
            matrix=matrix,
        )
        outline_text = self._validate_outline(
            str(analysis.get("outline_md") or ""), set(matrix_ids)
        )
        analysis_mode = str(analysis.get("analysis_mode") or "")
        references, _references_artifact = self._read_json(
            principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
        )
        index = deepcopy(references or {"project_id": project_id, "candidates": []})
        source_logical = f"planning/references/{candidate_id}/{safe_name}"
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={
                    source_logical: (raw, suffix.lstrip(".")),
                },
                input_snapshot={"filename": safe_name},
            )
            candidate = {
                "candidate_id": candidate_id,
                "outline_style": f"reference:{candidate_id}",
                "labels": {"en": safe_name, "zh": f"参考大纲：{safe_name}"},
                "outline_md": outline_text,
                "source": "reference",
                "source_name": safe_name,
                "source_artifact_id": published[source_logical].id,
                "analysis_mode": analysis_mode,
                "content_source": "current_matrix_only",
                "reference_content_reused": False,
                "content_firewall": deepcopy(analysis.get("content_firewall") or {}),
                "reference_structure_metrics": deepcopy(
                    analysis.get("reference_structure_metrics") or {}
                ),
                "writing_style": deepcopy(analysis.get("writing_style") or {}),
                "created_at": utc_now().isoformat(),
            }
            index["candidates"] = [*(index.get("candidates") or []), candidate]
            index_published, index_run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={REFERENCE_INDEX_LOGICAL_NAME: (_json_bytes(index), "json")},
                input_snapshot={"source_artifact_id": published[source_logical].id},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={
                    REFERENCE_INDEX_LOGICAL_NAME: index_published[
                        REFERENCE_INDEX_LOGICAL_NAME
                    ].id
                },
                run_id=index_run.id,
                expected_revision=revision,
                status="review",
            )
        return {
            "project_id": project_id,
            "candidate": candidate,
            "matrix_revision": state.revision,
        }

    def generate_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        matrix_rows = [
            row for row in matrix.get("rows") or [] if isinstance(row, dict)
        ]
        all_fact_extraction_failed = bool(matrix_rows) and all(
            str((row.get("fact_enrichment") or {}).get("status") or "pending")
            == "failed"
            for row in matrix_rows
        )
        if all_fact_extraction_failed and not bool(
            (matrix.get("fact_enrichment_summary") or {}).get(
                "limited_mode_confirmed"
            )
        ):
            raise WorkflowConflict(
                "Every Matrix fact extraction failed. Retry extraction or explicitly continue in limited mode."
            )
        project = self._owned_project(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if not outline.get("outline_complete") or not str(outline.get("outline_md") or "").strip():
            raise WorkflowConflict(
                "The selected outline is blank or incomplete. Edit and save it before Blueprint generation."
            )
        matrix_ids = set(_paper_ids(matrix["rows"]))
        parsed = _outline_sections(str(outline["outline_md"]))
        matrix_order = _paper_ids(matrix["rows"])
        auto_routing_adjustments: list[dict[str, Any]] = []
        resolved_outline_md = str(outline["outline_md"])
        tags_by_paper: dict[str, dict[str, Any]] = {}
        text_by_paper: dict[str, str] = {}
        if not bool(outline.get("manually_edited")):
            tags_by_paper, text_by_paper = self._outline_sources(
                principal, matrix["rows"]
            )
            parsed, auto_routing_adjustments = (
                self._auto_repair_generated_routing_sections(
                    parsed,
                    matrix["rows"],
                    text_by_paper,
                    outline_style=str(outline.get("outline_style") or ""),
                    taxonomy_profile=project.taxonomy_profile,
                )
            )
            parsed, evidence_realignments = self._realign_generated_body_sections(
                parsed,
                matrix_rows,
                text_by_paper,
                outline_style=str(outline.get("outline_style") or ""),
                taxonomy_profile=project.taxonomy_profile,
            )
            auto_routing_adjustments.extend(evidence_realignments)
            contextual_ids = self._contextual_outline_paper_ids(
                matrix_rows, tags_by_paper, text_by_paper
            )
            if contextual_ids:
                contextual_set = set(contextual_ids)
                introduction = next(
                    (
                        section
                        for section in parsed
                        if infer_section_role(
                            section.get("title"), section.get("section_role")
                        )
                        == "introduction"
                    ),
                    None,
                )
                if introduction is not None:
                    intro_context = introduction.setdefault("context_paper_ids", [])
                    intro_context.extend(
                        paper_id
                        for paper_id in contextual_ids
                        if paper_id not in intro_context
                    )
                for section in parsed:
                    role = infer_section_role(
                        section.get("title"), section.get("section_role")
                    )
                    if role != "body":
                        continue
                    before = list(section.get("paper_ids") or [])
                    removed = [
                        paper_id for paper_id in before if paper_id in contextual_set
                    ]
                    if not removed:
                        continue
                    section["paper_ids"] = [
                        paper_id for paper_id in before if paper_id not in contextual_set
                    ]
                    auto_routing_adjustments.append(
                        {
                            "source_section": str(section.get("title") or ""),
                            "target_section": "Introduction (context evidence)",
                            "paper_ids": removed,
                            "method": "contextual_source_detection",
                            "created_section": False,
                        }
                    )
            if auto_routing_adjustments:
                resolved_outline_md = _outline_markdown_from_sections(
                    parsed,
                    outline_style=str(outline.get("outline_style") or ""),
                    automatically_adjusted=True,
                )
        prepared = []
        for index, section in enumerate(parsed, start=1):
            role = infer_section_role(
                section.get("title"), section.get("section_role")
            )
            if role == "references":
                continue
            assigned = list(dict.fromkeys(section["paper_ids"]))
            if (
                not bool(outline.get("manually_edited"))
                and role == "body"
                and not assigned
            ):
                continue
            unknown = sorted(set(assigned) - matrix_ids)
            if unknown:
                raise WorkflowConflict(
                    "The selected outline refers to papers missing from the current Matrix.",
                    details={"paper_ids": unknown},
                )
            prepared.append(
                {
                    **section,
                    "section_id": f"S{len(prepared) + 1:02d}",
                    "section_role": role,
                    "paper_ids": assigned,
                    "context_paper_ids": list(
                        dict.fromkeys(section.get("context_paper_ids") or [])
                    ),
                }
            )

        normalized, primary_owner = assign_primary_paper_sections(
            prepared, matrix_order
        )
        body_primary_papers = list(
            dict.fromkeys(
                paper_id
                for section in normalized
                if section.get("section_role") == "body"
                for paper_id in section.get("primary_papers") or []
            )
        )
        rows_by_id = {
            str(row.get("paper_id") or ""): row for row in matrix_rows
        }
        index_summaries = (
            self.library_index.summaries(principal, matrix_order)
            if self.library_index is not None and self.library_index.enabled
            else {}
        )

        def evidence_readiness(
            role: str, primary_papers: list[str], context_papers: list[str]
        ) -> dict[str, Any]:
            if role in {"introduction", "conclusion"}:
                return {
                    "status": "synthesis",
                    "writeable_primary_papers": [],
                    "context_only_primary_papers": [],
                    "unresolved_primary_papers": [],
                    "context_papers": list(context_papers),
                }
            writeable: list[str] = []
            context_only: list[str] = []
            unresolved: list[str] = []
            for paper_id in primary_papers:
                row = rows_by_id.get(paper_id) or {}
                summary = index_summaries.get(paper_id) or {}
                has_fulltext = (
                    summary.get("fulltext") == "ready"
                    and int(summary.get("chunk_count") or 0) > 0
                )
                has_source_fact = any(
                    isinstance(fact, dict)
                    and str(fact.get("field_id") or "") != "abstract_summary"
                    and str(fact.get("value") or "").strip()
                    and bool(fact.get("evidence_refs"))
                    for fact in row.get("scientific_facts") or []
                )
                if has_fulltext or has_source_fact:
                    writeable.append(paper_id)
                elif self._matrix_abstract(row) or str(
                    (row.get("fact_enrichment") or {}).get("status") or ""
                ) == "limited":
                    context_only.append(paper_id)
                else:
                    unresolved.append(paper_id)
            status = (
                "ready"
                if primary_papers and len(writeable) == len(primary_papers)
                else "partial"
                if writeable or context_only
                else "insufficient"
            )
            return {
                "status": status,
                "assigned_primary_count": len(primary_papers),
                "writeable_primary_count": len(writeable),
                "context_only_primary_count": len(context_only),
                "unresolved_primary_count": len(unresolved),
                "writeable_primary_papers": writeable,
                "context_only_primary_papers": context_only,
                "unresolved_primary_papers": unresolved,
                "context_papers": list(context_papers),
            }

        sections: list[dict[str, Any]] = []
        for section in normalized:
            role = section["section_role"]
            primary = list(section["primary_papers"])
            supporting = list(section["supporting_papers"])
            context_papers = list(section.get("context_paper_ids") or [])
            if role == "conclusion":
                # A conclusion synthesizes the completed body arguments.  Give
                # it access to every body-owned paper so citations inherited
                # from those evidence-bound syntheses remain valid.
                supporting = list(body_primary_papers)
            if role == "introduction":
                thesis = (
                    str(section.get("purpose") or "").strip()
                    or "Define the review scope, organizing question, and evidence landscape "
                    "without repeating paper-level results from the body sections."
                )
                problem = "What problem, scope, and organizing logic does this review establish?"
                claim = (
                    "Frame the field and its evidence boundaries with brief representative "
                    "citations; reserve detailed study descriptions for their primary sections."
                )
                figure_need = "None unless an overview figure materially clarifies the review scope."
                target_words = 900
            elif role == "conclusion":
                thesis = (
                    str(section.get("purpose") or "").strip()
                    or "Synthesize cross-section findings, limitations, and future directions "
                    "without replaying individual paper summaries."
                )
                problem = "What conclusions hold across sections, and where do important limits remain?"
                claim = (
                    "Compare the body-section conclusions and cite prior evidence concisely; "
                    "do not restate full methods, conditions, or paper-by-paper results."
                )
                figure_need = "None unless a cross-section synthesis figure adds new comparative value."
                target_words = 900
            else:
                thesis = str(section.get("purpose") or "").strip() or f"Synthesize evidence for {section['title']}."
                problem = f"What does the current evidence establish about {section['title']}?"
                if primary:
                    claim = (
                        f"Develop claim-centered synthesis from {len(primary)} primary papers, "
                        "comparing convergent evidence, differences, and limitations."
                    )
                else:
                    claim = (
                        "Develop a cross-cutting comparison from previously introduced evidence "
                        "without repeating full study descriptions."
                    )
                figure_need = f"Support the comparison in {section['title']} where source evidence permits."
                target_words = max(700, 350 * max(1, len(primary)))
            sections.append(
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "section_role": role,
                    "section_thesis": thesis,
                    "review_problem": problem,
                    "major_papers": primary,
                    "primary_papers": primary,
                    "supporting_papers": supporting,
                    "context_papers": context_papers,
                    "review_claims": [{"claim": claim}],
                    "figure_or_table_needs": [
                        {
                            "type": "Figure or table",
                            "purpose": figure_need,
                            "candidate_papers": primary[:3],
                        }
                    ],
                    "avoid_patterns": [
                        "Do not infer unsupported conditions or mechanisms.",
                        "Do not repeat a paper-level description already owned by another section.",
                        "Do not organize prose as one title or one summary block per paper.",
                    ],
                    "section_transition": "Connect this evidence to the next comparison axis.",
                    "target_words": target_words,
                    "evidence_readiness": evidence_readiness(
                        role, primary, context_papers
                    ),
                }
            )
            sections[-1]["academic_contract"] = section_academic_contract(sections[-1])
            sections[-1]["synthesis_requirements"] = synthesis_requirements(
                sections[-1], taxonomy_profile=project.taxonomy_profile
            )
        if not sections:
            raise WorkflowValidationError("The selected outline contains no usable sections.")
        diagnostics = taxonomy_diagnostics(sections, matrix_order)
        contextual_paper_ids = list(
            dict.fromkeys(
                paper_id
                for section in sections
                for paper_id in section.get("context_papers") or []
            )
        )
        scope = derive_scope_contract(
            matrix.get("review_topic") or (discovery or {}).get("topic"),
            outline.get("outline_style"),
            matrix["rows"],
            current=outline.get("scope_contract")
            if isinstance(outline.get("scope_contract"), dict)
            else None,
        )
        scope_report = scope_diagnostics(scope)
        coverage_report = coverage_diagnostics(scope, matrix["rows"])
        basis = dict(outline.get("classification_basis") or classification_basis(outline.get("outline_style")))
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if matrix_state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        blueprint = {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "project_id": project_id,
            "review_topic": str(
                matrix.get("review_topic") or (discovery or {}).get("topic") or ""
            ),
            "outline_style": outline.get("outline_style"),
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "taxonomy_profile": project.taxonomy_profile,
            "taxonomy_diagnostics": diagnostics,
            "source_matrix_artifact_id": matrix_artifact.id,
            "source_outline_artifact_id": outline_artifact.id,
            "resolved_outline_md": resolved_outline_md,
            "auto_routing_adjustments": auto_routing_adjustments,
            "rule_pack": "general",
            "rule_pack_path": "references/rule_packs/general",
            "generated_at": utc_now().isoformat(),
            "paper_assignment_policy": {
                "mode": "single_primary_section_with_supporting_cross_references",
                "primary_section_by_paper": primary_owner,
                "introduction_and_conclusion_are_synthesis_only": True,
                "contextual_papers": contextual_paper_ids,
                "contextual_paper_policy": (
                    "Field-level reviews and perspectives may frame scope and history, "
                    "but do not substitute for primary body evidence."
                ),
            },
            "sections": sections,
            "synthesis_requirements": [
                {
                    "section_id": section["section_id"],
                    "components": section["synthesis_requirements"],
                }
                for section in sections
            ],
            "section_writing_plan_md": "# Section Writing Plan\n\n"
            + "\n".join(
                f"- {section['section_id']} {section['title']}: "
                f"{len(section['primary_papers'])} primary, "
                f"{len(section['supporting_papers'])} supporting papers."
                for section in sections
            )
            + "\n",
        }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="blueprint",
                files={BLUEPRINT_LOGICAL_NAME: (_json_bytes(blueprint), "json")},
                input_snapshot={
                    "matrix_artifact_id": matrix_artifact.id,
                    "outline_artifact_id": outline_artifact.id,
                },
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "blueprint",
                artifact_ids={BLUEPRINT_LOGICAL_NAME: published[BLUEPRINT_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
                approve_stages={"matrix": matrix_state.revision},
            )
        return {
            "project_id": project_id,
            "section_blueprint": blueprint,
            "blueprint_artifact_id": published[BLUEPRINT_LOGICAL_NAME].id,
            "blueprint_revision": state.revision,
            "matrix_revision": matrix_state.revision + 1,
        }

    def confirm_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        blueprint, _blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        _matrix, matrix_artifact = self._matrix(principal, project_id)
        _outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if (
            blueprint.get("source_matrix_artifact_id") != matrix_artifact.id
            or blueprint.get("source_outline_artifact_id") != outline_artifact.id
        ):
            raise WorkflowConflict(
                "Blueprint is out of date. Regenerate it from the current Matrix and outline."
            )
        diagnostics = blueprint.get("taxonomy_diagnostics")
        scope_report = blueprint.get("scope_diagnostics")
        blocking_issues = []
        if isinstance(scope_report, dict) and not scope_report.get("can_confirm", False):
            blocking_issues.extend(scope_report.get("issues") or [])
        if isinstance(diagnostics, dict) and not diagnostics.get("can_confirm", False):
            blocking_issues.extend(diagnostics.get("issues") or [])
        if blocking_issues:
            raise WorkflowConflict(
                "Blueprint cannot be confirmed until Scope and taxonomy blockers are resolved in the existing planning page.",
                details={"issues": blocking_issues},
            )
        state = self.repository.compare_and_set_stage(
            principal.user_id,
            project_id,
            "blueprint",
            int(revision),
            status="approved",
        )
        return {
            "project_id": project_id,
            "revision": state.revision,
            "status": state.status,
            "next_stage": "sections",
            "next_path": f"/sections?project={project_id}",
        }
