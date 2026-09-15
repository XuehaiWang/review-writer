"""PostgreSQL-native Matrix, outline, and Blueprint workflows."""

from __future__ import annotations

from review_writer_api.paper_labels import library_paper_labels

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
from review_writer_api.domain_services.base import OwnedProjectService
from review_writer_api.credentials import (
    ProviderKind,
    ProviderSettingsError,
    ProviderSettingsService,
)
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.domain_services.actions.planning.blueprint import (
    PlanningBlueprintActionsMixin,
)
from review_writer_api.domain_services.actions.planning.matrix import (
    PlanningMatrixActionsMixin,
)
from review_writer_api.domain_services.actions.planning.outline import (
    PlanningOutlineActionsMixin,
)
from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.job_service import job_payload as _planning_job_payload
from review_writer_api.model_catalog import resolve_model_tier
from review_writer_api.scientific_runner import (
    SENSITIVE_ENVIRONMENT_KEY,
    ScientificRunner,
)
from review_writer_api.workflow_models import LibraryPaper, WorkflowJob
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.taxonomy import (
    TaxonomyConfigurationError,
    effective_taxonomy_profile,
    load_taxonomy_rules,
)
from review_writer_core.metadata_tags import verified_structured_tags
from review_writer_core.bibliography_audit import bibliography_candidates
from review_writer_core.evidence_integrity import source_contains_excerpt
from review_writer_core.claim_contracts import (
    FACT_GROUNDED_BLUEPRINT_SCHEMA_VERSION,
    claim_is_executable,
)
from review_writer_core.scientific_facts import (
    FACT_PROMPT_VERSION, FACT_VALIDATION_VERSION, fact_is_usable, fact_support_spans,
    fact_usage, merge_facts, normalize_assertion_ceiling,
    fact_needs_verification, review_fingerprint,
)
from review_writer_core.academic_contracts import (
    ACADEMIC_SCHEMA_VERSION,
    classification_basis,
    coverage_diagnostics,
    derive_scope_contract,
    section_academic_contract,
    scope_diagnostics,
    synthesis_requirements,
    taxonomy_diagnostics,
    blueprint_taxonomy_diagnostics,
    evidence_key as academic_evidence_key,
)
from review_writer_core.evidence_queries import (
    COMPARISON_FIELD_IDS,
    build_question_query_plans,
    build_fact_query_plans,
    normalize_fact_request,
    registered_fact_field_ids,
    extraction_fact_field_ids,
    boolean_query,
)
from review_writer_core.review_fact_readiness import (
    fact_readiness_report,
    fact_processing_complete,
    fact_processing_state,
    required_fact_roles,
)
from review_writer_core.review_structure import (
    assign_primary_paper_sections,
    infer_section_role,
    publication_section_title,
    sanitize_internal_section_title,
)
from review_writer_core.classification_axes import (
    CLASSIFICATION_CONTRACT_VERSION,
    canonical_classification_contract,
    classification_contract_from_document,
)
from review_writer_core.section_narrative_contracts import (
    apply_single_paper_policy,
    derive_section_depth_contract,
)
from review_writer_core.stages.figures.overview_structure import derive_overview_structure_contract
from review_writer_core.stages.planning.blueprint import _blueprint_restructure_record
from review_writer_core.stages.planning.routing import routing_facts, confirmed_tags, current_classification_tags, verified_route
from review_writer_core.stages.planning.matrix import (
    refresh_matrix_fact_summary,
    _json_bytes,
    _matrix_publication_year,
    _paper_ids,
    _publication_year,
)
from review_writer_core.stages.planning.outline import (
    OUTLINE_STYLES,
    capitalize_outline_heading as _capitalize_outline_heading,
    outline_markdown_from_sections as _outline_markdown_from_sections,
    outline_sections as _outline_sections,
    sanitize_outline_markdown_headings as _sanitize_outline_markdown_headings,
)
from review_writer_core.stages.planning.topic import (
    TOPIC_AXIS_LABELS,
    TOPIC_GUIDED_STYLE,
    TOPIC_PARTITION_BOUNDARY_LABEL,
    _basis_with_axis_contract,
    _canonical_declared_partition,
    _clean_topic_partition,
    _matrix_classification_axes,
    _matrix_required_fact_roles,
    _required_topic_partitions_from_outline,
    _topic_outline_intent,
    _topic_partition_for_row,
    _topic_partition_for_text,
    _topic_partition_routes,
    _topic_partitions,
    _usable_fact_candidate,
)
from review_writer_core.stages.sections.rule_packs import (
    RulePackConfigurationError,
    resolve_rule_pack,
)
from review_writer_core.workflow.artifacts import (
    BLUEPRINT as BLUEPRINT_LOGICAL_NAME,
    DISCOVERY_REVIEW as DISCOVERY_LOGICAL_NAME,
    MATRIX as MATRIX_LOGICAL_NAME,
    PLANNING_OUTLINE as OUTLINE_LOGICAL_NAME,
    PLANNING_REFERENCE_OUTLINES as REFERENCE_INDEX_LOGICAL_NAME,
)


ROUTING_REQUIRED_LABEL = "Routing required — reassign these papers"
CROSS_CATEGORY_BOUNDARY_LABEL = "Cross-category evidence and boundary cases"
# Bump this whenever retrieval/query or source-validation semantics change.
# It is part of every per-paper fingerprint, so previously cached facts are
# re-extracted once under the new scientific contract.
MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION = 12
MATRIX_FACT_PROMPT_VERSION = FACT_PROMPT_VERSION


