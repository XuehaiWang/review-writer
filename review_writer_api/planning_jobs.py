"""One submission path for automatic and explicitly requested fact extraction."""

from review_writer_api.errors import WorkflowConflict

ACTIVE = {"queued", "running", "cancel_requested"}


def queue_matrix_enrichment(planning, jobs, principal, project_id, *, force=False,
                            paper_ids=None, idempotency_key=""):
    planning._owned_project(principal, project_id)
    _, artifact = planning._matrix(principal, project_id)
    current = jobs.repository.get_current_job(
        principal.user_id, scope="project", project_id=project_id, job_type="matrix.enrich",
        operation_key="matrix-enrichment")
    if current and current.status in ACTIVE:
        if current.payload.get("source_matrix_artifact_id") == artifact.id:
            return current
        raise WorkflowConflict("Earlier Matrix analysis is still running. Wait for it to finish before analyzing the updated selection.")
    # Source preparation and cache validation belong to the worker, not the
    # confirmation request. A reload/GET must never initiate paid work.
    payload = {"prepare_on_start": True, "source_matrix_artifact_id": artifact.id,
               "force_refresh": bool(force), "selected_paper_ids": sorted(set(paper_ids or []))}
    if (current and not force and current.payload.get("source_matrix_artifact_id") == artifact.id
            and any(isinstance((current.result or {}).get(key), dict)
                    for key in ("matrix_enrichment_checkpoint", "section_checkpoint"))):
        payload["resume_from_job_id"] = current.id
    key = idempotency_key or f"matrix:{artifact.id}:{int(force)}:{','.join(payload['selected_paper_ids'])}"
    try:
        return jobs.submit(principal, scope="project", project_id=project_id,
            job_type="matrix.enrich", operation_key="matrix-enrichment",
            idempotency_key=key, payload=payload)
    except WorkflowConflict as exc:
        current_id = exc.details.get("current_job_id")
        current = jobs.status(principal, current_id) if current_id else None
        if current and current.status in ACTIVE and all(current.payload.get(k) == v for k, v in payload.items()):
            return current
        raise
