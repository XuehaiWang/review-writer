"""Stage 03/04 task lifecycle, independent of HTTP route construction.

Builders produce isolated candidates. Services alone validate input versions and
promote artifacts; checkpoints never become current stage output here.
"""

from __future__ import annotations

import re
import logging
from copy import deepcopy
from collections.abc import Callable, Mapping

from review_writer_api.domain_services.sections import SectionProviderUnavailable
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.job_service import JobYieldRequested
from review_writer_api.scientific_runner import ScientificRunError, ScientificModelDeferred, ScientificInsufficientCredit
from review_writer_api.job_handlers.lifecycle import report_committed_progress
from review_writer_api.security import Principal, Role
from review_writer_core.stages.sections.coverage import reusable_section_entries
from review_writer_core.classification_axes import axis_requires_formal_route
from review_writer_core.stages.planning.matrix import has_fact_sources


TRANSIENT_PROVIDER_ERROR = re.compile(
    r"(?:HTTP\s*(?:429|503)|rate[_ -]?limit|service unavailable|temporar(?:y|ily))",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def selected_formal_classification_contract(outline: dict | None) -> dict | None:
    contract = (outline or {}).get("classification_contract")
    if not isinstance(contract, dict):
        return None
    return contract if any(
        axis_requires_formal_route(axis)
        for axis in contract.get("axes") or []
        if isinstance(axis, dict)
    ) else None


def wait_for_delegated_model(context, result: dict | None) -> None:
    """Resume when any child is ready, not only the first/slowest child."""
    result = result if isinstance(result, dict) else {}
    model_job_ids = result.get("waiting_model_job_ids") or (
        [result["waiting_model_job_id"]] if result.get("waiting_model_job_id") else [])
    if not model_job_ids:
        return
    jobs = [context.repository.get_job(context.user_id, str(job_id)) for job_id in model_job_ids]
    if any(job is None or job.project_id != context.project_id or job.job_type != "model.dispatch" for job in jobs):
        raise WorkflowConflict("The delegated model task is unavailable.")
    if all(job.status in {"queued", "running"} for job in jobs):
        raise JobYieldRequested(delay_seconds=10, queue_reason="model_waiting")
    # Parallel sections must consume each ready result independently. A failed
    # child is surfaced by its own chapter; don't discard successful siblings.
    if len(jobs) > 1:
        return
    model_job = jobs[0]
    if model_job.status != "succeeded":
        if model_job.error_code == "INSUFFICIENT_CREDIT":
            raise ScientificInsufficientCredit(attempts=1)
        raise WorkflowConflict("The delegated model task did not finish. No model call was replayed automatically.")


def yield_to_delegated_model(context, exc: ScientificModelDeferred) -> None:
    # The progress callback may just have published a resumable checkpoint.
    # Partial-result writes replace result_json, so retain that snapshot when
    # recording the child job that this task is waiting for.
    current_job = context.repository.get_job(context.user_id, context.job_id)
    current_result = current_job.result if current_job is not None else None
    snapshot = dict(current_result) if isinstance(current_result, dict) else {}
    snapshot.pop("waiting_model_job_id", None)
    snapshot["waiting_model_job_ids"] = exc.model_job_ids
    context.report_partial_result(snapshot)
    delegated = [context.repository.get_job(context.user_id, job_id) for job_id in exc.model_job_ids]
    delay = 10 if all(job is None or job.status in {"queued", "running"} for job in delegated) else 0
    raise JobYieldRequested(delay_seconds=delay, queue_reason="model_waiting")


def section_retry_checkpoints(context) -> tuple[dict | None, dict | None]:
    """Merge reusable checkpoints across a bounded retry ancestry.

    The nearest retry wins for sections it completed; older ancestors can
    still supply other completed sections.  The section builder performs the
    current task/evidence validation before any entry is reused.
    """

    checkpoint: dict | None = None
    merged_entries: dict = {}
    fact_repair_checkpoint: dict | None = None
    invalidated_sections: set[str] = set()
    seen: set[str] = set()
    source_job_id = str(context.retry_of_job_id or "")
    for _depth in range(16):
        if not source_job_id or source_job_id in seen:
            break
        seen.add(source_job_id)
        source_job = context.repository.get_job(context.user_id, source_job_id)
        if source_job is None:
            break
        source_result = dict(source_job.result or {})
        source_checkpoint = source_result.get("section_checkpoint")
        if isinstance(source_checkpoint, dict):
            if checkpoint is None:
                checkpoint = deepcopy(source_checkpoint)
            invalidated_sections.update(
                str(section_id)
                for section_id in (
                    source_checkpoint.get("rejected_entries") or {}
                )
                if str(section_id)
            )
            invalidated_sections.update(
                str(section_id)
                for section_id in (
                    source_checkpoint.get("invalidated_section_ids") or []
                )
                if str(section_id)
            )
        invalidated_sections.update(
            str(item.get("section_id") or "")
            for item in (
                (source_result.get("section_progress") or {}).get(
                    "failed_sections"
                )
                or []
            )
            if isinstance(item, dict) and str(item.get("section_id") or "")
        )
        if isinstance(source_checkpoint, dict):
            for section_id, entry in (
                source_checkpoint.get("entries") or {}
            ).items():
                if (
                    str(section_id) not in invalidated_sections
                    and section_id not in merged_entries
                    and isinstance(entry, dict)
                ):
                    merged_entries[str(section_id)] = deepcopy(entry)
        source_fact_checkpoint = source_result.get("matrix_enrichment_checkpoint")
        if fact_repair_checkpoint is None and isinstance(
            source_fact_checkpoint, dict
        ):
            fact_repair_checkpoint = deepcopy(source_fact_checkpoint)
        source_job_id = str(source_job.retry_of_job_id or "")
    if checkpoint is not None:
        checkpoint["entries"] = merged_entries
        # A user-triggered retry is a new attempt; old failures must not keep
        # their sections in the deferred set. Completed chapters remain reusable.
        checkpoint["failed_sections"] = []
    return checkpoint, fact_repair_checkpoint


def register_planning_handlers(planning_service, job_service, handlers: Mapping[str, Callable]) -> None:
    if (topic_builder := dict(handlers or {}).get("planning.topic-outline")) is not None:
        job_service.register_handler("planning.topic-outline", topic_builder)
    enrichment_builder = dict(handlers or {}).get("matrix.enrich")
    blueprint_builder = dict(handlers or {}).get("planning.blueprint")
    if blueprint_builder is not None:
        def blueprint_handler(context, payload):
            payload = dict(payload)
            principal = Principal(context.user_id, frozenset({Role.USER}))
            # Revalidate deterministic inputs before paying for model calls.
            payload = planning_service.resume_blueprint_after_matrix(principal, str(context.project_id), payload)
            planning_service.validate_prepared_blueprint(principal, str(context.project_id), payload)
            integration = dict(payload.get("integrated_fact_enrichment") or {})
            fact_result = {
                "status": "not_requested",
                "mode": str(integration.get("mode") or ""),
                "failure_policy": str(integration.get("failure_policy") or ""),
            }
            if integration.get("enabled") and enrichment_builder is not None:
                fact_payload = planning_service.matrix_enrichment_payload(
                    principal, str(context.project_id),
                    classification_contract_override=selected_formal_classification_contract(
                        payload.get("outline_snapshot")
                    ),
                )
                expected_matrix = str(
                    integration.get("source_matrix_artifact_id")
                    or payload["section_blueprint"].get("source_matrix_artifact_id")
                    or ""
                )
                if str(fact_payload.get("source_matrix_artifact_id") or "") != expected_matrix:
                    raise WorkflowConflict(
                        "Matrix changed before integrated scientific fact analysis started."
                    )
                pending_fact_count = int(
                    fact_payload.get("pending_paper_count") or 0
                )
                if pending_fact_count and has_fact_sources(fact_payload):
                    saved_facts = payload.get("matrix_enrichment_checkpoint")
                    if isinstance(saved_facts, dict):
                        fact_payload["resume_checkpoint"] = saved_facts
                    if context.retry_of_job_id:
                        previous_job = context.repository.get_job(
                            context.user_id, context.retry_of_job_id
                        )
                        previous_result = (
                            dict(previous_job.result or {})
                            if previous_job is not None
                            else {}
                        )
                        saved_checkpoint = previous_result.get(
                            "matrix_enrichment_checkpoint"
                        )
                        if isinstance(saved_checkpoint, dict):
                            fact_payload["resume_checkpoint"] = saved_checkpoint
                    context.report_partial_result(
                        {
                            "planning_pipeline": {
                                "phase": "fact_enrichment",
                                "fact_current": 0,
                                "fact_total": pending_fact_count,
                            }
                        }
                    )
                    try:
                        built_facts = enrichment_builder(context, fact_payload)
                        candidate = planning_service.publish_matrix_enrichment(
                            principal,
                            str(context.project_id),
                            fact_payload,
                            built_facts,
                            candidate_only=True,
                        )
                        payload["matrix_snapshot"] = candidate["matrix_snapshot"]
                        # Keep retrieval-only planning context separate from
                        # the Matrix candidate that may later be promoted.
                        # Otherwise an enriched candidate either loses the
                        # fallback passages prepared for papers without a
                        # reliable abstract, or persists those transient
                        # passages as Matrix business data.
                        planning_rows = {
                            str(row.get("paper_id") or ""): row
                            for row in (
                                payload.get("planning_matrix_snapshot") or {}
                            ).get("rows")
                            or []
                            if isinstance(row, dict)
                        }
                        planning_matrix = deepcopy(candidate["matrix_snapshot"])
                        for row in planning_matrix.get("rows") or []:
                            source = planning_rows.get(
                                str(row.get("paper_id") or "")
                            )
                            if source is None:
                                continue
                            if "abstract" in source:
                                row["abstract"] = deepcopy(source["abstract"])
                            if source.get("planning_source_passages"):
                                row["planning_source_passages"] = deepcopy(
                                    source["planning_source_passages"]
                                )
                        payload["planning_matrix_snapshot"] = planning_matrix
                        fact_result = {
                            "status": "completed",
                            "mode": integration.get("mode"),
                            "pending_paper_count": pending_fact_count,
                            "changed_paper_ids": candidate.get(
                                "changed_paper_ids"
                            )
                            or [],
                            "refreshed_paper_ids": candidate.get(
                                "refreshed_paper_ids"
                            )
                            or [],
                            "fact_enrichment_summary": candidate.get(
                                "fact_enrichment_summary"
                            )
                            or {},
                            "published_with_blueprint_confirmation": True,
                        }
                    except ScientificRunError as exc:
                        # Fact cards improve planning, but the registered source
                        # passages remain the scientific authority and keep the
                        # stage usable during a provider outage.
                        logger.warning(
                            "Integrated fact analysis unavailable; continuing with source passages",
                            extra={
                                "project_id": str(context.project_id),
                                "job_id": str(context.job_id),
                                "error_type": type(exc).__name__,
                            },
                        )
                        fact_result = {
                            "status": "degraded",
                            "mode": integration.get("mode"),
                            "pending_paper_count": pending_fact_count,
                            "reason": "fact_provider_unavailable",
                            "continued_with_source_passages": True,
                        }
                elif pending_fact_count:
                    fact_result = {
                        "status": "degraded",
                        "mode": integration.get("mode"),
                        "pending_paper_count": pending_fact_count,
                        "reason": "fulltext_fact_sources_unavailable",
                        "continued_with_source_passages": True,
                    }
                else:
                    fact_result = {
                        "status": "current",
                        "mode": integration.get("mode"),
                        "pending_paper_count": 0,
                    }
                context.report_partial_result(
                    {
                        "integrated_fact_enrichment": fact_result,
                        "planning_pipeline": {"phase": "chapter_planning"},
                    }
                )
            if context.retry_of_job_id:
                previous = context.repository.get_job(context.user_id, context.retry_of_job_id)
                if previous:
                    payload["blueprint_checkpoint"] = (previous.result or {}).get("blueprint_checkpoint") or {}
            planning_service.reconcile_blueprint_facts(principal, str(context.project_id), payload)
            context.report_progress(0, len(payload["section_blueprint"]["sections"]))
            built = blueprint_builder(context, payload)
            context.checkpoint()
            # Publish the candidate's actual input chain; activation belongs to confirmation.
            result = planning_service.publish_blueprint_candidate(principal, str(context.project_id), built)
            checkpoint = built.get("blueprint_checkpoint") or {}
            checkpoint.update(source_matrix_artifact_id=payload["section_blueprint"]["source_matrix_artifact_id"],
                working_matrix_artifact_id=result["section_blueprint"]["source_matrix_artifact_id"])
            return {
                **result,
                "blueprint_checkpoint": checkpoint,
                "integrated_fact_enrichment": fact_result,
                "planning_pipeline": {"phase": "completed"},
            }
        job_service.register_handler("planning.blueprint", blueprint_handler)
    if enrichment_builder is not None:

        def matrix_enrichment_handler(context, payload):
            payload = dict(payload)
            resume_from_job_id = context.retry_of_job_id or payload.get("resume_from_job_id")
            principal = Principal(context.user_id, frozenset({Role.USER}))
            current_job = context.repository.get_job(context.user_id, context.job_id)
            current_result = current_job.result if current_job is not None else None
            wait_for_delegated_model(context, current_result)
            current_checkpoint = (current_result.get("matrix_enrichment_checkpoint")
                                  if isinstance(current_result, dict) else None)
            if payload.get("prepare_on_start"):
                request = payload
                expected_artifact_id = str(
                    payload.get("source_matrix_artifact_id") or
                    (current_checkpoint or {}).get("source_matrix_artifact_id") or ""
                )
                planning_service.validate_matrix_enrichment_inputs(principal, str(context.project_id), payload)
                payload = (planning_service.fact_revision_payload(principal, str(context.project_id),
                    source_matrix_artifact_id=expected_artifact_id) if payload.get("operation") == "fact_revision"
                    else planning_service.matrix_enrichment_payload(principal, str(context.project_id),
                        force=bool(request.get("force_refresh")),
                        selected_paper_ids=request.get("selected_paper_ids") or None))
                if (
                    expected_artifact_id
                    and payload.get("source_matrix_artifact_id") != expected_artifact_id
                ):
                    raise WorkflowConflict(
                        "Matrix changed before scientific fact extraction started."
                    )
            planning_service.validate_matrix_enrichment_inputs(principal, str(context.project_id), payload)
            if isinstance(current_checkpoint, dict):
                payload["resume_checkpoint"] = current_checkpoint
            elif resume_from_job_id:
                source_job = context.repository.get_job(
                    context.user_id, resume_from_job_id
                )
                source_result = (source_job.result or {}) if source_job is not None else {}
                checkpoint = source_result.get("matrix_enrichment_checkpoint")
                if not isinstance(checkpoint, dict):
                    # The progress callback persists checkpoints before the
                    # handler publishes its final result.  A publish conflict
                    # therefore leaves the scientifically useful checkpoint
                    # under this generic progress key.
                    checkpoint = source_result.get("section_checkpoint")
                if isinstance(checkpoint, dict):
                    payload["resume_checkpoint"] = checkpoint
            total = int(payload.get("pending_paper_count") or 0)
            resumed = len((payload.get("resume_checkpoint") or {}).get("completed_papers") or [])
            context.report_progress(min(resumed, total), total)
            if not total:
                return {
                    "project_id": str(context.project_id),
                    "status": "current",
                    "matrix_artifact_id": payload.get("source_matrix_artifact_id"),
                    "message": "Matrix scientific facts are already current.",
                }
            if not has_fact_sources(payload):
                raise WorkflowConflict("Build full-text indexes before extracting Matrix scientific facts.")
            try:
                built = enrichment_builder(context, payload)
            except ScientificModelDeferred as exc:
                yield_to_delegated_model(context, exc)
            context.checkpoint()
            checkpoint = built.get("matrix_enrichment_checkpoint") if isinstance(built, dict) else None
            if isinstance(checkpoint, dict) and "completed_papers" in checkpoint:
                expected_papers = {str(row.get("paper_id") or "") for row in payload.get("papers") or []}
                completed_papers = set(checkpoint.get("completed_papers") or [])
                if expected_papers - completed_papers:
                    context.report_partial_result({"matrix_enrichment_checkpoint": checkpoint})
                    context.report_progress(len(expected_papers & completed_papers), total)
                    raise JobYieldRequested()
            result = planning_service.publish_matrix_enrichment(
                principal, str(context.project_id), payload, built
            )
            report_committed_progress(context, total, total)
            return result

        job_service.register_handler("matrix.enrich", matrix_enrichment_handler)


def queue_fact_revision(planning_service, job_service, principal, project_id, saved):
    """Saving and queuing are separate transactions. Never pretend an enqueue failure lost the edit."""
    if not saved.get("pending_fact_revisions"):
        return saved
    try:
        job = job_service.submit(principal, scope="project", project_id=project_id, job_type="matrix.enrich",
            idempotency_key="fact-revision:" + saved["matrix_artifact_id"], operation_key="matrix-enrichment",
            payload={"prepare_on_start": True, "operation": "fact_revision",
                     "source_matrix_artifact_id": saved["matrix_artifact_id"]})
        return {**saved, "fact_verification": {"status": "queued", "job_id": str(job.id)}}
    except Exception as exc:
        logger.warning("fact_verification_enqueue_failed", extra={"project_id": project_id,
            "artifact_id": saved["matrix_artifact_id"], "error_type": type(exc).__name__})
        return {**saved, "fact_verification": {"status": "pending", "retryable": True,
            "message": "Facts were saved, but verification could not be queued. Continue scientific fact extraction to retry."}}


def register_sections_handler(sections_service, job_service, handlers: Mapping[str, Callable]) -> None:
    builder = dict(handlers or {}).get("sections.generate")
    if builder is not None:

        def section_handler(context, payload):
            payload = dict(payload)
            principal = Principal(context.user_id, frozenset({Role.USER}))
            sections_service.validate_generation_inputs(principal, str(context.project_id), payload)
            if payload.get("evidence_preparation_pending") is True:
                payload = sections_service.prepare_generation_job(context, payload)
            # Retries carry the original task snapshot. Drop only obsolete
            # standalone conclusions; keep body inputs/checkpoints unchanged.
            payload["tasks"] = [task for task in payload.get("tasks") or []
                                if str(task.get("section_role") or "").strip().casefold() != "conclusion"]
            if context.retry_of_job_id:
                checkpoint, fact_repair_checkpoint = section_retry_checkpoints(
                    context
                )
                if isinstance(checkpoint, dict):
                    payload["resume_checkpoint"] = checkpoint
                if isinstance(fact_repair_checkpoint, dict):
                    payload["fact_repair_checkpoint"] = fact_repair_checkpoint
            total = len(payload.get("tasks") or [])
            current_job = context.repository.get_job(context.user_id, context.job_id)
            current_result = current_job.result if current_job is not None else None
            wait_for_delegated_model(context, current_result)
            checkpoint = current_result.get("section_checkpoint") if isinstance(current_result, dict) else None
            entries = checkpoint.get("entries") if isinstance(checkpoint, dict) else None
            completed = len(entries) if isinstance(entries, dict) else 0
            context.report_progress(min(completed, total), total)
            try:
                built = builder(context, payload)
            except ScientificModelDeferred as exc:
                yield_to_delegated_model(context, exc)
            except ScientificRunError:
                # Only an explicit provider rate limit is safe to retry
                # automatically. A timeout may have produced a paid response
                # whose outcome is still unknown, so it remains user-visible.
                latest = context.repository.get_job(context.user_id, context.job_id)
                snapshot = latest.result if latest is not None and isinstance(latest.result, dict) else {}
                saved_checkpoint = snapshot.get("section_checkpoint")
                failures = ((saved_checkpoint or {}).get("failed_sections") or []) if isinstance(saved_checkpoint, dict) else []
                retry_count = int(snapshot.get("section_auto_retry_count") or 0)
                if failures and all(isinstance(row, dict) and row.get("retryable_rate_limit") is True
                                    for row in failures) and retry_count < 2:
                    progress = dict(snapshot.get("section_progress") or {})
                    progress["phase"] = "waiting_retry"
                    progress["failed_sections"] = failures
                    context.report_partial_result({
                        "section_checkpoint": {**saved_checkpoint, "failed_sections": []},
                        "section_progress": progress,
                        "section_auto_retry_count": retry_count + 1,
                    })
                    raise JobYieldRequested(delay_seconds=30 * (retry_count + 1),
                                            queue_reason="provider_rate_limit")
                raise
            except Exception as exc:
                if TRANSIENT_PROVIDER_ERROR.search(str(exc)):
                    raise SectionProviderUnavailable(
                        "The section-writing provider remained unavailable after the gateway exhausted its provider retries. Completed object checkpoints were preserved; retry this job to continue.",
                        details={"attempts": 1, "resume_supported": True},
                    ) from exc
                raise
            context.checkpoint()
            publication_payload = (
                built.get("_publication_payload")
                if isinstance(built, dict)
                and isinstance(built.get("_publication_payload"), dict)
                else payload
            )
            try:
                result = sections_service.publish_generation(
                    principal,
                    str(context.project_id),
                    publication_payload,
                    built,
                    attempts=1,
                )
            except WorkflowValidationError as exc:
                # Publication is a final safety net, not a reason to retain a
                # known-invalid "completed" entry and replay the same failure.
                job = context.repository.get_job(context.user_id, context.job_id)
                snapshot = dict(job.result or {}) if job is not None else {}
                checkpoint = snapshot.get("section_checkpoint")
                if isinstance(checkpoint, dict):
                    entries = dict(checkpoint.get("entries") or {})
                    target = str(exc.details.get("section_id") or "")
                    if target:
                        entries.pop(target, None)
                    entries, rejected = reusable_section_entries(entries, publication_payload.get("tasks") or [], {
                        row["section_id"]: row for row in (publication_payload.get("evidence_package") or {}).get("sections") or []
                    })
                    if target:
                        rejected[target] = str(exc)
                    progress = dict(snapshot.get("section_progress") or {})
                    progress.update({
                        "phase": "publication_failed",
                        "completed_sections": [row for row in progress.get("completed_sections") or []
                                               if row.get("section_id") in entries],
                        "failed_sections": [{"section_id": sid, "heading": next(
                            (task.get("heading") for task in publication_payload.get("tasks") or [] if task.get("section_id") == sid), sid),
                            "error": reason} for sid, reason in rejected.items()],
                    })
                    context.report_partial_result({
                        "section_checkpoint": {**checkpoint, "entries": entries, "rejected_entries": rejected},
                        "section_progress": progress,
                    })
                raise
            report_committed_progress(context, total, total)
            return result

        job_service.register_handler("sections.generate", section_handler)