class PlanningService(
    PlanningBlueprintActionsMixin,
    PlanningMatrixActionsMixin,
    PlanningOutlineActionsMixin,
    OwnedProjectService,
):
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

    def _begin_gateway_job(
        self,
        principal: Principal,
        project_id: str,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
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
                    job_type=job_type,
                    status="running",
                    idempotency_scope_key=f"project:{project_id}",
                    idempotency_key=idempotency_key,
                    payload_json=payload,
                    started_at=now,
                )
            )
        return SimpleNamespace(
            job_id=str(job_id),
            user_id=principal.user_id,
            project_id=project_id,
            job_type=job_type,
        )

    def _finish_gateway_job(
        self,
        job_id: str,
        *,
        succeeded: bool,
        error_code: str,
        error_message: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        if self.model_gateway is None:
            return
        with database_session(self.model_gateway.session_factory) as session:
            row = session.get(WorkflowJob, uuid.UUID(job_id))
            if row is None:
                return
            row.status = "succeeded" if succeeded else "failed"
            row.error_code = "" if succeeded else error_code
            row.error_message = "" if succeeded else error_message[:2000]
            row.result_json = dict(result or {})
            row.finished_at = utc_now()

    def _begin_reference_gateway_job(
        self, principal: Principal, project_id: str, candidate_id: str
    ) -> SimpleNamespace:
        return self._begin_gateway_job(
            principal,
            project_id,
            job_type="planning.reference-analyze",
            idempotency_key=candidate_id,
            payload={"candidate_id": candidate_id},
        )

    def _finish_reference_gateway_job(
        self, job_id: str, *, succeeded: bool, error_message: str = ""
    ) -> None:
        self._finish_gateway_job(
            job_id,
            succeeded=succeeded,
            error_code="REFERENCE_ANALYSIS_FAILED",
            error_message=error_message,
        )

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
        run: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = run or self.repository.create_stage_run(
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
                metadata=metadata,
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

    def _with_current_bibliography(
        self,
        principal: Principal,
        matrix: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Overlay canonical Library bibliography while retaining Matrix facts.

        Bibliography verification may complete after the Matrix artifact was
        published.  Planning consumes the latest low-risk canonical fields and
        records their immutable metadata artifact IDs in the Blueprint, while
        scientific facts and user Matrix edits continue to come from Matrix.
        """

        updated = deepcopy(matrix)
        rows = [row for row in updated.get("rows") or [] if isinstance(row, dict)]
        paper_ids = [
            str(row.get("paper_id") or "")
            for row in rows
            if str(row.get("paper_id") or "")
        ]
        if not paper_ids:
            return updated, {}
        with database_session(self.repository.session_factory) as session:
            library_rows = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(paper_ids),
                    LibraryPaper.status == "active",
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
            audit_by_id = {
                row.paper_id: (
                    dict(row.bibliography_audit_row.audit_json or {})
                    if row.bibliography_audit_row is not None
                    else {}
                )
                for row in library_rows
            }
        by_id = {row.paper_id: row for row in library_rows}
        artifact_ids: dict[str, str] = {}
        for row in rows:
            paper_id = str(row.get("paper_id") or "")
            library_row = by_id.get(paper_id)
            if library_row is None:
                continue
            metadata = dict(library_row.metadata_json or {})
            for field in (
                "title",
                "authors",
                "year",
                "first_publication_date",
                "bibliographic_year",
                "publication_status",
                "journal",
                "doi",
            ):
                value = metadata.get(field)
                if isinstance(value, dict) and "value" in value:
                    value = value.get("value")
                if field in metadata:
                    row[field] = deepcopy(value)
            metadata_artifact_id = str(
                (metadata.get("_artifact_ids") or {}).get("metadata") or ""
            )
            if metadata_artifact_id:
                artifact_ids[paper_id] = metadata_artifact_id
            audit = audit_by_id.get(paper_id) or {}
            audit_status = str(audit.get("status") or "not_audited")
            manual_status = str(audit.get("manual_review_status") or "")
            resolution_complete = manual_status in {
                "approved",
                "resolved",
                "verified",
                "supporting_only",
            }
            row["bibliography_identity"] = {
                "status": audit_status,
                "verified": bool(audit_status == "verified" or resolution_complete),
                "manual_review_status": manual_status or "not_reviewed",
                "resolved_by": str(audit.get("resolved_by") or ""),
                "resolved_at": audit.get("resolved_at"),
                "unresolved_conflict_count": len(
                    audit.get("unresolved_conflicts") or []
                ),
                "missing_fields": list(
                    audit.get("automatic_resolution_missing_fields") or []
                ),
                "candidate_count": len(bibliography_candidates(audit)),
                "verification_method": str(
                    audit.get("verification_method") or ""
                ),
                "bibliography_role": str(
                    audit.get("bibliography_role") or "primary"
                ),
                "direct_claim_eligible": bool(
                    audit.get("direct_claim_eligible", True)
                ),
                "context_only": bool(audit.get("context_only", False)),
                "parent_paper_id": str(audit.get("parent_paper_id") or ""),
            }
        updated["bibliography_overlay"] = {
            "source_metadata_artifact_ids": artifact_ids,
            "applied_at": utc_now().isoformat(),
        }
        return updated, artifact_ids

    @staticmethod
    def _matrix_abstract(row: dict[str, Any]) -> str:
        abstract = row.get("abstract")
        if isinstance(abstract, dict):
            abstract = abstract.get("value")
        normalized = " ".join(str(abstract or "").split()).strip()
        return "" if "unavailable or unreliable" in normalized.casefold() else normalized

    def _user_fact_cache(
        self,
        principal: Principal,
        *,
        exclude_project_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Read reusable validated facts from this user's other current Matrices."""

        cached: dict[str, dict[str, Any]] = {}
        for artifact in self.repository.list_current_artifacts_for_user(
            principal.user_id,
            MATRIX_LOGICAL_NAME,
            exclude_project_id=exclude_project_id,
        ):
            try:
                resolved = self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact.id
                )
                payload = json.loads(resolved.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, WorkflowNotFound):
                continue
            if not isinstance(payload, dict):
                continue
            for row in payload.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                enrichment = dict(row.get("fact_enrichment") or {})
                cache_key = str(enrichment.get("fact_cache_key") or "")
                if not cache_key or int(enrichment.get("contract_version") or 0) != (
                    MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
                ):
                    continue
                facts = [
                    deepcopy(fact)
                    for fact in row.get("scientific_facts") or []
                    if isinstance(fact, dict)
                    and str(fact.get("fact_id") or "")
                    and str(fact.get("field_id") or "") != "topic_partition"
                    and fact.get("evidence_refs")
                ]
                if not facts:
                    continue
                cached.setdefault(
                    cache_key,
                    {
                        "facts": facts,
                        "source_project_id": artifact.project_id,
                        "source_matrix_artifact_id": artifact.id,
                    },
                )
        return cached

    def _prior_matrix_facts(
        self,
        principal: Principal,
        matrix: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Read the immediate predecessor's source-addressable fact cards.

        Matrix artifacts are immutable. A refresh must therefore carry a
        validated predecessor forward as a fallback before contacting the
        provider; otherwise a temporary 503 converts an already usable paper
        into a false extraction failure.
        """

        artifact_id = str(
            (matrix.get("fact_enrichment_summary") or {}).get(
                "source_matrix_artifact_id"
            )
            or ""
        ).strip()
        if not artifact_id:
            return {}
        try:
            resolved = self.artifacts.resolve_owned_artifact(
                principal.user_id,
                artifact_id,
            )
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, WorkflowNotFound):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(row.get("paper_id") or ""): [
                deepcopy(fact)
                for fact in row.get("scientific_facts") or []
                if isinstance(fact, dict)
                and str(fact.get("fact_id") or "")
                and str(fact.get("field_id") or "") != "topic_partition"
                and fact.get("evidence_refs")
            ]
            for row in payload.get("rows") or []
            if isinstance(row, dict) and str(row.get("paper_id") or "")
        }
    def matrix_enrichment_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        force: bool = False,
        selected_paper_ids: list[str] | None = None,
        matrix_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare source-addressable fact candidates for an asynchronous job."""

        principal.require(Permission.PROJECT_WRITE)
        project = self._owned_project(principal, project_id)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        if matrix_snapshot is not None:
            current_ids = set(_paper_ids(matrix.get("rows") or []))
            if set(_paper_ids(matrix_snapshot.get("rows") or [])) != current_ids:
                raise WorkflowConflict("Candidate Matrix must retain the selected paper set.")
            matrix = deepcopy(matrix_snapshot)
        from review_writer_core.stages.planning.fact_revision import pending_revisions
        if selected_paper_ids is None and any(pending_revisions(row) for row in matrix.get("rows") or []):
            return self.fact_revision_payload(principal, project_id, source_matrix_artifact_id=matrix_artifact.id)
        state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        if selected_paper_ids is not None:
            selected = set(selected_paper_ids)
            if not selected <= {str(row.get("paper_id")) for row in rows}:
                raise WorkflowValidationError("Fact repair requested papers outside the current Matrix.")
            rows = [row for row in rows if str(row.get("paper_id")) in selected]
        paper_ids = _paper_ids(rows)
        catalog = self._catalog(principal, paper_ids)
        supporting_parents = self._supporting_source_parents(catalog, paper_ids)
        source_ids = list(dict.fromkeys([*paper_ids, *supporting_parents]))
        if (
            self.library_index is not None
            and self.library_index.enabled
            and bool(getattr(self.library_index, "vector_enabled", False))
            and selected_paper_ids is None
        ):
            self.library_index.ensure_embeddings(principal, paper_ids)
        summaries = (
            self.library_index.summaries(principal, source_ids)
            if self.library_index is not None and self.library_index.enabled
            else {}
        )
        topic = str(matrix.get("review_topic") or "")
        topic_partitions = _topic_partitions(topic)
        classification_axes = _matrix_classification_axes(matrix, topic_partitions)
        classification_contract = canonical_classification_contract(
            classification_axes,
            primary_axis_hint=str(
                (matrix.get("classification_recommendation") or {}).get(
                    "primary_axis_id"
                )
                or (matrix.get("classification_contract") or {}).get(
                    "primary_axis_id"
                )
                or ""
            ),
            source="matrix_fact_extraction",
        )
        classification_axes = list(classification_contract["axes"])
        topic_required_roles = _matrix_required_fact_roles(
            topic,
            classification_axes,
        )
        routing_axis_id = str(
            classification_contract.get("primary_axis_id")
            or next(
                (
                    axis.get("axis_id")
                    for axis in classification_axes
                    if str(axis.get("axis_role") or "") == "primary_organization"
                ),
                "",
            )
        ).strip()
        routing_categories: list[dict[str, Any]] = []
        routing_category_labels: set[str] = set()

        def add_routing_category(label: Any, aliases: Any = ()) -> None:
            normalized_label = str(label or "").strip()
            identity = normalized_label.casefold()
            if not normalized_label or identity in routing_category_labels:
                return
            routing_category_labels.add(identity)
            routing_categories.append(
                {
                    "label": normalized_label[:160],
                    "aliases": list(
                        dict.fromkeys(
                            str(value).strip()[:160]
                            for value in aliases or []
                            if str(value or "").strip()
                        )
                    )[:16],
                }
            )

        primary_axis = next(
            (
                axis
                for axis in classification_axes
                if str(axis.get("axis_id") or "") == routing_axis_id
            ),
            {},
        )
        for partition in primary_axis.get("partitions") or []:
            if not isinstance(partition, dict):
                continue
            add_routing_category(
                partition.get("label"),
                [
                    *(partition.get("aliases") or []),
                    *(partition.get("positive_discriminators") or []),
                ],
            )
        if routing_axis_id:
            try:
                for label, category, aliases in load_taxonomy_rules(
                    self.root,
                    profile=project.taxonomy_profile,
                    topic_text=topic,
                ):
                    if str(category or "") == routing_axis_id:
                        add_routing_category(label, aliases)
            except TaxonomyConfigurationError:
                # Formal contract partitions above remain usable.  A missing
                # optional taxonomy profile must not break fact extraction.
                pass
        routing_categories_fingerprint = hashlib.sha256(
            json.dumps(
                routing_categories,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        deterministic_routing_by_paper: dict[str, str] = {}
        if routing_axis_id:
            routing_tags, routing_text = self._outline_sources(principal, rows)
            for row in rows:
                paper_id = str(row.get("paper_id") or "").strip()
                if not paper_id:
                    continue
                label = self._tag_value(
                    (routing_tags.get(paper_id) or {}).get(routing_axis_id)
                )
                if not label:
                    semantic = self._lexical_outline_candidates(
                        [row],
                        {paper_id: routing_text.get(paper_id, "")},
                        tag_key=routing_axis_id,
                        taxonomy_profile=effective_taxonomy_profile(
                            project.taxonomy_profile, topic
                        ),
                    )
                    label = next(
                        (
                            candidate_label
                            for candidate_label, assigned in semantic.items()
                            if candidate_label != ROUTING_REQUIRED_LABEL
                            and paper_id in assigned
                        ),
                        "",
                    )
                if label:
                    deterministic_routing_by_paper[paper_id] = label
        classification_partition_queries: list[dict[str, str]] = []
        seen_partition_queries: set[tuple[str, str]] = set()
        for axis in classification_axes:
            axis_id = str(axis.get("axis_id") or "")
            for partition in axis.get("partitions") or []:
                if not isinstance(partition, dict):
                    continue
                label = str(partition.get("label") or "").strip()
                if not label:
                    continue
                terms = list(dict.fromkeys(
                    str(value).strip()
                    for value in [
                        label,
                        *(partition.get("aliases") or []),
                        *(partition.get("positive_discriminators") or []),
                    ]
                    if str(value).strip()
                ))
                query = " ".join(terms[:5])[:700]
                identity = (axis_id, label.casefold())
                if not query or identity in seen_partition_queries:
                    continue
                seen_partition_queries.add(identity)
                classification_partition_queries.append(
                    {
                        "axis_id": axis_id,
                        "partition_id": str(partition.get("partition_id") or ""),
                        "label": label,
                        "query": query,
                    }
                )
        papers: list[dict[str, Any]] = []
        user_fact_cache = (
            {} if force else self._user_fact_cache(
                principal, exclude_project_id=project_id
            )
        )
        prior_facts_by_paper = self._prior_matrix_facts(principal, matrix)
        for row in rows:
            paper_id = str(row.get("paper_id") or "")
            summary = dict(summaries.get(paper_id) or {})
            lineage = str(summary.get("source_lineage_hash") or "")
            source_lineages = {source: str((summaries.get(source) or {}).get("source_lineage_hash") or "")
                               for source in [paper_id, *[key for key, parent in supporting_parents.items() if parent == paper_id]]}
            fingerprint_input = {
                "schema_version": 2,
                "fact_enrichment_contract_version": (
                    MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
                ),
                "topic": " ".join(topic.casefold().split()),
                "taxonomy_profile": project.taxonomy_profile,
                "paper_id": paper_id,
                "source_lineage_hash": lineage,
                "source_lineages": source_lineages,
                "chunker_version": summary.get("chunker_version"),
                "embedding_profile": summary.get("embedding_profile"),
                "embedding_model": (
                    summary.get("embedding_model")
                    if summary.get("semantic") == "ready"
                    else ""
                ),
                "embedding_dimension": (
                    summary.get("embedding_dimension")
                    if summary.get("semantic") == "ready"
                    else 0
                ),
                "prompt_schema_version": 1,
                "routing_adjudicator_version": 1,
                "routing_axis_id": routing_axis_id,
                "routing_categories_fingerprint": routing_categories_fingerprint,
                "deterministic_routing_label": deterministic_routing_by_paper.get(
                    paper_id, ""
                ),
                "actual_model_id": resolve_model_tier(project.model_tier, self.repository.session_factory).model,
            }
            if topic_partitions or classification_axes:
                fingerprint_input.update(
                    {
                        "topic_partition_classifier_version": 2,
                        "topic_partitions": topic_partitions,
                        "classification_contract_fingerprint": (
                            classification_contract["fingerprint"]
                        ),
                    }
                )
            source_fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            fact_cache_key = hashlib.sha256(
                json.dumps(
                    {
                        "contract_version": MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION,
                        "fact_schema_version": "scientific-fact/2",
                        "prompt_version": MATRIX_FACT_PROMPT_VERSION,
                        "source_lineage_hash": lineage,
                        "source_lineages": source_lineages,
                        "source_content_sha256": summary.get("content_sha256") or "",
                        "chunker_version": summary.get("chunker_version") or "",
                        "taxonomy_profile": project.taxonomy_profile,
                        "actual_model_id": resolve_model_tier(project.model_tier, self.repository.session_factory).model,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            existing = dict(row.get("fact_enrichment") or {})
            if (
                not force
                and (existing.get("last_attempt") or {}).get("status") != "failed"
                and existing.get("source_fingerprint") == source_fingerprint
                and fact_processing_complete(row.get("scientific_facts") or [], existing)
            ):
                continue
            plans = build_question_query_plans(
                review_topic=topic,
                heading="",
                core_argument=topic,
                section_role="body",
                required_fact_roles=topic_required_roles,
            )
            candidates: dict[str, dict[str, Any]] = {}
            partition_candidates: dict[str, dict[str, Any]] = {}
            strict_question_hit_count = 0
            relaxed_question_hit_count = 0
            if (
                self.library_index is not None
                and self.library_index.enabled
                and summary.get("fulltext") == "ready"
            ):
                def add_question_hits(
                    hits: list[Any],
                    *,
                    question_id: str,
                    retrieval_pass: str,
                ) -> int:
                    added = 0
                    for hit in hits:
                        if hit.is_neighbor or not _usable_fact_candidate(
                            hit.content_type,
                            hit.content,
                        ):
                            continue
                        added += 1
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
                                "retrieval_passes": [],
                            },
                        )
                        if question_id not in candidate["question_ids"]:
                            candidate["question_ids"].append(question_id)
                        if retrieval_pass not in candidate["retrieval_passes"]:
                            candidate["retrieval_passes"].append(retrieval_pass)
                    return added

                for plan in plans:
                    question_id = str(plan.get("question_id") or "")
                    if question_id == "section_focus":
                        continue
                    strict_hits = self.library_index.retrieve(
                        principal,
                        str(plan.get("websearch_query") or ""),
                        allowed_papers=[paper_id],
                        top_k=2,
                        per_paper_limit=2,
                        include_neighbors=False,
                        term_groups=list(plan.get("term_groups") or []),
                        exact_phrases=list(plan.get("exact_phrases") or []),
                    )
                    strict_added = add_question_hits(
                        strict_hits,
                        question_id=question_id,
                        retrieval_pass="strict_topic_and_question",
                    )
                    strict_question_hit_count += strict_added
                    # Matrix papers have already passed Topic admission. If a
                    # strict same-chunk Topic+question query returns nothing,
                    # search only inside that admitted paper for the scientific
                    # question. Source-addressable excerpt validation remains
                    # unchanged at publish time, so this restores recall without
                    # weakening factual acceptance.
                    question_groups = list(
                        plan.get("question_term_groups") or []
                    )
                    if not strict_added and question_groups:
                        relaxed_query = boolean_query(question_groups)
                        if relaxed_query:
                            relaxed_added = add_question_hits(
                                self.library_index.retrieve(
                                    principal,
                                    relaxed_query,
                                    allowed_papers=[paper_id],
                                    top_k=2,
                                    per_paper_limit=2,
                                    include_neighbors=False,
                                    term_groups=question_groups,
                                    exact_phrases=[
                                        str(term)
                                        for group in question_groups
                                        for term in group
                                        if " " in str(term)
                                    ],
                                ),
                                question_id=question_id,
                                retrieval_pass=(
                                    "admitted_paper_question_recovery"
                                ),
                            )
                            relaxed_question_hit_count += relaxed_added
                for partition_query in classification_partition_queries:
                    hits = self.library_index.retrieve(
                        principal,
                        partition_query["query"],
                        allowed_papers=[paper_id],
                        top_k=4,
                        per_paper_limit=4,
                        include_neighbors=True,
                    )
                    for hit in hits:
                        if not _usable_fact_candidate(
                            hit.content_type,
                            hit.content,
                        ):
                            continue
                        key = academic_evidence_key(
                            hit.paper_id, hit.chunk_id, hit.source_lineage_hash
                        )
                        partition_candidates.setdefault(
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
                                "matched_partitions": [],
                                "retrieval_passes": [],
                            },
                        )
                        matched = partition_candidates[key]["matched_partitions"]
                        partition_identity = {
                            "axis_id": partition_query["axis_id"],
                            "partition_id": partition_query["partition_id"],
                            "label": partition_query["label"],
                        }
                        if partition_identity not in matched:
                            matched.append(partition_identity)
                        retrieval_pass = (
                            "classification_neighbor_context"
                            if hit.is_neighbor
                            else "classification_discriminator_search"
                        )
                        passes = partition_candidates[key]["retrieval_passes"]
                        if retrieval_pass not in passes:
                            passes.append(retrieval_pass)
            coverage_seed_count = 0
            if (not candidates and summary.get("fulltext") == "ready"
                    and self.library_index is not None and callable(getattr(self.library_index, "primary_coverage_hits", None))):
                for hit in self.library_index.primary_coverage_hits(principal, allowed_papers=[paper_id], per_paper_limit=2):
                    if hit.paper_id != paper_id or hit.source_lineage_hash != lineage:
                        continue
                    key = academic_evidence_key(hit.paper_id, hit.chunk_id, hit.source_lineage_hash)
                    candidates[key] = {"evidence_key": key, "paper_id": paper_id, "chunk_id": hit.chunk_id,
                        "page_start": hit.page_start, "page_end": hit.page_end, "section_path": list(hit.section_path),
                        "content_type": hit.content_type, "content": hit.content, "source_lineage_hash": lineage,
                        "question_ids": list(topic_required_roles), "match_type": "coverage_seed",
                        "claim_eligible": False, "retrieval_passes": ["zero_hit_source_seed"]}
                    coverage_seed_count += 1
            abstract = self._matrix_abstract(row)
            if abstract:
                abstract_lineage = lineage or hashlib.sha256(
                    abstract.encode("utf-8")
                ).hexdigest()
                abstract_key = academic_evidence_key(
                    paper_id, "abstract", abstract_lineage
                )
                abstract_candidate = {
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
                }
                candidates.setdefault(
                    abstract_key,
                    abstract_candidate,
                )
                partition_candidates.setdefault(
                    abstract_key,
                    {
                        **abstract_candidate,
                        "matched_partitions": [],
                    },
                )
            current_facts = row.get("scientific_facts") or []
            candidates.update(self.fact_source_candidates(principal, row, current_facts, source_lineages))
            reusable_candidates = set(candidates) | set(partition_candidates)
            cross_project_cache = dict(user_fact_cache.get(fact_cache_key) or {})
            fallback_facts_by_id: dict[str, dict[str, Any]] = {}
            for fact in [
                *(
                    fact
                    for fact in row.get("scientific_facts") or []
                    if isinstance(fact, dict)
                    and str(fact.get("field_id") or "") != "topic_partition"
                ),
                *(prior_facts_by_paper.get(paper_id) or []),
                *(cross_project_cache.get("facts") or []),
            ]:
                if not isinstance(fact, dict):
                    continue
                fact_id = str(fact.get("fact_id") or "")
                refs = [
                    ref
                    for ref in fact.get("evidence_refs") or []
                    if isinstance(ref, dict)
                ]
                if (
                    not fact_id
                    or not refs
                    or any(
                        str(ref.get("evidence_key") or "")
                        not in reusable_candidates
                        for ref in refs
                    )
                ):
                    continue
                fallback_facts_by_id.setdefault(fact_id, deepcopy(fact))
            papers.append(
                {
                    "paper_id": paper_id,
                    "title": str(row.get("title") or paper_id),
                    "abstract": abstract,
                    "index_summary": summary,
                    "source_lineages": source_lineages,
                    "source_fingerprint": source_fingerprint,
                    "fact_cache_key": fact_cache_key,
                    "taxonomy_profile": project.taxonomy_profile,
                    "required_fact_roles": topic_required_roles,
                    "existing_fact_result": ({
                        **deepcopy(existing), "paper_id": paper_id, "facts": deepcopy(current_facts),
                        "paper_analysis": deepcopy(row.get("paper_analysis") or {}),
                        **{key: deepcopy(row.get(key)) for key in ("topic_partition_classification",
                            "evidence_backed_tags", "classification_outcomes", "routing_recommendation")},
                    } if existing.get("source_fingerprint") == source_fingerprint and current_facts else None),
                    "deterministic_routing_label": deterministic_routing_by_paper.get(
                        paper_id, ""
                    ),
                    "retrieval_summary": {
                        "coverage_seed_count": coverage_seed_count,
                        "strict_question_hit_count": strict_question_hit_count,
                        "relaxed_question_hit_count": relaxed_question_hit_count,
                        "classification_query_count": len(
                            classification_partition_queries
                        ),
                        "mode": (
                            "hybrid_targeted"
                            if summary.get("semantic") == "ready"
                            else "lexical_targeted"
                        ),
                    },
                    "evidence_candidates": list(candidates.values()),
                    "partition_evidence_candidates": list(
                        partition_candidates.values()
                    ),
                    "reused_fact_cache": {
                        **cross_project_cache,
                        "facts": list(fallback_facts_by_id.values()),
                        "source_project_id": (
                            project_id
                            if fallback_facts_by_id
                            else str(
                                cross_project_cache.get("source_project_id") or ""
                            )
                        ),
                        "source_matrix_artifact_id": (
                            matrix_artifact.id
                            if fallback_facts_by_id
                            else str(
                                cross_project_cache.get(
                                    "source_matrix_artifact_id"
                                )
                                or ""
                            )
                        ),
                    },
                }
            )
        return {
            "schema_version": 2,
            "fact_enrichment_contract_version": (
                MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
            ),
            "force_refresh": bool(force),
            "project_id": project_id,
            "review_topic": topic,
            "required_fact_roles": topic_required_roles,
            "topic_partitions": topic_partitions,
            "classification_axes": classification_axes,
            "classification_contract": classification_contract,
            "classification_contract_version": CLASSIFICATION_CONTRACT_VERSION,
            "routing_axis_id": routing_axis_id,
            "routing_categories": routing_categories,
            "routing_adjudicator_version": 1,
            "taxonomy_profile": project.taxonomy_profile,
            "actual_model_id": resolve_model_tier(project.model_tier, self.repository.session_factory).model,
            "source_matrix_artifact_id": matrix_artifact.id,
            "expected_matrix_revision": state.revision,
            "paper_count": len(rows),
            "pending_paper_count": len(papers),
            "fulltext_indexed_paper_count": sum((paper.get("index_summary") or {}).get("fulltext") == "ready" for paper in papers),
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
    def _validate_fact_sources(self, principal, papers):
        """Check both source versions and the owned article/SI relationship."""
        paper_ids = [str(paper["paper_id"]) for paper in papers]
        catalog = self._catalog(principal, paper_ids)
        parents = self._supporting_source_parents(catalog, paper_ids)
        summaries = self.library_index.summaries(principal, list(dict.fromkeys([*paper_ids, *parents])))
        for paper in papers:
            paper_id = str(paper["paper_id"])
            expected = paper.get("source_lineages") or {
                paper_id: str((paper.get("index_summary") or {}).get("source_lineage_hash") or "")}
            current = {source: str((summaries.get(source) or {}).get("source_lineage_hash") or "")
                       for source in [paper_id, *[key for key, parent in parents.items() if parent == paper_id]]}
            if paper_id not in catalog or current != expected:
                raise WorkflowConflict("The article or linked SI changed during fact extraction. The candidate was not published.")
        return summaries

    def retrieve_matrix_fact_evidence(
        self, principal: Principal, project_id: str, payload: dict[str, Any],
        request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Execute a bounded Agent query and register only server-owned evidence."""
        principal.require(Permission.PROJECT_WRITE)
        self._owned_project(principal, project_id)
        _matrix, current = self._matrix(principal, project_id)
        if str(current.id) != str(payload.get("source_matrix_artifact_id")):
            raise WorkflowConflict("Matrix changed during fact evidence retrieval.")
        paper_id = str(request.get("paper_id") or "")
        paper = next((row for row in payload.get("papers") or []
                      if str(row.get("paper_id")) == paper_id), None)
        if paper is None:
            raise WorkflowValidationError("Fact retrieval paper is outside this task.")
        if self.library_index is None or not self.library_index.enabled:
            return []
        self._validate_fact_sources(principal, [paper])
        expected = paper.get("source_lineages") or {
            paper_id: str((paper.get("index_summary") or {}).get("source_lineage_hash") or "")}
        allowed = [source for source, lineage in expected.items() if lineage]
        if not allowed:
            return []
        registry = {str(row.get("evidence_key")): row
                    for row in paper.get("evidence_candidates") or []}
        registered_fields = registered_fact_field_ids(
            required_roles=[
                *(paper.get("required_fact_roles") or []),
                *[
                    fact.get("field_id")
                    for fact in paper.get("repair_fact_candidates") or []
                    if isinstance(fact, dict)
                ],
            ],
            evidence_candidates=[
                *(paper.get("evidence_candidates") or []),
                *(paper.get("partition_evidence_candidates") or []),
            ],
        )
        found: dict[str, dict[str, Any]] = {}
        existing_keys = set(registry)
        for raw_question in (request.get("questions") or [])[:4]:
            question = normalize_fact_request(
                raw_question, allowed_field_ids=registered_fields
            )
            if question is None:
                continue
            role = question["field_id"]
            recovery_hits = []
            if question["source_recovery"]:
                pages_by_source: dict[str, set[int]] = {}
                for key in question["evidence_keys"]:
                    source = registry.get(key) or {}
                    source_id = str(source.get("source_file_id") or source.get("paper_id") or paper_id)
                    page = source.get("page_start")
                    if source_id in allowed and isinstance(page, int) and page > 0:
                        pages_by_source.setdefault(source_id, set()).add(page)
                recovery_pages_left = 3
                for source_id, pages in pages_by_source.items():
                    if recovery_pages_left <= 0:
                        break
                    requested_pages = sorted(pages)[:recovery_pages_left]
                    recovery_pages_left -= len(requested_pages)
                    try:
                        recovery_hits.extend(self.library_index.recover_pdf_pages(
                            principal, source_id, requested_pages, expected_lineage=expected[source_id]))
                        paper.setdefault("source_recovery_errors", {}).pop(source_id, None)
                    except (OSError, ValueError, ImportError, WorkflowNotFound) as exc:
                        # Preserve a specific diagnostic, then continue indexed
                        # recovery. Unreadable PDFs are not evidence of absence.
                        paper.setdefault("source_recovery_errors", {})[source_id] = str(exc)[:300]
            for pass_index, plan in enumerate(
                build_fact_query_plans(
                    question, allowed_field_ids=registered_fields
                )
            ):
                added = 0
                hits = recovery_hits if pass_index == 0 and recovery_hits else self.library_index.retrieve(
                    principal, plan["websearch_query"], allowed_papers=allowed, top_k=4,
                    per_paper_limit=4, include_neighbors=True,
                    term_groups=plan["term_groups"], exact_phrases=plan["exact_phrases"],
                    semantic_query=question["query"], use_semantic=pass_index == 0 and not recovery_hits,
                )
                for hit in hits:
                    if hit.paper_id not in allowed or hit.source_lineage_hash != expected[hit.paper_id] or not _usable_fact_candidate(hit.content_type, hit.content):
                        continue
                    key = academic_evidence_key(hit.paper_id, hit.chunk_id, hit.source_lineage_hash)
                    item = registry.setdefault(key, {
                        "evidence_key": key, "paper_id": paper_id,
                        "source_file_id": hit.paper_id,
                        "source_type": "main_article" if hit.paper_id == paper_id else "supporting_information",
                        "chunk_id": hit.chunk_id, "content": hit.content,
                        "content_type": hit.content_type, "page_start": hit.page_start,
                        "page_end": hit.page_end, "section_path": list(hit.section_path),
                        "source_lineage_hash": hit.source_lineage_hash,
                        "question_ids": [], "retrieval_passes": [],
                    })
                    item["question_ids"] = sorted(set(item.get("question_ids") or []) | {role})
                    item["retrieval_passes"] = sorted(set(item.get("retrieval_passes") or []) | {"fact_agent_targeted"})
                    added += key not in existing_keys
                    found[key] = item
                if added:
                    break
        # This payload lives only in the trusted Worker. The model receives a
        # copy; it cannot add arbitrary evidence to the publication registry.
        paper["evidence_candidates"] = list(registry.values())
        return list(found.values())

    def publish_matrix_enrichment(
        self,
        principal: Principal,
        project_id: str,
        payload: dict[str, Any],
        built: dict[str, Any],
        *,
        candidate_only: bool = False,
    ) -> dict[str, Any]:
        """Build or publish per-paper facts against one immutable Matrix.

        ``candidate_only`` is used by integrated chapter planning.  It applies
        the exact same source, relation and classification validation as the
        standalone Matrix job, but returns the enriched Matrix without moving
        any current artifact pointer.  The Matrix and Blueprint candidates are
        then published and confirmed together, so merely starting analysis
        cannot invalidate already approved downstream work.
        """

        principal.require(Permission.PROJECT_WRITE)
        if payload.get("operation") == "fact_revision":
            return self.publish_fact_revisions(principal, project_id, payload, built)
        matrix, matrix_artifact = self.validate_matrix_enrichment_inputs(principal, project_id, payload)
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
        declared_partitions = [
            _clean_topic_partition(value)
            for value in payload.get("topic_partitions") or []
            if _clean_topic_partition(value)
        ]
        routing_axis_id = str(payload.get("routing_axis_id") or "").strip()
        allowed_routing_labels = {
            str(item.get("label") or "").strip().casefold(): str(
                item.get("label") or ""
            ).strip()
            for item in payload.get("routing_categories") or []
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        }
        topic_required_roles = _matrix_required_fact_roles(
            updated.get("review_topic"),
            [
                axis
                for axis in payload.get("classification_axes") or []
                if isinstance(axis, dict)
            ],
        )
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
            partition_candidates = {
                str(item.get("evidence_key") or ""): item
                for item in [
                    *(source.get("partition_evidence_candidates") or []),
                    *(source.get("evidence_candidates") or []),
                ]
                if isinstance(item, dict) and item.get("evidence_key")
            }
            fact_candidates = {**partition_candidates, **candidates}
            facts = []
            for fact in result.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                raw_refs = fact_support_spans(fact, fact_candidates)
                if not raw_refs:
                    continue
                excerpt = " ".join(ref["support_excerpt"] for ref in raw_refs)
                if not excerpt or not str(fact.get("value") or "").strip():
                    continue
                if not str(fact.get("evidence_ceiling") or "").strip():
                    continue
                field_id = str(fact.get("field_id") or "").casefold()
                if field_id != "topic_partition" and field_id not in extraction_fact_field_ids(required_roles=topic_required_roles,
                        evidence_candidates=list(fact_candidates.values())):
                    continue
                assertion_ceiling = normalize_assertion_ceiling(
                    fact.get("assertion_ceiling")
                )
                facts.append(
                    {
                        **fact,
                        "paper_id": paper_id,
                        "support_level": "context_only" if fact.get("validation_contract") == FACT_VALIDATION_VERSION
                            and (fact.get("verification") or {}).get("status") != "supported" else fact.get("support_level"),
                        "assertion_ceiling": assertion_ceiling,
                        "evidence_refs": raw_refs,
                        "support_spans": raw_refs,
                        "support_excerpt": excerpt,
                    }
                )
                # Source registration may fill previously null metadata. Rebind
                # only when the shared checker proves the old audit still fits.
                if ((fact.get("verification") or {}).get("input_fingerprint")
                        and not fact_needs_verification(fact)
                        and not fact_needs_verification(facts[-1])):
                    facts[-1]["verification"] = {**fact["verification"],
                        "input_fingerprint": review_fingerprint(facts[-1])}
            raw_classification = result.get("topic_partition_classification")
            if not declared_partitions:
                partition_classification = {
                    "schema_version": 1,
                    "status": "not_requested",
                    "partition": "",
                    "confidence": 0.0,
                    "evidence_refs": [],
                }
            elif isinstance(raw_classification, dict):
                status_value = str(
                    raw_classification.get("status") or "insufficient_evidence"
                ).casefold()
                if status_value == "boundary":
                    status_value = "insufficient_evidence"
                canonical_partition = _canonical_declared_partition(
                    raw_classification.get("partition"), declared_partitions
                )
                try:
                    partition_confidence = max(
                        0.0,
                        min(
                            1.0,
                            float(raw_classification.get("confidence") or 0),
                        ),
                    )
                except (TypeError, ValueError):
                    partition_confidence = 0.0
                partition_refs = [
                    dict(ref)
                    for ref in raw_classification.get("evidence_refs") or []
                    if isinstance(ref, dict)
                    and str(ref.get("evidence_key") or "") in partition_candidates
                ]
                support_excerpt = " ".join(
                    str(raw_classification.get("support_excerpt") or "").split()
                )
                excerpt_valid = bool(
                    support_excerpt
                    and partition_refs
                    and all(
                        source_contains_excerpt(
                            partition_candidates[
                                str(ref.get("evidence_key") or "")
                            ].get("content"),
                            support_excerpt,
                        )
                        for ref in partition_refs
                    )
                )
                classified = bool(
                    status_value == "classified"
                    and canonical_partition
                    and partition_confidence >= 0.75
                    and excerpt_valid
                )
                unresolved_status = (
                    status_value
                    if status_value
                    in {"insufficient_evidence", "cross_category", "out_of_scope"}
                    else "insufficient_evidence"
                )
                if unresolved_status in {"cross_category", "out_of_scope"} and not excerpt_valid:
                    unresolved_status = "insufficient_evidence"
                partition_classification = {
                    "schema_version": 1,
                    "status": "classified" if classified else unresolved_status,
                    "partition": canonical_partition if classified else "",
                    "candidate_partition": (
                        canonical_partition
                        if canonical_partition and not classified
                        else ""
                    ),
                    "confidence": round(partition_confidence, 4),
                    "rationale": str(
                        raw_classification.get("rationale") or ""
                    )[:800],
                    "boundary_reason": (
                        ""
                        if classified
                        else str(
                            raw_classification.get("boundary_reason")
                            or "The source-bound classification did not meet the evidence and confidence requirements."
                        )[:800]
                    ),
                    "classification_reason": (
                        ""
                        if classified
                        else str(
                            raw_classification.get("classification_reason")
                            or raw_classification.get("boundary_reason")
                            or "The source-bound classification did not meet the evidence and confidence requirements."
                        )[:800]
                    ),
                    "support_excerpt": support_excerpt if excerpt_valid else "",
                    "evidence_ceiling": str(
                        raw_classification.get("evidence_ceiling")
                        or "Do not infer a contrasting partition from information absent in the cited passage."
                    )[:600],
                    "evidence_refs": partition_refs if excerpt_valid else [],
                    "review_status": (
                        "not_required" if classified else "needs_review"
                    ),
                    "extraction_method": "model_classified_from_bounded_source",
                }
            else:
                partition_classification = {
                    "schema_version": 1,
                    "status": "insufficient_evidence",
                    "partition": "",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "boundary_reason": "The model returned no valid Topic-partition classification.",
                    "review_status": "needs_review",
                    "extraction_method": "model_classification_missing",
                }
            axis_contract = {
                str(axis.get("axis_id") or ""): axis
                for axis in payload.get("classification_axes") or []
                if isinstance(axis, dict) and str(axis.get("axis_id") or "")
            }
            allowed_partitions = {
                axis_id: {
                    str(partition.get("partition_id") or "")
                    for partition in axis.get("partitions") or []
                    if isinstance(partition, dict)
                    and str(partition.get("partition_id") or "")
                }
                for axis_id, axis in axis_contract.items()
            }
            facts_by_id = {
                str(fact.get("fact_id") or ""): fact
                for fact in facts
                if str(fact.get("fact_id") or "")
            }
            evidence_backed_tags: dict[str, list[dict[str, Any]]] = {}
            raw_tags = result.get("evidence_backed_tags")
            if isinstance(raw_tags, dict):
                for axis_id, raw_values in raw_tags.items():
                    axis_id = str(axis_id or "")
                    if axis_id not in axis_contract or not isinstance(raw_values, list):
                        continue
                    for raw_tag in raw_values:
                        if not isinstance(raw_tag, dict):
                            continue
                        partition_id = str(raw_tag.get("partition_id") or "")
                        fact_ids = list(
                            dict.fromkeys(
                                str(value)
                                for value in raw_tag.get("fact_ids") or []
                                if str(value)
                            )
                        )
                        if (
                            partition_id not in allowed_partitions.get(axis_id, set())
                            or not fact_ids
                            or any(
                                fact_id not in facts_by_id
                                or fact_usage(facts_by_id[fact_id]) != "classification"
                                or str(facts_by_id[fact_id].get("field_id") or "")
                                != "topic_partition"
                                for fact_id in fact_ids
                            )
                        ):
                            continue
                        evidence_refs = [
                            dict(ref)
                            for ref in raw_tag.get("evidence_refs") or []
                            if isinstance(ref, dict)
                            and str(ref.get("evidence_key") or "") in partition_candidates
                        ]
                        if not evidence_refs:
                            continue
                        try:
                            tag_confidence = max(
                                0.0,
                                min(1.0, float(raw_tag.get("confidence") or 0)),
                            )
                        except (TypeError, ValueError):
                            tag_confidence = 0.0
                        evidence_backed_tags.setdefault(axis_id, []).append(
                            {
                                "axis_label": str(
                                    axis_contract[axis_id].get("label") or ""
                                )[:120],
                                "axis_role": str(
                                    axis_contract[axis_id].get("axis_role")
                                    or "comparison_dimension"
                                )[:80],
                                "partition_id": partition_id,
                                "partition_label": str(raw_tag.get("partition_label") or "")[:120],
                                "relation_to_paper": str(
                                    raw_tag.get("relation_to_paper") or "primary_contribution"
                                ),
                                "fact_ids": fact_ids,
                                "evidence_refs": evidence_refs,
                                "confidence": tag_confidence,
                                "assertion_ceiling": normalize_assertion_ceiling(
                                    raw_tag.get("assertion_ceiling")
                                ),
                            }
                        )
            classification_outcomes: list[dict[str, Any]] = []
            for raw_outcome in result.get("classification_outcomes") or []:
                if not isinstance(raw_outcome, dict):
                    continue
                axis_id = str(raw_outcome.get("axis_id") or "")
                if axis_id not in axis_contract or axis_id in evidence_backed_tags:
                    continue
                outcome_status = str(
                    raw_outcome.get("status") or "insufficient_evidence"
                ).casefold()
                if outcome_status not in {
                    "insufficient_evidence",
                    "cross_category",
                    "out_of_scope",
                }:
                    outcome_status = "insufficient_evidence"
                refs = [
                    dict(ref)
                    for ref in raw_outcome.get("evidence_refs") or []
                    if isinstance(ref, dict)
                    and str(ref.get("evidence_key") or "") in partition_candidates
                ]
                if outcome_status in {"cross_category", "out_of_scope"} and not refs:
                    outcome_status = "insufficient_evidence"
                classification_outcomes.append(
                    {
                        "axis_id": axis_id,
                        "axis_role": str(
                            axis_contract[axis_id].get("axis_role") or "comparison_dimension"
                        )[:80],
                        "status": outcome_status,
                        "reason": str(raw_outcome.get("reason") or "")[:800],
                        "support_excerpt": str(
                            raw_outcome.get("support_excerpt") or ""
                        )[:1600]
                        if refs
                        else "",
                        "evidence_refs": refs,
                        "resolution": str(
                            raw_outcome.get("resolution")
                            or "auto_route_from_positive_evidence_only"
                        )[:120],
                        "user_action_required": bool(
                            raw_outcome.get("user_action_required", False)
                        ),
                    }
                )
            raw_routing = result.get("routing_recommendation")
            if not isinstance(raw_routing, dict):
                raw_routing = {}
            requested_routing_label = str(raw_routing.get("label") or "").strip()
            routing_label = allowed_routing_labels.get(
                requested_routing_label.casefold(), ""
            )
            try:
                routing_confidence = max(
                    0.0, min(1.0, float(raw_routing.get("confidence") or 0))
                )
            except (TypeError, ValueError):
                routing_confidence = 0.0
            routing_refs = [
                dict(ref)
                for ref in raw_routing.get("evidence_refs") or []
                if isinstance(ref, dict)
                and str(ref.get("evidence_key") or "") in fact_candidates
            ]
            routing_excerpt = " ".join(
                str(raw_routing.get("support_excerpt") or "").split()
            )
            routing_excerpt_valid = bool(
                routing_excerpt
                and routing_refs
                and all(
                    source_contains_excerpt(
                        fact_candidates[
                            str(ref.get("evidence_key") or "")
                        ].get("content"),
                        routing_excerpt,
                    )
                    for ref in routing_refs
                )
            )
            routing_classified = bool(
                routing_axis_id
                and str(raw_routing.get("axis_id") or "") == routing_axis_id
                and str(raw_routing.get("status") or "").casefold()
                == "classified"
                and routing_label
                and routing_excerpt_valid
                and fact_usage(facts_by_id.get(str(raw_routing.get("verification_fact_id") or ""), {})) == "classification"
            )
            formal_route_available = bool(
                routing_axis_id in evidence_backed_tags
                or str(raw_routing.get("status") or "").casefold()
                == "formal_axis_route_available"
            )
            deterministic_route_available = bool(
                str(raw_routing.get("status") or "").casefold()
                == "deterministic_route_available"
                and routing_label
            )
            routing_recommendation = {
                "verification_fact_id": raw_routing.get("verification_fact_id") if routing_classified else None,
                "verification": dict((facts_by_id.get(str(raw_routing.get("verification_fact_id") or ""), {}).get("verification") or {})),
                "schema_version": 1,
                "axis_id": routing_axis_id,
                "status": (
                    "classified"
                    if routing_classified
                    else "deterministic_route_available"
                    if deterministic_route_available
                    else "formal_axis_route_available"
                    if formal_route_available
                    else "insufficient_evidence"
                ),
                "label": (
                    routing_label
                    if routing_classified or deterministic_route_available
                    else ""
                ),
                "candidate_label": (
                    routing_label
                    if routing_label and not routing_classified
                    else ""
                ),
                "confidence": round(routing_confidence, 4),
                "rationale": str(raw_routing.get("rationale") or "")[:800],
                "reason": (
                    ""
                    if routing_classified
                    or deterministic_route_available
                    or formal_route_available
                    else str(
                        raw_routing.get("reason")
                        or "The bounded routing adjudicator found no supported publication category."
                    )[:800]
                ),
                "support_excerpt": (
                    routing_excerpt if routing_classified else ""
                ),
                "evidence_ceiling": str(
                    raw_routing.get("evidence_ceiling")
                    or "Do not extend this routing decision beyond the cited study design."
                )[:600],
                "evidence_refs": routing_refs if routing_classified else [],
                "review_status": (
                    "not_required"
                    if routing_classified
                    or deterministic_route_available
                    or formal_route_available
                    else "auto_unresolved"
                ),
                "extraction_method": str(
                    raw_routing.get("extraction_method")
                    or "model_routing_not_completed"
                )[:120],
            }
            status = str(result.get("status") or "failed")
            if result.get("facts") and len(facts) < len(result.get("facts") or []):
                status = "partial" if facts else "failed"
            previous_enrichment = row.get("fact_enrichment") or {}
            if (
                status == "failed" and not facts
                and source.get("source_fingerprint")
                and previous_enrichment.get("source_fingerprint") == source.get("source_fingerprint")
                and any(fact_is_usable(fact) for fact in row.get("scientific_facts") or [])
            ):
                row["fact_enrichment"] = {
                    **previous_enrichment,
                    "last_attempt": {"status": "failed", "error": str(result.get("error") or "")[:1000],
                                     "updated_at": utc_now().isoformat()},
                }
                continue
            row["scientific_facts"] = merge_facts(facts)
            row["topic_partition_classification"] = partition_classification
            row["evidence_backed_tags"] = evidence_backed_tags
            row["classification_outcomes"] = classification_outcomes
            row["routing_recommendation"] = routing_recommendation
            facts = row["scientific_facts"]
            row["comparison_evidence"] = {
                field_id: [
                    dict(fact)
                    for fact in facts
                    if str(fact.get("field_id") or "") == field_id
                ]
                for field_id in COMPARISON_FIELD_IDS
            }
            review_status = str(result.get("review_status") or "needs_review")
            if review_status not in {
                "not_required",
                "auto_resolved",
                "needs_review",
                "human_checked",
            }:
                review_status = "needs_review"
            readiness = fact_readiness_report(
                facts=facts,
                required_roles=topic_required_roles,
                extraction_status=status,
                failed_fields=result.get("failed_fields") or [],
                baseline=True, topic=str(matrix.get("review_topic") or ""),
            )
            row["fact_enrichment"] = {
                "schema_version": 2,
                "contract_version": int(
                    payload.get("fact_enrichment_contract_version")
                    or MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
                ),
                "status": status,
                "extraction_status": status,
                **readiness,
                "review_status": review_status,
                "source_fingerprint": str(source.get("source_fingerprint") or ""),
                "fact_cache_key": str(source.get("fact_cache_key") or ""),
                "source_lineage_hash": str(
                    (source.get("index_summary") or {}).get("source_lineage_hash") or ""
                ),
                "source_lineages": deepcopy(source.get("source_lineages") or {}),
                "fact_count": len(facts),
                "failed_fields": list(result.get("failed_fields") or []),
                "failed_field_details": deepcopy(
                    result.get("failed_field_details") or []
                ),
                "normalization_rejections": deepcopy(result.get("normalization_rejections") or []),
                "error": str(result.get("error") or "")[:1000],
                "automatic_resolution": deepcopy(
                    result.get("automatic_resolution") or {}
                ),
                "fact_extraction_profile": deepcopy(
                    result.get("fact_extraction_profile") or {}
                ),
                "updated_at": utc_now().isoformat(),
            }
            row["fact_enrichment"].update(fact_processing_state(facts, row["fact_enrichment"]))
            analysis = deepcopy(result.get("paper_analysis") or row.get("paper_analysis") or {})
            analysis["fact_ids"] = [f["fact_id"] for f in facts if fact_is_usable(f)
                                    and f.get("fact_id") in (analysis.get("fact_ids") or [])]
            row["paper_analysis"] = analysis if analysis["fact_ids"] else {}
        classification_axes = [
            deepcopy(axis)
            for axis in payload.get("classification_axes") or updated.get("classification_axes") or []
            if isinstance(axis, dict) and str(axis.get("axis_id") or "")
        ]
        axis_coverage: dict[str, dict[str, Any]] = {}
        for axis in classification_axes:
            axis_id = str(axis.get("axis_id") or "")
            partition_counts: dict[str, int] = {}
            paper_ids: set[str] = set()
            for matrix_row in updated.get("rows") or []:
                if not isinstance(matrix_row, dict):
                    continue
                values = (matrix_row.get("evidence_backed_tags") or {}).get(axis_id) or []
                if not values:
                    continue
                paper_id = str(matrix_row.get("paper_id") or "")
                if paper_id:
                    paper_ids.add(paper_id)
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    partition_id = str(value.get("partition_id") or "")
                    if partition_id:
                        partition_counts[partition_id] = partition_counts.get(partition_id, 0) + 1
            axis_coverage[axis_id] = {
                "paper_ids": sorted(paper_ids),
                "paper_count": len(paper_ids),
                "partition_counts": partition_counts,
            }
            axis["evidence_coverage"] = deepcopy(axis_coverage[axis_id])
            if paper_ids:
                axis["role_status"] = "evidence_confirmed"
            elif str(axis.get("source_type") or "") != "explicit_topic":
                axis["role_status"] = "provisional"

        explicit_primary = next(
            (
                axis
                for axis in classification_axes
                if str(axis.get("source_type") or "") == "explicit_topic"
                and str(axis.get("axis_role") or "") == "primary_organization"
            ),
            None,
        )
        recommended_primary = explicit_primary
        if recommended_primary is None and classification_axes:
            recommended_primary = max(
                classification_axes,
                key=lambda axis: (
                    int(
                        (axis_coverage.get(str(axis.get("axis_id") or "")) or {}).get(
                            "paper_count"
                        )
                        or 0
                    ),
                    str(axis.get("axis_role") or "") == "primary_organization",
                ),
            )
            for axis in classification_axes:
                if axis is recommended_primary:
                    axis["axis_role"] = "primary_organization"
                    axis["heading_requirement"] = "primary_heading"
                elif str(axis.get("axis_role") or "") == "primary_organization":
                    axis["axis_role"] = "comparison_dimension"
                    axis["heading_requirement"] = "comparison_only"
        updated["classification_axes"] = classification_axes
        updated["classification_recommendation"] = {
            "schema_version": 1,
            "source": (
                "explicit_topic_with_matrix_evidence"
                if explicit_primary is not None
                else "system_recommended_from_selected_matrix_evidence"
            ),
            "primary_axis_id": str(
                (recommended_primary or {}).get("axis_id") or ""
            ),
            "primary_axis_label": str(
                (recommended_primary or {}).get("label") or ""
            ),
            "requires_existing_blueprint_confirmation": True,
            "axis_coverage": axis_coverage,
            "updated_at": utc_now().isoformat(),
        }
        updated_classification_contract = canonical_classification_contract(
            classification_axes,
            primary_axis_hint=str(
                (recommended_primary or {}).get("axis_id") or ""
            ),
            source=(
                "explicit_topic_with_matrix_evidence"
                if explicit_primary is not None
                else "matrix_evidence_recommendation"
            ),
        )
        updated["classification_contract"] = updated_classification_contract
        updated["classification_contract_version"] = (
            CLASSIFICATION_CONTRACT_VERSION
        )
        updated["fact_enrichment_summary"] = {
            **dict(updated.get("fact_enrichment_summary") or {}), "schema_version": 2,
            "contract_version": int(payload.get("fact_enrichment_contract_version") or MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION),
            "source_matrix_artifact_id": matrix_artifact.id, "updated_at": utc_now().isoformat(),
        }
        refresh_matrix_fact_summary(updated)
        updated["comparison_schema"] = {
            "schema_version": 1,
            "field_ids": list(COMPARISON_FIELD_IDS),
            "missing_value_policy": "keep_empty_and_do_not_infer",
            "source": "source_addressable_scientific_facts",
        }
        outline_compatible_ids = [
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        ]
        if matrix_artifact.id not in outline_compatible_ids:
            outline_compatible_ids.append(matrix_artifact.id)
        updated["outline_compatible_matrix_artifact_ids"] = outline_compatible_ids[-20:]

        def evidence_state_by_paper(document: dict[str, Any]) -> dict[str, Any]:
            return {
                str(row.get("paper_id") or ""): {
                    "scientific_facts": row.get("scientific_facts") or [],
                    "topic_partition_classification": row.get(
                        "topic_partition_classification"
                    )
                    or {},
                    "evidence_backed_tags": row.get("evidence_backed_tags") or {},
                    "classification_outcomes": row.get("classification_outcomes") or [],
                    "routing_recommendation": row.get("routing_recommendation") or {},
                    "fact_status": str(
                        (row.get("fact_enrichment") or {}).get("status") or "pending"
                    ),
                }
                for row in document.get("rows") or []
                if isinstance(row, dict) and str(row.get("paper_id") or "")
            }

        previous_evidence_state = evidence_state_by_paper(matrix)
        next_evidence_state = evidence_state_by_paper(updated)
        changed_paper_ids = sorted(
            paper_id
            for paper_id in set(previous_evidence_state) | set(next_evidence_state)
            if previous_evidence_state.get(paper_id) != next_evidence_state.get(paper_id)
        )
        def stable_classification_contract(document: dict[str, Any]) -> dict[str, Any]:
            contract = classification_contract_from_document(
                document,
                primary_axis_hint=str(
                    (document.get("classification_recommendation") or {}).get(
                        "primary_axis_id"
                    )
                    or ""
                ),
                source="matrix_contract_comparison",
            )
            return {
                "contract_version": contract["contract_version"],
                "fingerprint": contract["fingerprint"],
            }

        classification_contract_changed = bool(
            stable_classification_contract(matrix)
            != stable_classification_contract(updated)
        )

        def enrichment_cache_state(document: dict[str, Any]) -> dict[str, Any]:
            return {
                str(row.get("paper_id") or ""): {
                    "last_attempt": (row.get("fact_enrichment") or {}).get("last_attempt"),
                    "source_fingerprint": str(
                        (row.get("fact_enrichment") or {}).get("source_fingerprint") or ""
                    ),
                    "source_lineage_hash": str(
                        (row.get("fact_enrichment") or {}).get("source_lineage_hash") or ""
                    ),
                    "failed_fields": list(
                        (row.get("fact_enrichment") or {}).get("failed_fields") or []
                    ),
                }
                for row in document.get("rows") or []
                if isinstance(row, dict) and str(row.get("paper_id") or "")
            }

        previous_cache_state = enrichment_cache_state(matrix)
        next_cache_state = enrichment_cache_state(updated)
        refreshed_paper_ids = sorted(
            paper_id
            for paper_id in set(previous_cache_state) | set(next_cache_state)
            if previous_cache_state.get(paper_id) != next_cache_state.get(paper_id)
        )
        updated["matrix_change_set"] = {
            "schema_version": 1,
            "operation": "scientific_fact_refresh",
            "changed_paper_ids": changed_paper_ids,
            "changed_paper_count": len(changed_paper_ids),
            "classification_contract_changed": classification_contract_changed,
            "refreshed_paper_ids": refreshed_paper_ids,
            "dependency_policy": "invalidate_only_when_evidence_state_changed",
            "updated_at": utc_now().isoformat(),
        }
        if (
            not changed_paper_ids
            and not refreshed_paper_ids
            and not classification_contract_changed
        ):
            current_state = self.repository.get_stage_state(
                principal.user_id, project_id, "matrix"
            )
            return {
                "project_id": project_id,
                "matrix_artifact_id": matrix_artifact.id,
                "matrix_revision": current_state.revision if current_state else 0,
                "fact_enrichment_summary": matrix.get("fact_enrichment_summary")
                or updated["fact_enrichment_summary"],
                "changed_paper_ids": [],
                "refreshed_paper_ids": [],
                "classification_contract_changed": False,
                "unchanged": True,
                **(
                    {"matrix_snapshot": deepcopy(matrix)}
                    if candidate_only
                    else {}
                ),
            }
        if candidate_only:
            return {
                "project_id": project_id,
                "source_matrix_artifact_id": matrix_artifact.id,
                "fact_enrichment_summary": updated["fact_enrichment_summary"],
                "changed_paper_ids": changed_paper_ids,
                "refreshed_paper_ids": refreshed_paper_ids,
                "classification_contract_changed": classification_contract_changed,
                "unchanged": False,
                "matrix_snapshot": updated,
            }
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
                            (
                                "blueprint",
                                "sections",
                                "figure-review",
                                "figures",
                                "draft",
                                "final",
                            )
                            if changed_paper_ids or classification_contract_changed
                            else ()
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
            "blueprint_invalidated": bool(changed_paper_ids or classification_contract_changed),
            "changed_paper_ids": changed_paper_ids,
            "refreshed_paper_ids": refreshed_paper_ids,
            "classification_contract_changed": classification_contract_changed,
            "unchanged": False,
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
            # Organization precedence is separate from Claim eligibility:
            # verified Library metadata < formal Matrix fact classifications
            # < explicit human project tags. Stage 02 retrieval hints and
            # legacy automatic screening tags never organize the outline.
            project_tags = row.get("project_tags")
            formal_tags = current_classification_tags(row)
            if isinstance(formal_tags, dict):
                for axis_id, values in formal_tags.items():
                    labels = [
                        str(value.get("partition_label") or "").strip()
                        for value in values or []
                        if isinstance(value, dict)
                        and str(value.get("partition_label") or "").strip()
                    ]
                    if labels:
                        tags[str(axis_id)] = labels
                        axis_label = next(
                            (
                                str(value.get("axis_label") or "").strip()
                                for value in values or []
                                if isinstance(value, dict)
                                and str(value.get("axis_label") or "").strip()
                            ),
                            "",
                        )
                        if axis_label:
                            tags[axis_label] = labels
            routing = verified_route(row)
            if isinstance(routing, dict):
                routing_axis = str(routing.get("axis_id") or "").strip()
                routing_label = str(routing.get("label") or "").strip()
                if (
                    routing_axis
                    and routing_label
                    and str(routing.get("status") or "") == "classified"
                    and routing_axis not in tags
                    and bool(routing.get("evidence_refs"))
                ):
                    # This bounded Agent recommendation is a routing aid only.
                    # It does not become Claim evidence or replace a stronger
                    # formal axis assignment.
                    tags[routing_axis] = [routing_label]
            human_tags = row.get("human_confirmed_tags")
            if isinstance(human_tags, dict) and human_tags:
                tags.update(human_tags)
            elif row.get("project_tag_review_status") == "confirmed" and isinstance(
                project_tags, dict
            ):
                tags.update(project_tags)
            tags_by_paper[record.paper_id] = tags
            scientific_facts = routing_facts(row)
            # Route from the paper's extracted scientific object before title
            # words.  Product names in titles are otherwise easily mistaken
            # for the substrate/precursor used by the study.
            fact_priority = {
                "object_input": 0,
                "document_scope": 1,
                "transformation": 2,
                "method_family": 3,
            }
            fact_parts: list[str] = []
            for item in sorted(
                scientific_facts,
                key=lambda item: fact_priority.get(
                    str(item.get("field_id") or ""), 10
                ),
            ):
                # A normalized fact may intentionally omit the reagent or
                # substrate phrase that disambiguates the paper's primary
                # analytical route.  Include only the bounded supporting
                # excerpt (not the full source page) so routing can use that
                # explicit evidence without admitting unrelated-work text.
                for value in (
                    item.get("value"),
                    item.get("normalized_value"),
                    item.get("support_excerpt"),
                ):
                    text = str(value or "").strip()
                    if text and text not in fact_parts:
                        fact_parts.append(text)
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
    def _lexical_outline_candidates(
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        try:
            rules = [
                (label, aliases)
                for label, category, aliases in load_taxonomy_rules(
                    Path.cwd(),
                    profile=taxonomy_profile,
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
        semantic = self._lexical_outline_candidates(
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
            # Keep unresolved classification as workflow state. It must not be
            # converted into a reader-facing catch-all or "boundary" chapter.
            repaired[ROUTING_REQUIRED_LABEL] = still_unresolved
        return repaired or groups

    def _auto_repair_generated_routing_sections(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
        tag_key_override: str = "",
        axis_label_override: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve system-created routing placeholders before Blueprint build.

        User-authored catch-all sections remain subject to the normal academic
        gate.  Only the exact placeholder emitted by older built-in outline
        versions is repaired automatically. Taxonomy matches are merged into
        an existing same-title section when possible; otherwise a defensible
        category is inserted before the conclusion. Truly unresolved primary
        studies remain explicit Matrix routing state instead of being relabeled
        as Introduction context or exposed as a reader-facing ``Other`` section.
        """

        style = str(outline_style or "").casefold()
        definition = OUTLINE_STYLES.get(style)
        if definition is None:
            return sections, []
        routing_tag_key = str(tag_key_override or definition["tag_key"])
        routing_axis_label = str(axis_label_override or definition["axis"])

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
        semantic = self._lexical_outline_candidates(
            unresolved_rows,
            text_by_paper,
            tag_key=routing_tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        grouped: dict[str, list[str]] = {
            label: list(dict.fromkeys(paper_ids))
            for label, paper_ids in semantic.items()
            if label != ROUTING_REQUIRED_LABEL and paper_ids
        }
        routed = {paper_id for paper_ids in grouped.values() for paper_id in paper_ids}
        still_unresolved = [paper_id for paper_id in unresolved_ids if paper_id not in routed]

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
        if still_unresolved:
            grouped[CROSS_CATEGORY_BOUNDARY_LABEL] = list(still_unresolved)
        for label, paper_ids in grouped.items():
            public_label = publication_section_title("", label)
            target = existing_by_title.get(public_label.casefold())
            created = target is None
            if target is None:
                target = {
                    "title": public_label,
                    "paper_ids": [],
                    "context_paper_ids": [],
                    "section_role": "body",
                    "purpose": (
                        f"compare the selected papers within this {routing_axis_label} "
                        "category and state its evidence boundaries."
                    ),
                    "notes": "Automatically routed from a system-generated placeholder.",
                }
                if label == CROSS_CATEGORY_BOUNDARY_LABEL:
                    target["boundary_rationale"] = (
                        "The available source evidence does not support assigning these papers "
                        "to one primary-axis category; retain them for explicit cross-category "
                        "comparison until a narrower evidence-backed route is available."
                    )
                repaired.insert(insert_at, target)
                insert_at += 1
                existing_by_title[public_label.casefold()] = target
            bucket = target.setdefault("paper_ids", [])
            bucket.extend(paper_id for paper_id in paper_ids if paper_id not in bucket)
            adjustments.append(
                {
                    "source_section": ", ".join(source_titles),
                    "target_section": public_label,
                    "paper_ids": list(paper_ids),
                    "method": (
                        "unresolved_classification_retained"
                        if label == CROSS_CATEGORY_BOUNDARY_LABEL
                        else "taxonomy_evidence_match"
                    ),
                    "created_section": created,
                }
            )
        return repaired, adjustments

    def _reconcile_generated_outline_coverage(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
        tag_key_override: str = "",
        axis_label_override: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Give every selected paper a visible, evidence-bounded disposition.

        Generated outlines used to drop the internal ``Routing required``
        group before saving.  That made a minority or cross-category paper an
        orphan even though it remained selected in the Matrix.  This pass is
        deliberately topic-agnostic: it first retries the configured primary
        axis from bounded fact excerpts, then keeps genuinely unresolved
        primary studies in an explicit boundary-analysis section.  Reviews,
        perspectives, and source-confirmed out-of-scope papers remain context
        evidence rather than being forced into a scientific category.
        """

        style = str(outline_style or "").casefold()
        definition = OUTLINE_STYLES.get(style)
        if definition is None:
            return deepcopy(sections), []
        routing_tag_key = str(tag_key_override or definition["tag_key"])
        routing_axis_label = str(axis_label_override or definition["axis"])
        repaired = deepcopy(sections)
        matrix_order = list(
            dict.fromkeys(
                str(row.get("paper_id") or "").strip()
                for row in rows
                if str(row.get("paper_id") or "").strip()
            )
        )
        disposed = {
            str(paper_id)
            for section in repaired
            for paper_id in [
                *(section.get("paper_ids") or []),
                *(section.get("context_paper_ids") or []),
                *(
                    exclusion.get("paper_id")
                    for exclusion in section.get("excluded_papers") or []
                    if isinstance(exclusion, dict)
                    and str(exclusion.get("reason") or "").strip()
                ),
            ]
            if str(paper_id or "").strip()
        }
        missing_ids = [paper_id for paper_id in matrix_order if paper_id not in disposed]
        if not missing_ids:
            return repaired, []

        missing_set = set(missing_ids)
        missing_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() in missing_set
        ]
        contextual_ids = self._contextual_outline_paper_ids(
            missing_rows,
            tags_by_paper,
            text_by_paper,
        )
        contextual_set = set(contextual_ids)
        introduction = next(
            (
                section
                for section in repaired
                if infer_section_role(
                    section.get("title"), section.get("section_role")
                )
                == "introduction"
            ),
            None,
        )
        adjustments: list[dict[str, Any]] = []
        if introduction is not None and contextual_ids:
            target_context = introduction.setdefault("context_paper_ids", [])
            target_context.extend(
                paper_id for paper_id in contextual_ids if paper_id not in target_context
            )
            adjustments.append(
                {
                    "source_section": "Unassigned selected papers",
                    "target_section": str(introduction.get("title") or "Introduction"),
                    "paper_ids": contextual_ids,
                    "method": "contextual_source_disposition",
                    "created_section": False,
                }
            )

        analytical_rows = [
            row
            for row in missing_rows
            if str(row.get("paper_id") or "").strip() not in contextual_set
        ]
        groups = self._outline_groups(
            analytical_rows,
            tags_by_paper,
            text_by_paper,
            tag_key=routing_tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        unresolved_ids = list(groups.pop(ROUTING_REQUIRED_LABEL, []) or [])
        existing_by_title = {
            str(section.get("title") or "").strip().casefold(): section
            for section in repaired
            if infer_section_role(
                section.get("title"), section.get("section_role")
            )
            == "body"
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

        def route_group(
            label: str,
            paper_ids: list[str],
            *,
            method: str,
            boundary_rationale: str = "",
        ) -> None:
            nonlocal insert_at
            if not paper_ids:
                return
            display_label = (
                publication_section_title("", CROSS_CATEGORY_BOUNDARY_LABEL)
                if boundary_rationale
                else publication_section_title(
                    "", _capitalize_outline_heading(label)
                )
            )
            target = existing_by_title.get(display_label.casefold())
            created = target is None
            if target is None:
                target = {
                    "title": display_label,
                    "paper_ids": [],
                    "context_paper_ids": [],
                    "excluded_papers": [],
                    "section_role": "body",
                    "purpose": (
                        "compare the selected boundary evidence without assigning an "
                        f"unsupported {routing_axis_label} label."
                        if boundary_rationale
                        else (
                            f"compare the selected papers within this {routing_axis_label} "
                            "category and state its evidence boundaries."
                        )
                    ),
                    "notes": (
                        "Automatically retained after the primary-axis route could not be "
                        "resolved from the current source-addressable evidence."
                        if boundary_rationale
                        else "Automatically routed from source-addressable paper evidence."
                    ),
                }
                if boundary_rationale:
                    target["boundary_rationale"] = boundary_rationale
                repaired.insert(insert_at, target)
                insert_at += 1
                existing_by_title[display_label.casefold()] = target
            target_papers = target.setdefault("paper_ids", [])
            target_papers.extend(
                paper_id for paper_id in paper_ids if paper_id not in target_papers
            )
            adjustments.append(
                {
                    "source_section": "Unassigned selected papers",
                    "target_section": display_label,
                    "paper_ids": list(paper_ids),
                    "method": method,
                    "created_section": created,
                }
            )

        for label, paper_ids in groups.items():
            route_group(
                label,
                list(dict.fromkeys(paper_ids)),
                method="coverage_reconciliation_evidence_route",
            )
        route_group(
            CROSS_CATEGORY_BOUNDARY_LABEL,
            list(dict.fromkeys(unresolved_ids)),
            method="coverage_reconciliation_boundary_route",
            boundary_rationale=(
                "The selected primary study is relevant to the review scope, but its current "
                "source-addressable evidence does not justify one declared primary-axis "
                "category. Retain it for explicit cross-category comparison until stronger "
                "routing evidence is available."
            ),
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

    @staticmethod
    def _sanitize_generated_outline_titles(
        sections: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Keep classification diagnostics out of reader-facing headings."""

        repaired = deepcopy(sections)
        adjustments: list[dict[str, Any]] = []
        for section in repaired:
            if str(section.get("section_role") or "body").casefold() != "body":
                continue
            original = str(section.get("title") or "").strip()
            public = sanitize_internal_section_title(
                original,
                topic_partition=section.get("topic_partition"),
            )
            if not public or public == original:
                continue
            section["title"] = public
            adjustments.append(
                {
                    "source_section": original,
                    "target_section": public,
                    "paper_ids": list(section.get("paper_ids") or []),
                    "method": "publication_title_sanitization",
                    "created_section": False,
                }
            )
        return repaired, adjustments

    @staticmethod
    def _merge_equivalent_generated_body_sections(
        sections: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Merge only provably equivalent system-generated body categories.

        A one-paper section is not automatically evidence that two scientific
        categories are interchangeable. This pass therefore merges exact
        normalized category identities and leaves merely similar categories as
        reviewable suggestions in ``taxonomy_diagnostics``.
        """

        repaired: list[dict[str, Any]] = []
        by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        adjustments: list[dict[str, Any]] = []
        for source in deepcopy(sections):
            role = infer_section_role(
                source.get("title"), source.get("section_role")
            )
            if role != "body":
                repaired.append(source)
                continue
            title_key = re.sub(
                r"[^a-z0-9\u3400-\u9fff]+",
                " ",
                str(source.get("title") or "").casefold(),
            ).strip()
            partition_key = re.sub(
                r"[^a-z0-9\u3400-\u9fff]+",
                " ",
                str(source.get("topic_partition") or "").casefold(),
            ).strip()
            identity = (title_key, partition_key)
            target = by_identity.get(identity) if title_key else None
            if target is None:
                repaired.append(source)
                if title_key:
                    by_identity[identity] = source
                continue
            moved = [
                str(paper_id)
                for paper_id in source.get("paper_ids") or []
                if str(paper_id or "").strip()
            ]
            target_papers = target.setdefault("paper_ids", [])
            target_papers.extend(
                paper_id for paper_id in moved if paper_id not in target_papers
            )
            target_context = target.setdefault("context_paper_ids", [])
            target_context.extend(
                str(paper_id)
                for paper_id in source.get("context_paper_ids") or []
                if str(paper_id or "").strip()
                and str(paper_id) not in target_context
            )
            for field in ("boundary_rationale", "single_paper_justification"):
                if not str(target.get(field) or "").strip() and str(
                    source.get(field) or ""
                ).strip():
                    target[field] = source[field]
            adjustments.append(
                {
                    "source_section": str(source.get("title") or ""),
                    "target_section": str(target.get("title") or ""),
                    "paper_ids": moved,
                    "method": "equivalent_primary_axis_category_merge",
                    "created_section": False,
                }
            )
        return repaired, adjustments

    def _realign_generated_body_sections(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
        tag_key_override: str = "",
        axis_label_override: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Realign a generated candidate from current source-bound classifications.

        Saved generated outlines can predate fact extraction or taxonomy fixes.
        This pass is intentionally disabled for manually edited outlines.  It
        preserves roles and ordering, never reassigns on lexical scores alone,
        and never overrides a user's confirmed organization.
        """

        definition = OUTLINE_STYLES.get(str(outline_style or "").casefold())
        if definition is None:
            return deepcopy(sections), []
        routing_tag_key = str(tag_key_override or definition["tag_key"])
        routing_axis_label = str(axis_label_override or definition["axis"])
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
        # Do not move an established paper on the strength of word matches.
        # Only current source-bound classifications can realign a generated
        # candidate; explicit human organization is never overwritten.
        target_by_paper: dict[str, str] = {}
        for row in rows:
            paper_id = str(row.get("paper_id") or "")
            if paper_id not in body_set or confirmed_tags(row):
                continue
            entries = current_classification_tags(row).get(routing_tag_key) or []
            labels = {str(tag.get("partition_label") or "").strip() for tag in entries}
            labels.discard("")
            if len(labels) == 1:
                target_by_paper[paper_id] = labels.pop()

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
            source_partition = str(source.get("topic_partition") or "").strip()
            for paper_id in source_papers:
                target_category = target_by_paper.get(str(paper_id))
                if not target_category:
                    continue
                display_category = target_category
                # Re-routing follows the selected primary axis only.  A Topic
                # partition remains a comparison requirement and must not be
                # multiplied into another set of top-level sections.
                target_title = publication_section_title(
                    "", display_category
                )
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
                        "topic_partition": source_partition,
                        "purpose": (
                            f"compare the selected papers within this {routing_axis_label} "
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

    def _topic_outline_document(
        self,
        rows: list[dict[str, Any]],
        *,
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        taxonomy_profile: str,
        intent: dict[str, Any],
    ) -> str:
        """Build a Matrix-grounded outline from explicit Topic organization.

        The Topic controls the hierarchy, while Matrix evidence controls paper
        placement. This prevents the recommendation from becoming a decorative
        restatement of the prompt or assigning papers by prompt words alone.
        """

        primary_axis = str(intent.get("primary_axis") or "reaction_type")
        secondary_axes = [
            str(axis)
            for axis in intent.get("secondary_axes") or []
            if str(axis) and str(axis) != primary_axis
        ]
        partitions = [
            _clean_topic_partition(label)
            for label in (
                intent.get("required_partitions")
                or intent.get("partitions")
                or []
            )
            if _clean_topic_partition(label)
        ]
        comparison_dimensions = [
            str(value).strip()
            for value in (
                intent.get("comparison_dimensions")
                or intent.get("named_systems")
                or []
            )
            if str(value).strip()
        ]
        focus_dimensions = [
            str(value).strip()
            for value in (
                intent.get("focus_dimensions")
                or intent.get("outcome_dimensions")
                or intent.get("requested_outcomes")
                or []
            )
            if str(value).strip()
        ]
        contextual_paper_ids = self._contextual_outline_paper_ids(
            rows,
            tags_by_paper,
            text_by_paper,
        )
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            positively_out_of_scope = any(
                isinstance(outcome, dict)
                and str(outcome.get("status") or "") == "out_of_scope"
                and bool(outcome.get("evidence_refs"))
                for outcome in row.get("classification_outcomes") or []
            ) or (
                str(
                    (row.get("topic_partition_classification") or {}).get("status")
                    or ""
                )
                == "out_of_scope"
                and bool(
                    (row.get("topic_partition_classification") or {}).get(
                        "evidence_refs"
                    )
                )
            )
            if paper_id and positively_out_of_scope and paper_id not in contextual_paper_ids:
                contextual_paper_ids.append(paper_id)
        contextual_set = set(contextual_paper_ids)
        analytical_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() not in contextual_set
        ]
        # A Topic may request that a secondary distinction be discussed
        # independently, but that does not make it a second top-level axis.
        # Build body sections once from the declared primary axis; preserve
        # Topic partitions as traceable within-section requirements below.
        partitioned_rows: dict[str, list[dict[str, Any]]] = {"": analytical_rows}

        primary_label = str(
            intent.get("primary_axis_label")
            or (TOPIC_AXIS_LABELS.get(primary_axis) or {}).get("en")
            or primary_axis.replace("_", " ")
        )
        secondary_axis_labels = dict(intent.get("secondary_axis_labels") or {})
        secondary_labels = [
            str(
                secondary_axis_labels.get(axis)
                or (TOPIC_AXIS_LABELS.get(axis) or {}).get("en")
                or axis.replace("_", " ")
            )
            for axis in secondary_axes
        ]
        structure_description = primary_label
        if secondary_labels:
            structure_description += ", with " + ", ".join(secondary_labels) + " as secondary axes"
        if partitions:
            structure_description += "; track " + " versus ".join(partitions) + " within the primary structure"
        if focus_dimensions:
            structure_description += "; cover " + ", ".join(focus_dimensions)
        ordered_partitions = [""]
        groups_by_partition: dict[str, dict[str, list[str]]] = {}
        for partition in ordered_partitions:
            groups = self._outline_groups(
                partitioned_rows.get(partition) or [],
                tags_by_paper,
                text_by_paper,
                tag_key=primary_axis,
                taxonomy_profile=taxonomy_profile,
            )
            # An unresolved primary-study route is not evidence that the paper
            # is a review or an Introduction-only source. Retain it in a
            # visible boundary-analysis route instead of silently dropping it
            # from the selected corpus.
            unresolved = list(groups.pop(ROUTING_REQUIRED_LABEL, []) or [])
            if unresolved:
                groups[CROSS_CATEGORY_BOUNDARY_LABEL] = unresolved
            groups_by_partition[partition] = groups
        lines = [
            "# Selected Outline",
            "",
            f"Primary structure: Topic-guided ({structure_description}).",
            (
                "This system-recommended organization is grounded in the selected Matrix evidence and remains editable before Blueprint generation."
                if intent.get("system_recommended")
                else "This recommendation implements the explicit organization instructions in the user Topic and remains editable before Blueprint generation."
            ),
            "",
            "## Introduction",
            "Section role: introduction",
            "Purpose: define the review terminology, scope, explicit focus dimensions, and the evidence basis for the Topic-requested organization.",
        ]
        if focus_dimensions:
            lines.append(
                "Focus dimensions: " + ", ".join(focus_dimensions) + "."
            )
        if contextual_paper_ids:
            lines.extend(
                [
                    f"Context papers: {', '.join(contextual_paper_ids)}.",
                    "Notes: Use field-level sources for terminology and historical framing, not as primary body evidence.",
                ]
            )
        lines.append("")

        body_index = 0
        for partition in ordered_partitions:
            partition_rows = partitioned_rows.get(partition) or []
            groups = groups_by_partition.get(partition) or {}
            for group, paper_ids in groups.items():
                if not paper_ids:
                    continue
                body_index += 1
                partition_label = (
                    _capitalize_outline_heading(partition) if partition else ""
                )
                group_title = _capitalize_outline_heading(group)
                title = publication_section_title(partition_label, group_title)
                purpose_parts = [
                    f"compare the selected evidence within this {primary_label} category"
                ]
                if secondary_labels:
                    purpose_parts.append(
                        "compare " + ", ".join(secondary_labels) + " within the category"
                    )
                if comparison_dimensions:
                    purpose_parts.append(
                        "track the explicitly named comparison examples where supported: "
                        + ", ".join(comparison_dimensions)
                    )
                if focus_dimensions:
                    purpose_parts.append(
                        "cover the requested focus dimensions where supported: "
                        + ", ".join(focus_dimensions)
                    )
                paper_id_set = set(paper_ids)
                group_rows = [
                    row
                    for row in partition_rows
                    if str(row.get("paper_id") or "") in paper_id_set
                ]
                represented_secondary: dict[str, list[str]] = {}
                for secondary_axis in secondary_axes:
                    secondary_groups = self._outline_groups(
                        group_rows,
                        tags_by_paper,
                        text_by_paper,
                        tag_key=secondary_axis,
                        taxonomy_profile=taxonomy_profile,
                    )
                    represented_secondary[secondary_axis] = [
                        label
                        for label, assigned in secondary_groups.items()
                        if assigned and label != ROUTING_REQUIRED_LABEL
                    ]
                represented_partitions = list(
                    dict.fromkeys(
                        label
                        for row in group_rows
                        if (
                            label := _topic_partition_for_row(
                                row,
                                partitions,
                                text_by_paper.get(
                                    str(row.get("paper_id") or ""), ""
                                ),
                            )
                        )
                        and label in partitions
                    )
                )
                lines.extend(
                    [
                        f"## {body_index}. {title}",
                        "Section role: body",
                        f"Assigned papers: {', '.join(paper_ids)}.",
                        *(
                            [f"Topic partition: {partition}."]
                            if partition in partitions
                            else []
                        ),
                        "Purpose: " + "; ".join(purpose_parts) + ".",
                    ]
                )
                if group == CROSS_CATEGORY_BOUNDARY_LABEL:
                    lines.append(
                        "Boundary rationale: The selected primary studies are relevant to "
                        "the review scope, but their current source-addressable evidence "
                        "does not justify one declared primary-axis category. Retain them "
                        "for explicit cross-category comparison until stronger routing "
                        "evidence is available."
                    )
                notes: list[str] = []
                if represented_partitions:
                    notes.append(
                        "Topic-requested independent discussion represented by assigned evidence: "
                        + ", ".join(represented_partitions)
                        + "."
                    )
                for secondary_axis, represented in represented_secondary.items():
                    if represented:
                        notes.append(
                            str(
                                secondary_axis_labels.get(secondary_axis)
                                or (TOPIC_AXIS_LABELS.get(secondary_axis) or {}).get("en")
                                or secondary_axis.replace("_", " ")
                            ).capitalize()
                            + " represented by assigned evidence: "
                            + ", ".join(represented)
                            + "."
                        )
                if notes:
                    normalized_notes = " ".join(
                        note.removeprefix("Notes: ").strip() for note in notes
                    )
                    lines.append(f"Notes: {normalized_notes}")
                lines.append("")
        lines.extend(
            [
                "## Cross-regime comparison, limitations, and outlook",
                "Section role: conclusion",
                "Purpose: compare the primary categories, secondary axes, explicit focus dimensions, evidence boundaries, limitations, and future directions across the Topic-requested partitions.",
                "",
            ]
        )
        return "\n".join(lines)

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
        # Only document-scope evidence may create an Introduction context
        # source. Failed taxonomy routing remains a visible boundary-analysis
        # route and is never silently omitted from the generated outline.
        unresolved = list(groups.pop(ROUTING_REQUIRED_LABEL, []) or [])
        if unresolved:
            groups[CROSS_CATEGORY_BOUNDARY_LABEL] = unresolved
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
            public_label = publication_section_title(
                "", _capitalize_outline_heading(label)
            )
            block = [
                f"## {index}. {public_label}",
                "Section role: body",
                f"Assigned papers: {', '.join(paper_ids)}.",
                f"Purpose: compare the selected papers within this {definition['axis']} category.",
            ]
            if label == CROSS_CATEGORY_BOUNDARY_LABEL:
                block.append(
                    "Boundary rationale: The selected primary studies are relevant to the "
                    "review scope, but their current source-addressable evidence does not "
                    "justify one declared primary-axis category. Retain them for explicit "
                    "cross-category comparison until stronger routing evidence is available."
                )
            lines.extend([*block, ""])
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
        if any(section["title"] == "<!-- outline-untitled -->" for section in sections):
            raise WorkflowValidationError("Every section needs a title.")
        unknown = sorted(
            {
                paper_id
                for section in sections
                for paper_id in [
                    *section["paper_ids"],
                    *section.get("context_paper_ids", []),
                    *(
                        exclusion.get("paper_id")
                        for exclusion in section.get("excluded_papers", [])
                        if isinstance(exclusion, dict)
                    ),
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
        refresh_matrix_fact_summary(matrix)
        matrix, bibliography_metadata_artifact_ids = self._with_current_bibliography(
            principal, matrix
        )
        labels = library_paper_labels(self.repository.session_factory, principal.user_id)
        for row in matrix.get("rows") or []:
            if isinstance(row, dict):
                paper_id = str(row.get("paper_id") or "")
                row["display_label"] = labels.get(paper_id, paper_id)
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
        active_blueprint_artifact = blueprint_artifact
        candidate, candidate_artifact = self.latest_blueprint_candidate(
            principal, project_id, blueprint_artifact, blueprint_state.revision if blueprint_state else 0
        )
        if candidate is not None:
            blueprint, blueprint_artifact = candidate, candidate_artifact
        candidate_inputs = None
        if candidate is not None:
            candidate_matrix, _ = self._owned_blueprint_input(principal, project_id, candidate["source_matrix_artifact_id"], MATRIX_LOGICAL_NAME)
            candidate_outline, _ = self._owned_blueprint_input(principal, project_id, candidate["source_outline_artifact_id"], OUTLINE_LOGICAL_NAME)
            candidate_inputs = {"literature_matrix": candidate_matrix, "outline": candidate_outline,
                "source_matrix_artifact_id": candidate["source_matrix_artifact_id"],
                "source_outline_artifact_id": candidate["source_outline_artifact_id"]}
        rows = matrix["rows"]
        project = self._owned_project(principal, project_id)
        tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
        review_topic = str(
            matrix.get("review_topic") or (discovery or {}).get("topic") or ""
        )
        planning_taxonomy_profile = effective_taxonomy_profile(
            project.taxonomy_profile, review_topic
        )
        topic_intent = _topic_outline_intent(
            review_topic,
            discovery,
            list(matrix.get("classification_axes") or []),
        )
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
                    taxonomy_profile=planning_taxonomy_profile,
                ),
                "source": "builtin",
            }
            for style, definition in OUTLINE_STYLES.items()
            if style != TOPIC_GUIDED_STYLE
        ]
        if topic_intent.get("available"):
            generated.insert(
                0,
                {
                    "candidate_id": TOPIC_GUIDED_STYLE,
                    "outline_style": TOPIC_GUIDED_STYLE,
                    "labels": {
                        "en": "Recommended from your Topic",
                        "zh": "根据你的 Topic 推荐",
                    },
                    "outline_md": self._topic_outline_document(
                        rows,
                        tags_by_paper=tags_by_paper,
                        text_by_paper=text_by_paper,
                        taxonomy_profile=planning_taxonomy_profile,
                        intent=topic_intent,
                    ),
                    "source": "topic",
                    "topic_outline_intent": topic_intent,
                },
            )
        if outline_artifact is not None and outline is not None:
            generated.append(
                {
                    "candidate_id": "saved-current",
                    "outline_style": outline.get("outline_style", "custom"),
                    "labels": {"en": "Saved outline", "zh": "已保存大纲"},
                    "outline_md": _sanitize_outline_markdown_headings(
                        outline.get("outline_md")
                    ),
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
                or self._matrix_dependency_matches(principal, project_id, outline_source_id, matrix_artifact)
            )
        )
        blueprint_current = bool(
            blueprint is not None
            and blueprint_artifact is not None
            and outline_artifact is not None
            and outline_current
            and self._matrix_dependency_matches(principal, project_id, blueprint.get("source_matrix_artifact_id"), matrix_artifact)
            and str(blueprint.get("source_outline_artifact_id") or "")
            == outline_artifact.id
            and blueprint_state is not None
            and (candidate is not None or blueprint_state.status != "stale")
        )
        if candidate_inputs is not None:
            blueprint_current = True
        public_outline = deepcopy(outline) if isinstance(outline, dict) else None
        if public_outline is not None:
            public_outline["outline_md"] = _sanitize_outline_markdown_headings(
                public_outline.get("outline_md")
            )
        public_blueprint = (
            deepcopy(blueprint) if isinstance(blueprint, dict) else None
        )
        if public_blueprint is not None:
            public_blueprint["sections"] = [
                apply_single_paper_policy(section)
                for section in public_blueprint.get("sections") or []
                if isinstance(section, dict)
            ]
            # Diagnose the original headings before display sanitization so
            # catch-all routing blockers remain visible for legacy artifacts.
            public_blueprint["taxonomy_diagnostics"] = blueprint_taxonomy_diagnostics(
                public_blueprint, [row["paper_id"] for row in rows])
            for section in public_blueprint.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                section["title"] = sanitize_internal_section_title(
                    section.get("title"),
                    topic_partition=section.get("topic_partition"),
                )
            public_blueprint["section_writing_plan_md"] = (
                _sanitize_outline_markdown_headings(
                    public_blueprint.get("section_writing_plan_md")
                )
            )
        scope_contract = dict((outline or {}).get("scope_contract") or {})
        scope_report = dict((outline or {}).get("scope_diagnostics") or {})
        coverage_report = dict((outline or {}).get("coverage_diagnostics") or {})
        basis = dict((outline or {}).get("classification_basis") or {})
        public_classification_contract = dict(
            (blueprint or {}).get("classification_contract")
            or (outline or {}).get("classification_contract")
            or matrix.get("classification_contract")
            or {}
        )
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
        enrichment_counts.update(
            {
                f"readiness_{status}": sum(
                    1
                    for row in rows
                    if str(
                        (row.get("fact_enrichment") or {}).get(
                            "review_readiness"
                        )
                        or "source_not_established"
                    )
                    == status
                )
                for status in ("complete", "partial", "source_not_established")
            }
        )
        enrichment_counts["verification_pending"] = sum(
            (row.get("fact_enrichment", {}).get("processing") or {}).get("verification") == "pending" for row in rows)
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
            "topic": review_topic,
            "literature_matrix": matrix,
            "matrix_artifact_id": matrix_artifact.id,
            "bibliography_metadata_artifact_ids": bibliography_metadata_artifact_ids,
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
                "planning_blocked": False,  # Compatibility field; evidence gaps do not gate planning.
            },
            "discovery_selection": {
                "selected_paper_count": len(selected_ids),
                "selected_paper_ids": selected_ids,
                "selection_current": selection_current,
            },
            "selected_outline_md": str(
                (public_outline or {}).get("outline_md") or ""
            ),
            "outline_selection": (
                {**public_outline, "artifact_id": outline_artifact.id}
                if public_outline is not None and outline_artifact is not None
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
            "classification_contract": public_classification_contract,
            "taxonomy_diagnostics": dict(
                (public_blueprint or {}).get("taxonomy_diagnostics")
                or outline_diagnostics
            ),
            "outline_candidates": generated + reference_candidates,
            "reference_outline_candidates": reference_candidates,
            "legacy_reference_outline_count": len(all_reference_candidates)
            - len(reference_candidates),
            "section_blueprint": public_blueprint,
            "blueprint_artifact_id": blueprint_artifact.id if blueprint_artifact else None,
            "blueprint_revision": blueprint_state.revision if blueprint_state else 0,
            "blueprint_current": blueprint_current,
            "blueprint_candidate_pending": candidate is not None,
            "blueprint_candidate_inputs": candidate_inputs,
            "active_blueprint_artifact_id": active_blueprint_artifact.id if active_blueprint_artifact else None,
            "blueprint_jobs": [_planning_job_payload(job) for job in self.repository.list_project_jobs(
                principal.user_id, project_id, job_type="planning.blueprint", limit=5
            )],
            "section_writing_plan_md": str(
                (public_blueprint or {}).get("section_writing_plan_md") or ""
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
        current_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        actual_revision = current_state.revision if current_state else 0
        if actual_revision != int(revision):
            raise WorkflowConflict(
                "Workflow stage changed since it was loaded.",
                details={"expected_revision": int(revision), "actual_revision": actual_revision},
            )
        matrix, matrix_artifact = self._matrix(principal, project_id)
        matrix, _bibliography_metadata_artifact_ids = self._with_current_bibliography(
            principal, matrix
        )
        project = self._owned_project(principal, project_id)
        rows = matrix["rows"]
        matrix_ids = set(_paper_ids(rows))
        style = str(outline_style or "").strip().casefold()
        discovery, _discovery_artifact = self._read_json(
            principal,
            project_id,
            DISCOVERY_LOGICAL_NAME,
            required=False,
        )
        review_topic = str(
            matrix.get("review_topic") or (discovery or {}).get("topic") or ""
        )
        planning_taxonomy_profile = effective_taxonomy_profile(
            project.taxonomy_profile, review_topic
        )
        topic_intent = _topic_outline_intent(
            review_topic,
            discovery,
            list(matrix.get("classification_axes") or []),
        )
        topic_text_by_paper: dict[str, str] = {}
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
        elif style == TOPIC_GUIDED_STYLE:
            if not topic_intent.get("available"):
                raise WorkflowValidationError(
                    "The Topic does not contain a usable organization instruction."
                )
            tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
            topic_text_by_paper = text_by_paper
            markdown = self._validate_outline(
                self._topic_outline_document(
                    rows,
                    tags_by_paper=tags_by_paper,
                    text_by_paper=text_by_paper,
                    taxonomy_profile=planning_taxonomy_profile,
                    intent=topic_intent,
                ),
                matrix_ids,
            )
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
                taxonomy_profile=planning_taxonomy_profile,
            )
            complete = True
        current_outline, current_outline_artifact = self._read_json(
            principal,
            project_id,
            OUTLINE_LOGICAL_NAME,
            required=False,
        )
        parsed_sections = _outline_sections(markdown) if complete else []
        if complete and not manual and style in OUTLINE_STYLES:
            generated_tags, generated_text = self._outline_sources(principal, rows)
            if not topic_text_by_paper:
                topic_text_by_paper = generated_text
            routing_tag_key = ""
            routing_axis_label = ""
            if style == TOPIC_GUIDED_STYLE:
                primary_axis = str(topic_intent.get("primary_axis") or "")
                if primary_axis in TOPIC_AXIS_LABELS:
                    routing_tag_key = primary_axis
                    routing_axis_label = TOPIC_AXIS_LABELS[primary_axis]["en"]
            parsed_sections, coverage_adjustments = (
                self._reconcile_generated_outline_coverage(
                    parsed_sections,
                    rows,
                    generated_tags,
                    generated_text,
                    outline_style=style,
                    taxonomy_profile=planning_taxonomy_profile,
                    tag_key_override=routing_tag_key,
                    axis_label_override=routing_axis_label,
                )
            )
            if coverage_adjustments:
                markdown = self._validate_outline(
                    _outline_markdown_from_sections(
                        parsed_sections,
                        outline_style=style,
                        automatically_adjusted=True,
                    ),
                    matrix_ids,
                )
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
        scope_seed = dict(scope_input or {})
        scope_seed.setdefault(
            "coverage_mode", str(matrix.get("coverage_mode") or "local_bounded")
        )
        discovery_coverage = matrix.get("coverage_diagnostics")
        if isinstance(discovery_coverage, dict):
            scope_seed.setdefault(
                "discovery_coverage_diagnostics", deepcopy(discovery_coverage)
            )
        scope_style = (
            "reaction"
            if style == TOPIC_GUIDED_STYLE
            and topic_intent.get("primary_axis") == "reaction_type"
            else "catalyst"
            if style == TOPIC_GUIDED_STYLE
            and topic_intent.get("primary_axis") == "catalyst_or_method"
            else "substrate"
            if style == TOPIC_GUIDED_STYLE
            and topic_intent.get("primary_axis") == "substrate"
            else style
        )
        scope = derive_scope_contract(
            matrix.get("review_topic"),
            scope_style,
            rows,
            current=scope_seed,
        )
        if style == TOPIC_GUIDED_STYLE:
            scope["primary_navigation_axis"] = str(
                topic_intent.get("primary_axis") or "reaction_type"
            )
            scope["secondary_axes"] = list(topic_intent.get("secondary_axes") or [])
        scope_report = scope_diagnostics(scope)
        coverage_report = coverage_diagnostics(scope, rows)
        basis = classification_basis(scope_style)
        if style == TOPIC_GUIDED_STYLE:
            required_partitions = list(
                topic_intent.get("required_partitions")
                or topic_intent.get("partitions")
                or []
            )
            if not topic_text_by_paper:
                _topic_tags, topic_text_by_paper = self._outline_sources(
                    principal, rows
                )
            represented_partitions = {
                label
                for row in rows
                if (
                    label := _topic_partition_for_row(
                        row,
                        required_partitions,
                        topic_text_by_paper.get(
                            str(row.get("paper_id") or ""), ""
                        ),
                    )
                )
                in required_partitions
            }
            partition_coverage_boundaries = {
                partition: {
                    "reason": (
                        "No selected Matrix paper currently has a source-supported, "
                        "high-confidence route to this independently requested partition."
                    ),
                    "source": "matrix_evidence_partition_classifier",
                }
                for partition in required_partitions
                if partition not in represented_partitions
            }
            basis.update(
                {
                    "primary_axis": scope["primary_navigation_axis"],
                    "overview_axis": scope["primary_navigation_axis"],
                    "orthogonal_axes": list(topic_intent.get("secondary_axes") or []),
                    "overview_secondary_axes": list(
                        topic_intent.get("secondary_axes") or []
                    ),
                    "topic_partitions": required_partitions,
                    "required_outline_partitions": required_partitions,
                    "topic_partition_coverage_boundaries": partition_coverage_boundaries,
                    "topic_comparison_dimensions": list(
                        topic_intent.get("comparison_dimensions")
                        or topic_intent.get("named_systems")
                        or []
                    ),
                    "topic_axis_examples": dict(
                        topic_intent.get("axis_examples") or {}
                    ),
                    "topic_outcome_dimensions": list(
                        topic_intent.get("focus_dimensions")
                        or topic_intent.get("outcome_dimensions")
                        or topic_intent.get("requested_outcomes")
                        or []
                    ),
                    "topic_focus_dimensions": list(
                        topic_intent.get("focus_dimensions")
                        or topic_intent.get("outcome_dimensions")
                        or topic_intent.get("requested_outcomes")
                        or []
                    ),
                    "partition_trace_policy": str(
                        topic_intent.get("partition_trace_policy")
                        or "source_bounded_model_or_section_contract"
                    ),
                    "boundary_policy": "explicit_rationale_allowed",
                    "source": "explicit_user_topic",
                }
            )
        selected_axis_contract = canonical_classification_contract(
            (
                (topic_intent.get("classification_contract") or {}).get("axes")
                if style == TOPIC_GUIDED_STYLE
                and isinstance(topic_intent.get("classification_contract"), dict)
                else matrix.get("classification_axes") or []
            ),
            primary_axis_hint=str(
                scope.get("primary_navigation_axis")
                or basis.get("primary_axis")
                or ""
            ),
            source="selected_outline",
        )
        basis = _basis_with_axis_contract(basis, selected_axis_contract)
        diagnostics = taxonomy_diagnostics(
            parsed_sections,
            _paper_ids(rows),
            classification_contract=basis,
        )
        payload = {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "outline_style": style,
            "outline_md": markdown,
            "outline_complete": complete,
            "selection_source": (
                "manual"
                if manual
                else "custom_draft"
                if not complete
                else "topic_recommendation"
                if style == TOPIC_GUIDED_STYLE
                else "template"
            ),
            "manually_edited": bool(manual),
            "source_matrix_artifact_id": matrix_artifact.id,
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "classification_contract": selected_axis_contract,
            "taxonomy_diagnostics": diagnostics,
            "topic_outline_intent": (
                topic_intent if style == TOPIC_GUIDED_STYLE else None
            ),
            "saved_at": utc_now().isoformat(),
        }
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
            and current_outline.get("classification_contract")
            == selected_axis_contract
            and current_outline.get("taxonomy_diagnostics") == diagnostics
            and current_outline.get("topic_outline_intent")
            == payload.get("topic_outline_intent")
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
                "classification_contract": selected_axis_contract,
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
            "classification_contract": selected_axis_contract,
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

    def prepare_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        previous_blueprint, previous_blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        previous_blueprint_state = self.repository.get_stage_state(
            principal.user_id, project_id, "blueprint"
        )
        if (previous_blueprint_state.revision if previous_blueprint_state else 0) != revision:
            raise WorkflowConflict("Blueprint changed since this page was loaded.")
        matrix, matrix_artifact = self._matrix(principal, project_id)
        matrix, bibliography_metadata_artifact_ids = self._with_current_bibliography(
            principal, matrix
        )
        matrix_rows = [
            row for row in matrix.get("rows") or [] if isinstance(row, dict)
        ]
        project = self._owned_project(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        review_topic = str(
            matrix.get("review_topic") or (discovery or {}).get("topic") or ""
        )
        planning_taxonomy_profile = effective_taxonomy_profile(
            project.taxonomy_profile, review_topic
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
        structure_change_suggestions: list[dict[str, Any]] = []
        resolved_outline_md = str(outline["outline_md"])
        outline_style = str(outline.get("outline_style") or "")
        routing_style = outline_style
        routing_tag_key = ""
        routing_axis_label = ""
        if outline_style == TOPIC_GUIDED_STYLE:
            primary_axis = str(
                (outline.get("topic_outline_intent") or {}).get("primary_axis")
                or ""
            )
            if primary_axis in TOPIC_AXIS_LABELS:
                routing_tag_key = primary_axis
                routing_axis_label = TOPIC_AXIS_LABELS[primary_axis]["en"]
        tags_by_paper: dict[str, dict[str, Any]] = {}
        text_by_paper: dict[str, str] = {}
        if not bool(outline.get("manually_edited")):
            tags_by_paper, text_by_paper = self._outline_sources(
                principal, matrix["rows"]
            )
            parsed, placeholder_adjustments = (
                self._auto_repair_generated_routing_sections(
                    parsed,
                    matrix["rows"],
                    text_by_paper,
                    outline_style=routing_style,
                    taxonomy_profile=planning_taxonomy_profile,
                    tag_key_override=routing_tag_key,
                    axis_label_override=routing_axis_label,
                )
            )
            auto_routing_adjustments.extend(placeholder_adjustments)
            parsed, coverage_adjustments = (
                self._reconcile_generated_outline_coverage(
                    parsed,
                    matrix_rows,
                    tags_by_paper,
                    text_by_paper,
                    outline_style=routing_style,
                    taxonomy_profile=planning_taxonomy_profile,
                    tag_key_override=routing_tag_key,
                    axis_label_override=routing_axis_label,
                )
            )
            auto_routing_adjustments.extend(coverage_adjustments)
            parsed, evidence_realignments = self._realign_generated_body_sections(
                parsed,
                matrix_rows,
                text_by_paper,
                outline_style=routing_style,
                taxonomy_profile=planning_taxonomy_profile,
                tag_key_override=routing_tag_key,
                axis_label_override=routing_axis_label,
            )
            auto_routing_adjustments.extend(evidence_realignments)
            parsed, title_adjustments = self._sanitize_generated_outline_titles(
                parsed
            )
            auto_routing_adjustments.extend(title_adjustments)
            parsed, equivalent_merge_adjustments = (
                self._merge_equivalent_generated_body_sections(parsed)
            )
            auto_routing_adjustments.extend(equivalent_merge_adjustments)
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
                    outline_style=outline_style,
                    automatically_adjusted=True,
                )
        else:
            # Custom headings are user-owned. Keep evidence-based corrections
            # as suggestions rather than rewriting the saved structure.
            _tags, text_by_paper = self._outline_sources(
                principal, matrix["rows"]
            )
            candidate, candidate_adjustments = self._realign_generated_body_sections(
                parsed,
                matrix_rows,
                text_by_paper,
                outline_style=routing_style,
                taxonomy_profile=planning_taxonomy_profile,
                tag_key_override=routing_tag_key,
                axis_label_override=routing_axis_label,
            )
            structure_change_suggestions = candidate_adjustments
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
                    "section_id": (section.get("section_id") if outline.get("manually_edited") else None) or f"S{len(prepared) + 1:02d}",
                    "section_role": role,
                    "paper_ids": assigned,
                    "context_paper_ids": list(
                        dict.fromkeys(section.get("context_paper_ids") or [])
                    ),
                }
            )

        if len({section["section_id"] for section in prepared}) != len(prepared):
            raise WorkflowValidationError("Custom outline section IDs must be unique.")
        scope = derive_scope_contract(
            matrix.get("review_topic") or (discovery or {}).get("topic"),
            outline.get("outline_style"),
            matrix["rows"],
            current=outline.get("scope_contract")
            if isinstance(outline.get("scope_contract"), dict)
            else None,
        )
        time_span = (
            scope.get("time_span") if isinstance(scope.get("time_span"), dict) else {}
        )
        scope_year_from = _publication_year(time_span.get("from"))
        scope_year_to = _publication_year(time_span.get("to"))
        if scope_year_from is not None and scope_year_to is not None:
            if scope_year_from > scope_year_to:
                scope_year_from, scope_year_to = scope_year_to, scope_year_from
            rows_by_scope_id = {
                str(row.get("paper_id") or ""): row
                for row in matrix_rows
                if str(row.get("paper_id") or "")
            }
            for section in prepared:
                if str(section.get("section_role") or "body") != "body":
                    continue
                assigned = list(section.get("paper_ids") or [])
                outside = [
                    paper_id
                    for paper_id in assigned
                    if (
                        (year := _matrix_publication_year(
                            rows_by_scope_id.get(paper_id) or {}
                        ))
                        is not None
                        and not (scope_year_from <= year <= scope_year_to)
                    )
                ]
                if not outside:
                    continue
                section["paper_ids"] = [
                    paper_id for paper_id in assigned if paper_id not in set(outside)
                ]
                section["context_paper_ids"] = list(
                    dict.fromkeys(
                        [*(section.get("context_paper_ids") or []), *outside]
                    )
                )
                auto_routing_adjustments.append(
                    {
                        "source_section": str(section.get("title") or ""),
                        "target_section": f"Context evidence outside {scope_year_from}–{scope_year_to}",
                        "paper_ids": outside,
                        "method": "explicit_time_scope_role_downgrade",
                        "created_section": False,
                    }
                )
            if not bool(outline.get("manually_edited")):
                prepared = [
                    section
                    for section in prepared
                    if section.get("section_role") != "body"
                    or section.get("paper_ids")
                    or section.get("context_paper_ids")
                ]
                resolved_outline_md = _outline_markdown_from_sections(
                    prepared,
                    outline_style=outline_style,
                    automatically_adjusted=bool(auto_routing_adjustments),
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
        # Resolve the one authoritative classification contract before section
        # contracts are built. Thesis and paragraph-depth derivation consume
        # this existing contract; no parallel taxonomy state is introduced.
        basis = dict(
            outline.get("classification_basis")
            or classification_basis(outline.get("outline_style"))
        )
        selected_axis_contract = classification_contract_from_document(
            outline,
            primary_axis_hint=str(basis.get("primary_axis") or ""),
            source="blueprint_from_selected_outline",
        )
        basis = _basis_with_axis_contract(basis, selected_axis_contract)

        sections: list[dict[str, Any]] = []
        topic_required_partitions = _required_topic_partitions_from_outline(outline)
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
                thesis = str(section.get("purpose") or "").strip()
                problem = f"What does the current evidence establish about {section['title']}?"
                if len(primary) == 1:
                    claim = (
                        "Analyze the assigned study's question, verified findings and limitations "
                        "as a bounded research case; do not infer field-wide consensus."
                    )
                elif primary:
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
            thesis_contract = {"text": thesis, "status": "provisional" if role == "body" else "structural_synthesis"}
            depth_contract = derive_section_depth_contract(
                {
                    "section_role": role,
                    "primary_papers": primary,
                    "target_words": target_words,
                }
            )
            sections.append(
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "heading_level": section.get("heading_level", 2),
                    "parent_headings": section.get("parent_headings") or [],
                    "organizing_only": bool(section.get("organizing_only")),
                    "notes": str(section.get("notes") or ""),
                    "excluded_papers": section.get("excluded_papers") or [],
                    "section_role": role,
                    "topic_partition": str(
                        section.get("topic_partition") or ""
                    ).strip(),
                    "boundary_rationale": str(
                        section.get("boundary_rationale") or ""
                    ).strip(),
                    "section_thesis": thesis,
                    "scientific_thesis": thesis_contract,
                    "thesis_status": str(
                        thesis_contract.get("status") or "provisional"
                    ),
                    "review_problem": problem,
                    "major_papers": primary,
                    "primary_papers": primary,
                    "supporting_papers": supporting,
                    "context_papers": context_papers,
                    # ``claim`` is an authoring operation, not a proposition
                    # expected to occur in a source paper.  Keep the legacy
                    # field for old renderers, but make its role explicit and
                    # publish the executable contract separately.
                    "review_claims": [
                        {
                            "claim": claim,
                            "legacy_role": "writing_requirement",
                        }
                    ],
                    "scientific_claims": [],
                    "generation_eligible": role != "body",
                    "executable_claim_count": 0,
                    "pending_claim_count": 0,
                    "automatic_resolution": {"action": "awaiting_chapter_planning", "requires_user_action": False},
                    "writing_requirements": [
                        {
                            "requirement_id": f"WR-{section['section_id']}-01",
                            "type": (
                                "framing_synthesis"
                                if role == "introduction"
                                else "cross_section_synthesis"
                                if role == "conclusion"
                                else "cross_study_synthesis"
                            ),
                            "instruction": claim,
                            "source": "native_blueprint",
                        }
                    ],
                    "figure_or_table_needs": [
                        {
                            "type": "Figure or table",
                            "purpose": figure_need,
                            "requirement": "required" if role == "body" else "optional",
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
                    "depth_contract": depth_contract,
                    "required_fact_roles": [],
                    "targeted_fact_gaps": {},
                    "secondary_axis_routes": {},
                    "targeted_fact_extraction": {},
                    "evidence_readiness": {"status": "not_reviewed" if role == "body" else "synthesis"},
                }
            )
        partition_routes, partition_support = _topic_partition_routes(
            sections,
            rows_by_id,
            topic_required_partitions,
            text_by_paper,
        )
        for section in sections:
            section_id = str(section.get("section_id") or "")
            section["secondary_axis_routes"] = partition_routes.get(section_id, {})
            # The section academic contract must include the repaired route.
            section["academic_contract"] = section_academic_contract(section)
            section["synthesis_requirements"] = synthesis_requirements(
                section, taxonomy_profile=planning_taxonomy_profile
            )
        if not sections:
            raise WorkflowValidationError("The selected outline contains no usable sections.")
        contextual_paper_ids = list(
            dict.fromkeys(
                paper_id
                for section in sections
                for paper_id in section.get("context_papers") or []
            )
        )
        scope_report = scope_diagnostics(scope)
        coverage_report = coverage_diagnostics(scope, matrix["rows"])
        if topic_required_partitions:
            represented_partitions = {
                partition
                for routes in partition_routes.values()
                for partition, paper_ids in routes.items()
                if paper_ids
            }
            raw_boundaries = basis.get("topic_partition_coverage_boundaries")
            partition_boundaries = (
                deepcopy(raw_boundaries) if isinstance(raw_boundaries, dict) else {}
            )
            for partition in topic_required_partitions:
                if partition in represented_partitions:
                    partition_boundaries.pop(partition, None)
                    continue
                supported = list(partition_support.get(partition) or [])
                partition_boundaries.setdefault(
                    partition,
                    {
                        "reason": (
                            "Source-supported Matrix evidence exists, but it is not owned "
                            "by an in-scope body section in the current outline."
                            if supported
                            else "No selected Matrix paper currently has a source-supported, "
                            "high-confidence route to this independently requested partition."
                        ),
                        "source": "blueprint_matrix_partition_route_audit",
                        "paper_ids": supported,
                    },
                )
            basis["topic_partitions"] = list(topic_required_partitions)
            basis["required_outline_partitions"] = list(topic_required_partitions)
            basis["topic_partition_coverage_boundaries"] = partition_boundaries
        diagnostics = taxonomy_diagnostics(
            sections,
            matrix_order,
            classification_contract=basis,
        )
        restructure_reasons: list[str] = []
        if auto_routing_adjustments:
            restructure_reasons.append("evidence_based_paper_routing_changed")
        if any(
            str((section.get("evidence_readiness") or {}).get("status") or "")
            in {"partial", "insufficient"}
            for section in sections
            if str(section.get("section_role") or "body") == "body"
        ):
            restructure_reasons.append("section_evidence_distribution_is_uneven")
        for issue in diagnostics.get("issues") or []:
            if isinstance(issue, dict) and issue.get("rule_id"):
                restructure_reasons.append(str(issue["rule_id"]))
        restructure_record = _blueprint_restructure_record(
            previous_blueprint,
            sections,
            previous_artifact_id=(
                previous_blueprint_artifact.id if previous_blueprint_artifact else ""
            ),
            trigger_reasons=restructure_reasons,
        )
        current_sections_artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, "sections/section_drafts.json"
        )
        current_draft_artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, "draft/manuscript.md"
        )
        has_manual_draft = bool(
            current_draft_artifact
            and (
                current_draft_artifact.metadata.get("unverified_manual_paragraph_ids")
                or str(current_draft_artifact.metadata.get("operation") or "")
                == "full-edit"
                or str(current_draft_artifact.metadata.get("operation") or "").startswith(
                    "paragraph-edit:"
                )
            )
        )
        restructure_record.update(
            {
                "application_mode": (
                    "candidate_requires_existing_blueprint_confirmation"
                    if restructure_record["is_restructure"]
                    else "not_applicable"
                ),
                "downstream_sections_present": current_sections_artifact is not None,
                "manual_draft_content_present": has_manual_draft,
            }
        )
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if matrix_state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        try:
            rule_pack = resolve_rule_pack(self.root, topic=review_topic)
        except RulePackConfigurationError as exc:
            raise WorkflowConflict(str(exc)) from exc
        overview_structure_contract = derive_overview_structure_contract(
            review_topic,
            query_plan=(discovery or {}).get("query_plan") or {},
            matrix=matrix,
            sections=sections,
            taxonomy_profile=project.taxonomy_profile,
        )
        blueprint = {
            "schema_version": FACT_GROUNDED_BLUEPRINT_SCHEMA_VERSION,
            "evidence_mode": "source_passages/1",
            "project_id": project_id,
            "review_topic": review_topic,
            "outline_style": outline.get("outline_style"),
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "classification_contract": selected_axis_contract,
            "classification_contract_lineage": {
                "matrix_fingerprint": str(
                    classification_contract_from_document(
                        matrix,
                        primary_axis_hint=str(
                            (matrix.get("classification_recommendation") or {}).get(
                                "primary_axis_id"
                            )
                            or ""
                        ),
                        source="blueprint_matrix_input",
                    ).get("fingerprint")
                    or ""
                ),
                "outline_fingerprint": str(
                    selected_axis_contract.get("fingerprint") or ""
                ),
                "effective_fingerprint": str(
                    selected_axis_contract.get("fingerprint") or ""
                ),
                "status": "selected_outline_contract_applied",
            },
            "taxonomy_profile": project.taxonomy_profile,
            "effective_taxonomy_profile": planning_taxonomy_profile,
            "taxonomy_diagnostics": diagnostics,
            "overview_structure_contract": overview_structure_contract,
            "source_matrix_artifact_id": matrix_artifact.id,
            "source_outline_artifact_id": outline_artifact.id,
            "source_bibliography_metadata_artifact_ids": bibliography_metadata_artifact_ids,
            "resolved_outline_md": resolved_outline_md,
            "auto_routing_adjustments": auto_routing_adjustments,
            "structure_change_suggestions": structure_change_suggestions,
            "restructure_record": restructure_record,
            "rule_pack": rule_pack["name"],
            "rule_pack_path": rule_pack["path"],
            "rule_pack_files": rule_pack["files"],
            "rule_pack_sha256": rule_pack["sha256"],
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
        planning_matrix = deepcopy(matrix)
        missing_abstract = []
        for row in planning_matrix.get("rows") or []:
            row["abstract"] = self._matrix_abstract(row)
            if not row["abstract"]:
                missing_abstract.append(str(row["paper_id"]))
        if missing_abstract and self.library_index is not None and self.library_index.enabled:
            rows_by_id = {str(row["paper_id"]): row for row in planning_matrix["rows"]}
            for hit in self.library_index.primary_coverage_hits(
                principal, allowed_papers=missing_abstract, per_paper_limit=3
            ):
                rows_by_id[hit.paper_id].setdefault("planning_source_passages", []).append({
                    "chunk_id": hit.chunk_id,
                    "source_lineage_hash": hit.source_lineage_hash,
                    "page_start": hit.page_start,
                    "page_end": hit.page_end,
                    "content": hit.content[:3000],
                })
        prepared = {
            "project_id": project_id,
            "section_blueprint": blueprint,
            "blueprint_revision": revision,
            "matrix_revision": matrix_state.revision,
            "base_blueprint_artifact_id": previous_blueprint_artifact.id if previous_blueprint_artifact else None,
            # The publication candidate stays free of retrieval-only planning
            # passages.  The academic planner reads the second snapshot and
            # never promotes it as Matrix business data.
            "matrix_snapshot": deepcopy(matrix),
            "planning_matrix_snapshot": planning_matrix,
            "outline_snapshot": deepcopy(outline),
            "base_matrix_status": matrix_state.status,
            "base_blueprint_status": previous_blueprint_state.status if previous_blueprint_state else "pending",
            "restructure_record": restructure_record,
        }
        return prepared
