"""Missing topic recommendations use persisted jobs, never mutate selected outlines."""
import hashlib
import json
import uuid

from sqlalchemy import select
from review_writer_api.database import AIModelRequest, database_session

from review_writer_api.job_service import job_payload
from review_writer_api.errors import WorkflowConflict
from review_writer_core.stages.planning.topic_recommendation import normalize_recommended_outline


class TopicRecommendationMixin:
    def topic_recommendation_input(self, principal, project_id):
        matrix, _ = self._matrix(principal, project_id)
        project = self._owned_project(principal, project_id)
        rows = matrix.get("rows") or []
        per_paper = max(300, min(6000, 120000 // max(1, len(rows))))
        papers = [{"paper_id": r["paper_id"], "title": r.get("title"),
                   "evidence": "\n".join(filter(None, [self._matrix_abstract(r), str(r.get("main_content") or "")]))[:per_paper]}
                  for r in rows]
        data = {"topic": matrix.get("review_topic") or project.topic, "papers": papers}
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {**data, "input_fingerprint": fingerprint}

    def topic_recommendation_status(self, principal, project_id, inputs=None):
        inputs = inputs or self.topic_recommendation_input(principal, project_id)
        jobs = self.repository.list_project_jobs(principal.user_id, project_id,
            job_type="planning.topic-outline", limit=100)
        current = next((j for j in jobs if j.payload.get("input_fingerprint") == inputs["input_fingerprint"]), None)
        current = current or next((j for j in jobs if j.status in {"queued", "running", "cancel_requested"}), None)
        ready = next((j for j in jobs if j.status == "succeeded" and j.result.get("outline_md")
                      and j.payload.get("input_fingerprint") == inputs["input_fingerprint"]), None)
        previous = next((j for j in jobs if j.status == "succeeded" and j.result.get("outline_md")), None)
        model_progress = None
        if current and current.status in {"queued", "running", "cancel_requested"}:
            with database_session(self.repository.session_factory) as session:
                request = session.scalar(select(AIModelRequest).where(
                    AIModelRequest.job_id == uuid.UUID(str(current.id)),
                    AIModelRequest.user_id == uuid.UUID(str(principal.user_id)),
                ).order_by(AIModelRequest.created_at.desc()).limit(1))
                if request:
                    model_progress = {"attempt": request.attempt_count,
                                      "status": request.status, "model": request.model_name}
        return {"input_fingerprint": inputs["input_fingerprint"],
                "model_progress": model_progress,
                "status": current.status if current else "not_started" if inputs["papers"] else "waiting_for_papers",
                "job": job_payload(current) if current else None,
                "candidate": {**ready.result, "outline_md": normalize_recommended_outline(ready.result["outline_md"])} if ready else None,
                "previous_outline_md": previous.result["outline_md"] if previous and not ready else ""}

    def require_topic_recommendation(self, principal, project_id):
        candidate = self.topic_recommendation_status(principal, project_id)["candidate"]
        if not candidate:
            raise WorkflowConflict("Generate the topic recommendation first. The existing outline was not changed.")
        return candidate
