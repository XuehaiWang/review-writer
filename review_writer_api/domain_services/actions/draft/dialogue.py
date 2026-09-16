"""Paragraph dialogue on existing persisted jobs and immutable candidate artifacts."""
from review_writer_core.provider_errors import public_model_error
import json
import uuid

from review_writer_api.database import utc_now, database_session, AIModelRequest
from sqlalchemy import select
from review_writer_api.errors import WorkflowConflict, WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission
from review_writer_core.paragraph_revision import exact_hash, paragraph_keys, dialogue_sections
from review_writer_core.dialogue_memory import conversation_memory, candidate_response
from review_writer_core.draft_quality import QUALITY_INPUT_ARTIFACTS
from review_writer_core.draft_bibliography import citation_entries_from_draft
from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT, DRAFT_REWRITE_CANDIDATES


from .section_versions import SectionVersionsMixin


class DraftDialogueMixin(SectionVersionsMixin):
    def section_stream_snapshot(self, principal, project_id, section_id, job_id):
        self._owned_project(principal, project_id)
        job = self.repository.get_job(principal.user_id, job_id)
        if (not job or job.project_id != project_id or job.job_type != "draft.optimize"
                or job.payload.get("section_id") != section_id):
            raise WorkflowNotFound("Chapter conversation not found.")
        with database_session(self.repository.session_factory) as session:
            row = session.scalar(select(AIModelRequest).where(
                AIModelRequest.user_id == uuid.UUID(principal.user_id),
                AIModelRequest.project_id == uuid.UUID(project_id),
                AIModelRequest.job_id == uuid.UUID(job_id),
            ).order_by(AIModelRequest.created_at.desc()).limit(1))
            data = (row.response_json or {}) if row else {}
            phase = "queued"
            if row:
                phase = "scope" if row.stage == "Chapter conversation scope" else "answer"
                if row.status == "succeeded":
                    phase = "reading" if row.stage == "Chapter conversation scope" else "checking"
            return {"id": job.id, "status": job.status, "phase": phase,
                "streaming_reply": data.get("partial_reply", ""),
                "stream_diagnostics": data.get("stream_diagnostics", {}),
                "progress_current": job.progress_current, "progress_total": job.progress_total,
                "error_message": public_model_error(job.error_message or "")}

    def section_conversation_memory(self, principal, project_id, section, turns, *, exclude_job_id="", branch_id=""):
        store, _ = self._read_json(principal, project_id, DRAFT_REWRITE_CANDIDATES, required=False)
        hashes = {p["paragraph_key"]: p["text_sha256"] for p in section["paragraphs"]}
        records = {t["id"]: {"id": t["id"], "created_at": t["created_at"], "user": t["message"],
            "task_status": t["status"], "responses": []} for t in turns if t["id"] != exclude_job_id and t.get("branch_id", "") == branch_id}
        jobs = self.repository.list_project_jobs(principal.user_id, project_id, job_type="draft.rewrite", limit=500)
        for job in reversed(jobs):
            request = job.payload.get("dialogue") or {}
            if request.get("paragraph_key") in hashes and job.id != exclude_job_id and request.get("branch_id", "") == branch_id:
                turn_id = request.get("turn_id") or job.id
                records[turn_id] = {"id": turn_id, "created_at": job.created_at.isoformat(),
                    "user": request.get("message", ""), "task_status": job.status, "responses": []}
        for entry in (store.get("entries") or {}).values():
            if entry.get("paragraph_key") not in hashes or entry.get("revision_mode") != "dialogue" or entry.get("branch_id", "") != branch_id:
                continue
            turn_id = entry.get("batch_job_id") or entry["candidate_id"]
            if turn_id == exclude_job_id:
                continue
            record = records.setdefault(turn_id, {"id": turn_id, "created_at": entry["created_at"],
                "user": entry.get("message", ""), "task_status": "completed", "responses": []})
            record["responses"].append(candidate_response(entry, hashes[entry["paragraph_key"]]))
        records = sorted(records.values(), key=lambda r: (r["created_at"], r["id"]))
        previous = self.repository.list_project_jobs(principal.user_id, project_id, job_type="draft.optimize",
            operation_key=f"section:{section['section_id']}", limit=1)
        cached = (previous[0].payload.get("section_context") or {}).get("conversation_memory") if previous and previous[0].payload.get("branch_id", "") == branch_id else None
        return conversation_memory(records, cached)

    def draft_dialogue_sections(self, principal, project_id):
        self._owned_project(principal, project_id)
        text, artifact = self._read_text(principal, project_id, DRAFT_MANUSCRIPT)
        return dialogue_sections(text, artifact.metadata, artifact.id)

    def section_dialogue_history(self, principal, project_id, section_id):
        sections = self.draft_dialogue_sections(principal, project_id)
        section = next((s for s in sections if s["section_id"] == section_id), None)
        if section is None:
            raise WorkflowNotFound("This section no longer belongs to the current Draft.")
        jobs = self.repository.list_project_jobs(principal.user_id, project_id,
            job_type="draft.optimize", operation_key=f"section:{section_id}", limit=100)
        current_keys = {p["paragraph_key"] for p in section["paragraphs"]}
        jobs = [j for j in jobs if set((j.payload.get("section_input") or {}).get("base_hashes") or {}) == current_keys]
        live = [uuid.UUID(j.id) for j in jobs if j.status in {"queued", "running"}]
        streaming = {}
        if live:
            with database_session(self.repository.session_factory) as session:
                rows = session.scalars(select(AIModelRequest).where(
                    AIModelRequest.user_id == uuid.UUID(principal.user_id),
                    AIModelRequest.project_id == uuid.UUID(project_id), AIModelRequest.job_id.in_(live),
                    AIModelRequest.status == "running",
                ).order_by(AIModelRequest.created_at)).all()
                for row in rows:
                    streaming[str(row.job_id)] = str((row.response_json or {}).get("partial_reply") or "")
        return {"section": section, "turns": [{"id": j.id, "status": j.status,
            "streaming_reply": streaming.get(j.id, ""),
            "branch_id": j.payload.get("branch_id", ""), "initial_artifact_id": j.payload.get("initial_artifact_id", ""), "message": j.payload.get("message", ""), "action": j.payload.get("action", "discuss"), "created_at": j.created_at.isoformat(),
            "progress_current": j.progress_current, "progress_total": j.progress_total or len(j.payload.get("paragraphs") or []),
            "result": j.result, "error_message": j.error_message} for j in reversed(jobs)]}

    def section_dialogue_payload(self, principal, project_id, section_id, *, message,
                                 base_hashes, idempotency_key, action="discuss", branch_id="", initial_artifact_id=""):
        principal.require(Permission.PROJECT_WRITE)
        self._owned_project(principal, project_id)
        if action not in {"discuss", "revise"}:
            raise WorkflowValidationError("Unknown chapter conversation action.")
        request = dict(message=message, base_hashes=base_hashes, action=action, branch_id=branch_id, initial_artifact_id=initial_artifact_id)
        existing = self.repository.list_project_jobs(principal.user_id, project_id,
            job_type="draft.optimize", operation_key=f"section:{section_id}", idempotency_key=idempotency_key, limit=1)
        if existing:
            if existing[0].payload.get("section_input") != request:
                raise WorkflowConflict("This request key was used for a different message.")
            return None, existing[0]
        sections = self.draft_dialogue_sections(principal, project_id)
        section = next((s for s in sections if s["section_id"] == section_id), None)
        if section is None:
            raise WorkflowNotFound("This section no longer belongs to the current Draft.")
        paragraphs = section["paragraphs"]
        if base_hashes != {p["paragraph_key"]: p["text_sha256"] for p in paragraphs}:
            raise WorkflowConflict("This section changed. Review the latest text before sending.")
        _, artifact = self._read_text(principal, project_id, DRAFT_MANUSCRIPT)
        if self._freshness(principal, project_id, artifact)["upstream_stale"]:
            raise WorkflowConflict("Draft sources changed; refresh the current Draft first.")
        history = self.section_dialogue_history(principal, project_id, section_id)["turns"]
        if bool(branch_id) != bool(initial_artifact_id):
            raise WorkflowValidationError("An initial-version discussion requires both its branch and version.")
        initial = self.initial_section_for_restart(principal, project_id, section_id, initial_artifact_id) if initial_artifact_id else None
        for turn in history:
            if turn.get("branch_id") == branch_id and branch_id:
                previous = self.repository.get_job(principal.user_id, turn["id"])
                if previous.payload.get("initial_artifact_id") != initial_artifact_id:
                    raise WorkflowConflict("This discussion branch belongs to another initial version.")
        memory = self.section_conversation_memory(principal, project_id, section, history, branch_id=branch_id)
        return {"project_id": project_id, "revision_mode": "dialogue_batch", "section_id": section_id,
            "section_input": request, "message": message, "action": action,
            "branch_id": branch_id, "initial_artifact_id": initial_artifact_id,
            "paragraphs": paragraphs,
            "section_context": {"title": section["title"],
                "paragraphs": [{"paragraph_id": p["paragraph_id"], "text": p["text"]} for p in (initial or section)["paragraphs"]],
                "outline": [{"section_id": s["section_id"], "title": s["title"],
                    "saved_opening_excerpt": s["paragraphs"][0]["text"][:500]} for s in sections],
                "conversation_memory": memory}}, None

    def dialogue_paragraph(self, principal, project_id, key):
        text, artifact = self._read_text(principal, project_id, DRAFT_MANUSCRIPT)
        keys = paragraph_keys(text, artifact.metadata, artifact.id)
        paragraph = next((p for p in self._paragraph_spans(text) if keys[p["paragraph_id"]] == key), None)
        if paragraph is None:
            raise WorkflowNotFound("This paragraph no longer belongs to the current Draft.")
        return text, artifact, paragraph

    def dialogue_history(self, principal, project_id, key):
        self._owned_project(principal, project_id)
        jobs = self.repository.list_project_jobs(principal.user_id, project_id,
            job_type="draft.rewrite", operation_key=f"paragraph:{key}", limit=500)
        store, _ = self._read_json(principal, project_id, DRAFT_REWRITE_CANDIDATES, required=False)
        entries = store.get("entries") or {}
        try:
            _, artifact, paragraph = self.dialogue_paragraph(principal, project_id, key)
            current_hash = exact_hash(paragraph["text"])
            archived = False
        except WorkflowNotFound:
            current_hash, archived = "", True
        messages = []
        seen = set()
        for job in jobs:
            request = job.payload.get("dialogue") or {}
            if request.get("turn_id") in seen:
                continue
            seen.add(request.get("turn_id"))
            entry = dict(entries.get(request.get("turn_id")) or {})
            if entry.get("status") == "pending" and entry.get("base_text_sha256") != current_hash:
                entry["status"] = "stale"
            if entry:
                entry["context_paragraph_ids"] = list(entry.get("context_hashes") or {})
            messages.append({"job_id": job.id, "status": job.status, "error": job.error_message,
                "created_at": job.created_at.isoformat(), "message": request.get("message", ""),
                "candidate": entry or None})
        represented = {(m["candidate"] or {}).get("candidate_id") for m in messages}
        for value in entries.values():
            if value.get("paragraph_key") == key and value.get("candidate_id") not in represented:
                entry = dict(value)
                if entry.get("status") == "pending" and entry.get("base_text_sha256") != current_hash:
                    entry["status"] = "stale"
                messages.append({"job_id": entry.get("batch_job_id") or entry["candidate_id"], "status": "succeeded",
                    "error": "", "created_at": entry["created_at"], "message": entry.get("message", ""), "candidate": entry})
        messages.sort(key=lambda m: m["created_at"])
        return {"paragraph_key": key, "archived": archived, "messages": messages}

    def dialogue_payload(self, principal, project_id, key, *, message, base_text_sha256,
                         parent_candidate_id="", use_saved=False, context_keys=None, preferences="", idempotency_key,
                         include_memory=True, branch_id=""):
        principal.require(Permission.PROJECT_WRITE)
        existing = self.repository.list_project_jobs(principal.user_id, project_id,
            job_type="draft.rewrite", operation_key=f"paragraph:{key}", idempotency_key=idempotency_key, limit=1)
        request_input = {"message": message, "base_text_sha256": base_text_sha256,
                         "parent_candidate_id": parent_candidate_id, "use_saved": use_saved,
                         "context_keys": context_keys or [], "preferences": preferences, "branch_id": branch_id}
        if existing:
            if existing[0].payload.get("dialogue_input") != request_input:
                raise WorkflowConflict("This request key was used for a different message.")
            return None, existing[0]
        text, artifact, paragraph = self.dialogue_paragraph(principal, project_id, key)
        if self._freshness(principal, project_id, artifact)["upstream_stale"]:
            raise WorkflowConflict("Draft sources changed; refresh the current Draft first.")
        if exact_hash(paragraph["text"]) != base_text_sha256:
            raise WorkflowConflict("The saved paragraph changed. Refresh before sending this message.")
        history = self.dialogue_history(principal, project_id, key)["messages"]
        pending = [m["candidate"] for m in history if m["candidate"] and m["candidate"].get("status") == "pending" and m["candidate"].get("branch_id", "") == branch_id]
        parent = next((c for c in pending if c["candidate_id"] == parent_candidate_id), None) if parent_candidate_id else (pending[-1] if pending and not use_saved else None)
        if parent_candidate_id and not parent:
            raise WorkflowConflict("The selected candidate is no longer available for this discussion.")
        siblings = self._paragraph_spans(text)
        position = next(i for i, p in enumerate(siblings) if p["paragraph_id"] == paragraph["paragraph_id"])
        neighbours = siblings[max(0, position-1):position] + siblings[position+1:position+2]
        keys = paragraph_keys(text, artifact.metadata, artifact.id)
        selected = set(context_keys or [])
        if not selected.issubset(set(keys.values())):
            raise WorkflowValidationError("A requested context paragraph is outside this Draft.")
        neighbours = list({p["paragraph_id"]: p for p in [*neighbours, *[
            p for p in siblings if keys[p["paragraph_id"]] in selected and keys[p["paragraph_id"]] != key]]}.values())
        related = [{"paragraph_id": p["paragraph_id"], "text": p["text"][:4000]} for p in neighbours]
        topic = self._owned_project(principal, project_id).topic
        dialogue = {"turn_id": str(uuid.uuid4()), "paragraph_key": key, "paragraph_id": paragraph["paragraph_id"],
            "message": message, "topic": topic, "branch_id": branch_id, "discussion_text": parent["candidate_text"] if parent else paragraph["text"],
            "preferences": preferences,
            "parent_candidate_id": parent["candidate_id"] if parent else "", "base_text_sha256": base_text_sha256,
            "context": related, "context_hashes": {p["paragraph_id"]: exact_hash(p["text"]) for p in neighbours}}
        if include_memory:
            dialogue["conversation_memory"] = conversation_memory([{"id": (m["candidate"] or {}).get("candidate_id") or m["job_id"],
                "user": m["message"], "responses": [candidate_response(m["candidate"], base_text_sha256)] if m["candidate"] else [],
                "task_status": m["status"]} for m in history])
        compatibility = self.compatibility_payload(principal, project_id)
        return {**compatibility, "revision_mode": "dialogue", "project_id": project_id,
            "citation_identity": citation_entries_from_draft(text, compatibility.get("section_index") or {}),
            "paragraph_id": paragraph["paragraph_id"], "paragraph_text": paragraph["text"],
            "draft_text": text, "source_draft_artifact_id": artifact.id,
            "dialogue": dialogue, "dialogue_input": request_input}, None

    def validate_dialogue_inputs(self, principal, project_id, payload):
        key = payload["dialogue"]["paragraph_key"]
        _, artifact, paragraph = self.dialogue_paragraph(principal, project_id, key)
        if self._freshness(principal, project_id, artifact)["upstream_stale"]:
            raise WorkflowConflict("Draft sources changed while preparing this revision.")
        if exact_hash(paragraph["text"]) != payload["dialogue"]["base_text_sha256"]:
            raise WorkflowConflict("This paragraph changed. The saved text was not overwritten.")
        expected = self.validate_artifact_inputs(principal, project_id, payload, {
            field: name for field, name in QUALITY_INPUT_ARTIFACTS.items()
            if field != "source_rewrite_overlay_artifact_id"})
        return {**expected, DRAFT_MANUSCRIPT: artifact.id}

    def publish_dialogue(self, principal, project_id, payload, built):
        request = payload["dialogue"]
        for attempt in range(3):
            revision = self._revision(principal, project_id)
            store, store_artifact = self._read_json(principal, project_id, DRAFT_REWRITE_CANDIDATES, required=False)
            entries = dict(store.get("entries") or {})
            turn_id = request["turn_id"]
            if turn_id in entries:
                return {"candidate_id": turn_id, "reply": entries[turn_id].get("reply", "")}
            status = "pending" if built.get("outcome") == "candidate" and built.get("candidate_text") else "kept_original"
            if built.get("validation_errors"):
                status = "validation_failed"
            try:
                self.validate_dialogue_inputs(principal, project_id, payload)
            except (WorkflowConflict, WorkflowNotFound):
                status = "stale"
            parent = request.get("parent_candidate_id")
            if parent and (entries.get(parent) or {}).get("status") != "pending":
                status = "stale"
            entries[turn_id] = {**built, "candidate_id": turn_id, "revision_mode": "dialogue",
                "branch_id": request.get("branch_id", ""), "message": request["message"], "batch_job_id": request.get("batch_job_id", ""),
                "paragraph_key": request["paragraph_key"], "paragraph_id": payload["paragraph_id"],
                "base_text_sha256": request["base_text_sha256"], "original_text": payload["paragraph_text"],
                "discussion_text": request["discussion_text"], "parent_candidate_id": parent,
                "source_draft_artifact_id": payload["source_draft_artifact_id"],
                "source_inputs": {field: payload[field] for field in QUALITY_INPUT_ARTIFACTS
                                  if field in payload and field != "source_rewrite_overlay_artifact_id"},
                "context_hashes": request["context_hashes"], "status": status, "created_at": utc_now().isoformat()}
            try:
                _, state = self._publish_files(principal, project_id,
                    {DRAFT_REWRITE_CANDIDATES: ((json.dumps({"entries": entries}, ensure_ascii=False)+"\n").encode(), "json")},
                    expected_revision=revision, expected_current_artifacts={DRAFT_REWRITE_CANDIDATES: store_artifact.id if store_artifact else ""},
                    invalidate_final=False, metadata={"operation": "paragraph-dialogue"})
                return {"candidate_id": turn_id, "reply": built.get("reply", ""), "revision": state.revision}
            except WorkflowConflict:
                if attempt == 2:
                    raise

    def decide_dialogue(self, principal, project_id, candidate_id, *, decision):
        principal.require(Permission.PROJECT_WRITE)
        if decision not in {"accept", "reject"}:
            raise WorkflowValidationError("Unknown candidate decision.")
        for attempt in range(3):
            revision = self._revision(principal, project_id)
            store, store_artifact = self._read_json(principal, project_id, DRAFT_REWRITE_CANDIDATES)
            entries = dict(store.get("entries") or {})
            candidate = dict(entries.get(candidate_id) or {})
            if candidate.get("revision_mode") != "dialogue":
                raise WorkflowNotFound("Dialogue candidate not found.")
            target_status = "accepted" if decision == "accept" else "rejected"
            if candidate.get("status") == target_status:
                return {"decision": decision, "revision": revision}
            if candidate.get("status") != "pending":
                raise WorkflowConflict("This candidate is no longer pending.")
            files = {}
            expected = {DRAFT_REWRITE_CANDIDATES: store_artifact.id}
            metadata = {"operation": f"dialogue-{decision}"}
            if decision == "accept":
                text, artifact, paragraph = self.dialogue_paragraph(principal, project_id, candidate["paragraph_key"])
                if exact_hash(paragraph["text"]) != candidate["base_text_sha256"]:
                    raise WorkflowConflict("This paragraph changed. Your candidate has been retained for comparison.")
                if self._freshness(principal, project_id, artifact)["upstream_stale"]:
                    raise WorkflowConflict("Draft sources changed before candidate acceptance.")
                expected.update(self.validate_artifact_inputs(principal, project_id,
                    candidate.get("source_inputs") or {}, QUALITY_INPUT_ARTIFACTS))
                if candidate.get("validation_errors") or not candidate.get("candidate_text"):
                    raise WorkflowValidationError("This response has no safe candidate to accept.")
                updated = text[:paragraph["start"]] + candidate["candidate_text"] + text[paragraph["end"]:]
                if updated != text:
                    files[DRAFT_MANUSCRIPT] = (updated.encode(), "markdown")
                expected[DRAFT_MANUSCRIPT] = artifact.id
                metadata = {**artifact.metadata, **metadata, "previous_draft_artifact_id": artifact.id}
                # No score or unearned scientific verification is inherited.
                metadata["unverified_manual_paragraph_ids"] = sorted(set(metadata.get("unverified_manual_paragraph_ids") or []) | {paragraph["paragraph_id"]})
                for value in entries.values():
                    if value.get("paragraph_key") == candidate["paragraph_key"] and value.get("status") == "pending":
                        value["status"] = "superseded"
            candidate.update(status=target_status, decided_at=utc_now().isoformat())
            entries[candidate_id] = candidate
            if decision == "reject":
                invalid = {candidate_id}
                for _ in range(len(entries)):
                    descendants = {cid for cid, entry in entries.items() if entry.get("parent_candidate_id") in invalid}
                    if descendants.issubset(invalid):
                        break
                    invalid.update(descendants)
                for cid in invalid - {candidate_id}:
                    if entries[cid].get("status") == "pending":
                        entries[cid]["status"] = "superseded"
            files[DRAFT_REWRITE_CANDIDATES] = ((json.dumps({"entries": entries}, ensure_ascii=False)+"\n").encode(), "json")
            try:
                _, state = self._publish_files(principal, project_id, files, expected_revision=revision,
                    expected_current_artifacts=expected, metadata=metadata, invalidate_final=DRAFT_MANUSCRIPT in files,
                    approval_events=[{"id": str(uuid.uuid4()), "stage_id": "draft",
                        "subject_type": "paragraph-dialogue-candidate", "subject_id": candidate_id,
                        "decision": decision, "created_at": utc_now(), "details": {
                            "paragraph_key": candidate["paragraph_key"], "base_text_sha256": candidate["base_text_sha256"],
                            "source_draft_artifact_id": candidate["source_draft_artifact_id"]}}])
                return {"candidate_id": candidate_id, "decision": decision, "revision": state.revision}
            except WorkflowConflict:
                if attempt == 2:
                    raise
