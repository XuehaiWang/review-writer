"""Draft task registration independent of HTTP route construction."""

from review_writer_api.job_handlers.lifecycle import register_publishing_handler
from review_writer_api.job_service import JobYieldRequested, JobCancellationRequested, JobShutdownRequested, JobLeaseLost
from review_writer_api.security import Principal, Role
import uuid
from review_writer_core.workflow.artifacts import DRAFT_REWRITE_CANDIDATES


def register_draft_handlers(drafts_service, job_service, handlers):
    available = dict(handlers or {})

    def dialogue_batch(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        current = context.repository.get_job(context.user_id, context.job_id)
        results = dict((current.result or {}).get("paragraph_results") or {})
        routing = (current.result or {}).get("routing")
        if current.retry_of_job_id and not results:
            previous = context.repository.get_job(context.user_id, current.retry_of_job_id)
            results = {key: value for key, value in (previous.result or {}).get("paragraph_results", {}).items()
                       if value.get("status") not in {"failed", "skipped"}} if previous else {}
            routing = routing or ((previous.result or {}).get("routing") if previous else None)
        if payload.get("section_context") and not routing:
            first = payload["paragraphs"][0]
            if payload.get("action") == "revise":
                routing = {"mode": "revision", "targets": [p["paragraph_id"] for p in payload["paragraphs"]], "related": []}
        if payload.get("section_context") and not routing:
            first = payload["paragraphs"][0]
            route_request, _ = drafts_service.dialogue_payload(principal, context.project_id, first["paragraph_key"],
                message=payload["message"], base_text_sha256=first["text_sha256"], use_saved=True,
                idempotency_key=f"scope:{context.job_id}", include_memory=False)
            route_request["dialogue"].update(route_only=True, section_context=payload["section_context"],
                route_allowed_ids=[p["paragraph_id"] for p in payload["paragraphs"]])
            routing = available["draft.rewrite"](context, route_request).get("routing")
            allowed = {p["paragraph_id"] for p in payload["paragraphs"]}
            if not routing or routing.get("mode") not in {"question", "revision"} or not routing.get("targets") or not set(routing["targets"]).issubset(allowed):
                raise ValueError("Unable to resolve chapter conversation scope. Specify a paragraph and retry.")
            routing["mode"] = "question"
            routing["targets"] = routing["targets"][:1]
            context.report_partial_result({"paragraph_results": results, "routing": routing})
            context.report_progress(0, len(routing["targets"]))
            raise JobYieldRequested()
        targets = [p for p in payload["paragraphs"] if not routing or p["paragraph_id"] in routing["targets"]]
        pending = [p for p in targets if p["paragraph_key"] not in results]
        if not pending:
            return {"paragraph_results": results, "routing": routing}
        target = pending[0]
        key = target["paragraph_key"]
        claimed = context.repository.paragraph_task_slot(context.user_id, context.project_id, key, context.job_id)
        if not claimed:
            results[key] = {"paragraph_id": target["paragraph_id"], "status": "skipped", "reason": "paragraph_busy"}
        else:
            try:
                context.report_partial_result({"paragraph_results": results, "routing": routing, "active_paragraph_key": key})
                turn_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{context.job_id}:{key}"))
                cached, _artifact = drafts_service._read_json(principal, context.project_id, DRAFT_REWRITE_CANDIDATES, required=False)
                entry = (cached.get("entries") or {}).get(turn_id)
                if entry:
                    result = {"candidate_id": entry["candidate_id"]}
                else:
                    one, _ = drafts_service.dialogue_payload(principal, context.project_id, key,
                        message=payload["message"], base_text_sha256=target["text_sha256"], use_saved=payload.get("use_saved", True),
                        idempotency_key=f"batch:{context.job_id}:{key}", include_memory=not bool(payload.get("section_context")))
                    one["dialogue"]["turn_id"] = turn_id
                    one["dialogue"]["batch_job_id"] = context.job_id
                    one["dialogue"]["routing"] = routing
                    if payload.get("section_context"):
                        section_context = dict(payload["section_context"])
                        # Read decisions afresh at execution time; HTTP submission may have waited in the queue.
                        sections = drafts_service.draft_dialogue_sections(principal, context.project_id)
                        section = next((s for s in sections if s["section_id"] == payload.get("section_id")), None)
                        if section:
                            history = drafts_service.section_dialogue_history(principal, context.project_id, section["section_id"])["turns"]
                            # This turn is represented below as provisional candidates, not past conversation.
                            section_context["conversation_memory"] = drafts_service.section_conversation_memory(
                                principal, context.project_id, section, history, exclude_job_id=context.job_id)
                            section_context["paragraphs"] = [{"paragraph_id": p["paragraph_id"], "text": p["text"]}
                                for p in section["paragraphs"]]
                        # Earlier candidates from this turn are provisional context, never saved text or evidence.
                        section_context["earlier_turn_candidates"] = [
                            {"paragraph_id": c["paragraph_id"], "candidate_text": c["candidate_text"]}
                            for c in (cached.get("entries") or {}).values()
                            if c.get("batch_job_id") == context.job_id and c.get("candidate_text")
                            and c.get("status") in {"pending", "accepted"}]
                        one["dialogue"]["section_context"] = section_context
                        one["dialogue"]["scope_instruction"] = (
                            "The user is discussing the chapter as a whole. Read its context and previous requests; "
                            "revise ONLY the current target paragraph if needed for that request. "
                            "Retain it when the request concerns another paragraph. Avoid repeating content already "
                            "covered by other paragraphs. Context is read-only and is not scientific evidence.")
                    built = available["draft.rewrite"](context, one)
                    context.checkpoint()
                    result = drafts_service.publish_dialogue(principal, context.project_id, one, built)
                results[key] = {"paragraph_id": target["paragraph_id"], "status": "completed", **result}
            except (JobCancellationRequested, JobShutdownRequested, JobLeaseLost):
                raise
            except Exception as exc:
                results[key] = {"paragraph_id": target["paragraph_id"], "status": "failed", "reason": str(exc)[:1000]}
            finally:
                context.repository.paragraph_task_slot(context.user_id, context.project_id, key, context.job_id, release=True)
        context.report_partial_result({"paragraph_results": results, "routing": routing, "active_paragraph_key": ""})
        context.report_progress(len(results), len(targets))
        if len(results) < len(targets):
            raise JobYieldRequested()
        return {"paragraph_results": results, "routing": routing}

    def dispatch_revision(existing):
        def handler(context, payload):
            if payload.get("revision_mode") == "dialogue_batch":
                return dialogue_batch(context, payload)
            if payload.get("revision_mode") == "dialogue":
                principal = Principal(context.user_id, frozenset({Role.USER}))
                store, _ = drafts_service._read_json(principal, context.project_id, DRAFT_REWRITE_CANDIDATES, required=False)
                saved = (store.get("entries") or {}).get(payload["dialogue"]["turn_id"])
                if saved:
                    return {"candidate_id": saved["candidate_id"], "reply": saved.get("reply", "")}
                return existing(context, payload)
            from review_writer_api.errors import WorkflowValidationError
            raise WorkflowValidationError("This legacy revision task is retired. Start a paragraph dialogue from the current Draft.")
        return handler

    for job_type in ("draft.rewrite", "draft.optimize"):
        register_publishing_handler(
            job_service, job_type, available.get("draft.rewrite"), drafts_service.publish_dialogue,
            progress_total=4, validate=drafts_service.validate_dialogue_inputs,
            dispatch=dispatch_revision,
        )
