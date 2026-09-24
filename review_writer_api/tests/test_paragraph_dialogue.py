import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from review_writer_api.tests import test_drafts_v1 as fixtures
from review_writer_api.errors import WorkflowConflict


class ParagraphDialogueTests(fixtures.DraftsV1Tests):
    def test_global_plan_without_located_issues_keeps_paragraphs_without_rewriting(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            before = client.get(base).json()
            self.coherence_plan = {"status": "complete", "issues": [],
                "covered_ids": [p["paragraph_id"] for p in before["paragraphs"]]}
            response = client.post(base + "/dialogue-batch", headers=self.headers("global-no-changes"))
            self.assertEqual(202, response.status_code, response.text)
            for _ in range(150):
                job = client.get("/api/v1/jobs/" + response.json()["id"]).json()
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(.02)
            self.assertEqual("succeeded", job["status"], job)
            self.assertEqual("complete", job["result"]["coherence_plan"]["status"])
            self.assertTrue(all(r.get("outcome") == "kept_original" for r in job["result"]["paragraph_results"].values()))
            self.assertEqual(before["draft_artifact_id"], client.get(base).json()["draft_artifact_id"])

    def test_automatic_candidate_checks_dependencies_without_staling_independent_candidates(self):
        from review_writer_core.manuscript_coherence import candidate_fingerprint
        from review_writer_core.paragraph_revision import exact_hash
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            paragraphs = service.get(self.first, self.project_id)["paragraphs"]
            first, second = paragraphs[:2]
            def make(paragraph, key, dependencies):
                payload, _ = service.dialogue_payload(self.first, self.project_id, paragraph["paragraph_key"],
                    message="Clarify wording", base_text_sha256=exact_hash(paragraph["text"]),
                    use_saved=True, idempotency_key=key, include_memory=False)
                payload["dialogue"].update(automatic_batch=True, dependency_hashes=dependencies)
                text = paragraph["text"] + " Clear wording."
                sources = [{"ref": "P1:1", "paper_id": "P1", "text": "Original evidence"}]
                built = {"outcome": "candidate", "candidate_text": text, "reply": "Checked", "sources": sources,
                    "validation_errors": [], "automatic_source_check": {"status": "supported", "fingerprint": candidate_fingerprint(text, sources)}}
                return service.publish_dialogue(self.first, self.project_id, payload, built)["candidate_id"]
            dependent = make(first, "dependent", {second["paragraph_id"]: exact_hash(second["text"])})
            independent = make(second, "independent", {})
            service.decide_dialogue(self.first, self.project_id, independent, decision="accept")
            with self.assertRaisesRegex(WorkflowConflict, "Related manuscript"):
                service.decide_dialogue(self.first, self.project_id, dependent, decision="accept")
            self.assertEqual(first["text"], service.get(self.first, self.project_id)["paragraphs"][0]["text"])

    def test_initial_chapter_is_immutable_and_restart_is_isolated_until_acceptance(self):
        from review_writer_core.workflow.artifacts import DRAFT_INITIAL_MANUSCRIPT
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            before = client.get(base).json()
            section = before["sections"][0]
            endpoint = base + "/section-dialogues/" + section["section_id"]
            versions = client.get(endpoint + "/versions").json()
            initial = versions["initial"]
            self.assertIsNotNone(initial)
            original = section["paragraphs"][0]
            self.assertEqual(original["text"], initial["paragraphs"][0]["text"])
            self.start_turn(client, original, "old-discussion")
            service = self.app.state.drafts_service
            candidate = service.dialogue_history(self.first, self.project_id, original["paragraph_key"])["messages"][-1]["candidate"]
            service.decide_dialogue(self.first, self.project_id, candidate["candidate_id"], decision="accept")
            saved = client.get(base).json()
            current = saved["sections"][0]
            self.assertNotEqual(original["text"], current["paragraphs"][0]["text"])
            self.assertEqual(initial, client.get(endpoint + "/versions").json()["initial"])
            body = {"message": "Start from the initial wording", "action": "revise", "branch_id": "new-branch",
                    "initial_artifact_id": initial["artifact_id"],
                    "base_hashes": {p["paragraph_key"]: p["text_sha256"] for p in current["paragraphs"]}}
            response = client.post(endpoint, headers=self.headers("restart"), json=body)
            self.assertEqual(202, response.status_code, response.text)
            for _ in range(150):
                job = client.get("/api/v1/jobs/" + response.json()["id"]).json()
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(.02)
            self.assertEqual("succeeded", job["status"])
            self.assertEqual(next(p["text"] for p in initial["paragraphs"] if p["paragraph_id"] == self.last_dialogue["paragraph_id"]), self.last_dialogue["discussion_text"])
            self.assertEqual("", self.last_dialogue["parent_candidate_id"])
            self.assertFalse(self.last_dialogue["section_context"]["conversation_memory"]["recent_turns"])
            after = client.get(base).json()
            self.assertEqual(saved["draft_artifact_id"], after["draft_artifact_id"])
            new_candidate = next(c for c in after["rewrite_candidates"] if c.get("batch_job_id") == job["id"] and c["paragraph_key"] == original["paragraph_key"])
            self.assertEqual("new-branch", new_candidate["branch_id"])
            self.assertEqual(original["text"], new_candidate["discussion_text"])
            self.assertEqual(current["paragraphs"][0]["text_sha256"], new_candidate["base_text_sha256"])
            self.assertEqual(200, client.post(base + "/dialogue-candidates/" + new_candidate["candidate_id"] + "/accept").status_code)
            accepted = client.get(base).json()
            for other in accepted["paragraphs"]:
                if other["paragraph_key"] != original["paragraph_key"]:
                    self.assertEqual(other["text"], next(p["text"] for p in saved["paragraphs"] if p["paragraph_key"] == other["paragraph_key"]))
            self.assertEqual(initial, client.get(endpoint + "/versions").json()["initial"])
            self.assertEqual(1, len(service.repository.list_artifacts(self.first.user_id, self.project_id, DRAFT_INITIAL_MANUSCRIPT)))
            forged = client.post(endpoint, headers=self.headers("forged-baseline"), json={**body,
                "base_hashes": {p["paragraph_key"]: p["text_sha256"] for p in accepted["sections"][0]["paragraphs"]},
                "initial_artifact_id": accepted["draft_artifact_id"]})
            self.assertEqual(409, forged.status_code)

    def test_section_turn_is_scoped_persisted_and_does_not_save_automatically(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            before = client.get(base).json()
            section = before["sections"][0]
            target = section["paragraphs"][0]
            self.start_turn(client, target, "earlier-message")
            service = self.app.state.drafts_service
            earlier = service.dialogue_history(self.first, self.project_id, target["paragraph_key"])["messages"][-1]["candidate"]
            service.decide_dialogue(self.first, self.project_id, earlier["candidate_id"], decision="reject")
            body = {"message": "Improve the chapter's argument where needed",
                "base_hashes": {p["paragraph_key"]: p["text_sha256"] for p in section["paragraphs"]},
                "action": "revise"}
            endpoint = base + "/section-dialogues/" + section["section_id"]
            response = client.post(endpoint, headers=self.headers("section-turn"), json=body)
            self.assertEqual(202, response.status_code, response.text)
            job = response.json()
            for _ in range(250):
                job = client.get(f"/api/v1/jobs/{job['id']}").json()
                if job["status"] not in {"queued", "running", "cancel_requested"}:
                    break
                time.sleep(.03)
            self.assertEqual("succeeded", job["status"], job)
            sent = self.last_dialogue
            self.assertEqual("revision", sent["routing"]["mode"])
            self.assertNotIn("conversation_memory", sent)
            remembered = sent["section_context"]["conversation_memory"]["recent_turns"]
            self.assertEqual(1, len(remembered))
            self.assertEqual("rejected", remembered[0]["responses"][0]["decision"])
            self.assertEqual("Clarified using the source", remembered[0]["responses"][0]["assistant"])
            self.assertEqual({p["paragraph_key"] for p in section["paragraphs"]}, set(job["result"]["paragraph_results"]))
            history = client.get(endpoint).json()
            self.assertEqual(1, len(history["turns"]))
            self.assertEqual(body["message"], history["turns"][0]["message"])
            stream_url = endpoint + "/stream/" + job["id"]
            streamed = client.get(stream_url)
            self.assertEqual(200, streamed.status_code, streamed.text)
            self.assertIn("text/event-stream", streamed.headers["content-type"])
            self.assertIn("event: done", streamed.text)
            self.assertEqual("no", streamed.headers["x-accel-buffering"])
            self.assertEqual(404, client.get(base + "/section-dialogues/foreign/stream/" + job["id"]).status_code)
            snapshot = service.section_stream_snapshot(self.first, self.project_id, section["section_id"], job["id"])
            with patch.object(service, "section_stream_snapshot", side_effect=[
                {**snapshot, "status": "running", "streaming_reply": "First"},
                {**snapshot, "status": "running", "streaming_reply": "First second"}, snapshot,
            ]):
                streamed = client.get(stream_url)
                self.assertEqual(3, streamed.text.count("event: snapshot"))
                self.assertLess(streamed.text.index('"First"'), streamed.text.index('"First second"'))
            after = client.get(base).json()
            self.assertEqual(before["draft_artifact_id"], after["draft_artifact_id"])
            candidate = next(c for c in after["rewrite_candidates"] if c.get("batch_job_id") == job["id"] and c["paragraph_key"] == target["paragraph_key"])
            self.assertEqual(target["paragraph_key"], candidate["paragraph_key"])
            self.assertEqual(["protected_chemical_identities_changed"], candidate["validation_warnings"])
            repeated = client.post(endpoint, headers=self.headers("section-turn"), json=body)
            self.assertEqual(job["id"], repeated.json()["id"])
            conflicting = client.post(endpoint, headers=self.headers("section-turn"), json={**body, "message": "Different"})
            self.assertEqual(409, conflicting.status_code)
            accepted = client.post(base + "/dialogue-candidates/" + candidate["candidate_id"] + "/accept")
            self.assertEqual(200, accepted.status_code, accepted.text)
            stale = client.post(endpoint, headers=self.headers("stale-section"), json=body)
            self.assertEqual(409, stale.status_code)
            current = client.get(base).json()
            self.assertEqual(section["section_id"], current["sections"][0]["section_id"])
            for p in current["paragraphs"]:
                if p["paragraph_key"] != target["paragraph_key"]:
                    original = next(x for x in before["paragraphs"] if x["paragraph_key"] == p["paragraph_key"])
                    self.assertEqual(original["text"], p["text"])

    def test_section_rejects_foreign_targets_and_groups_without_topic_specific_rules(self):
        from review_writer_core.paragraph_revision import dialogue_sections
        sections = dialogue_sections("## Background\n\nFirst.\n<!-- paragraph_id: S1-p1 -->\n\nSecond.\n<!-- paragraph_id: S1-p2 -->\n\n## Other domain\n\nThird.\n<!-- paragraph_id: S2-p1 -->\n", {}, "seed")
        self.assertEqual([2, 1], [len(s["paragraphs"]) for s in sections])
        self.assertEqual("Background", sections[0]["title"])
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            base = f"/api/v1/projects/{self.project_id}/draft"
            section = client.get(base).json()["sections"][0]
            response = client.post(base + "/section-dialogues/" + section["section_id"],
                headers=self.headers("foreign-target"), json={"message": "Revise", "paragraph_keys": ["foreign"],
                    "base_hashes": {p["paragraph_key"]: p["text_sha256"] for p in section["paragraphs"]}})
            self.assertEqual(422, response.status_code, response.text)
            missing = client.post(base + "/section-dialogues/" + section["section_id"],
                headers=self.headers("missing-target"), json={"message": "Generate from discussion", "action": "revise",
                    "base_hashes": {p["paragraph_key"]: p["text_sha256"] for p in section["paragraphs"]}})
            self.assertEqual(202, missing.status_code, missing.text)

    # Keep the shared setup helpers without rerunning inherited legacy tests here.
    def extra_native_workflow_overrides(self):
        handlers = super().extra_native_workflow_overrides()
        old = handlers["draft.rewrite"]
        def rewrite(context, payload):
            if payload.get("revision_mode") != "dialogue":
                return old(context, payload)
            if payload["dialogue"].get("coherence_only"):
                return {"coherence_plan": getattr(self, "coherence_plan", {"status": "unavailable", "issues": []})}
            if payload["dialogue"].get("route_only"):
                return {"routing": {"mode": "revision", "targets": payload["dialogue"]["route_allowed_ids"], "related": []}}
            self.last_dialogue = payload["dialogue"]
            if getattr(self, "fail_paragraph", "") == payload["paragraph_id"]:
                raise RuntimeError("Isolated provider failure")
            return {"reply": "Clarified using the source", "candidate_text": payload["dialogue"]["discussion_text"] + " Clear wording.",
                    "outcome": "candidate", "validation_errors": [], "source_refs": ["P1:p2:b1"],
                    "validation_warnings": ["protected_chemical_identities_changed"] if payload["dialogue"].get("section_context") else [],
                    "sources": [{"ref": "P1:p2:b1", "paper_id": "P1", "title": "Study", "page": 2, "text": "Original evidence"}]}
        handlers["draft.rewrite"] = rewrite
        return handlers

    def start_turn(self, client, paragraph, key="turn-1"):
        response = client.post(f"/api/v1/projects/{self.project_id}/draft/dialogues/{paragraph['paragraph_key']}",
            headers=self.headers(key), json={"message": "Clarify", "base_text_sha256": paragraph["text_sha256"]})
        self.assertEqual(202, response.status_code, response.text)
        job = response.json()
        for _ in range(150):
            job = client.get(f"/api/v1/jobs/{job['id']}").json()
            if job["status"] not in {"queued", "running", "cancel_requested"}:
                break
            time.sleep(.03)
        self.assertEqual("succeeded", job["status"], job)
        return job

    def test_dialogue_without_evaluation_persists_and_accepts_without_scoring(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            current = service.get(self.first, self.project_id)
            paragraph = current["paragraphs"][0]
            job = self.start_turn(client, paragraph)
            history = service.dialogue_history(self.first, self.project_id, paragraph["paragraph_key"])
            candidate = history["messages"][-1]["candidate"]
            self.assertEqual("pending", candidate["status"])
            self.assertEqual("Original evidence", candidate["sources"][0]["text"])
            self.assertEqual(2, candidate["sources"][0]["page"])
            self.assertEqual(current["draft_artifact_id"], service.get(self.first, self.project_id)["draft_artifact_id"])
            duplicate = self.start_turn(client, paragraph)
            self.assertEqual(job["id"], duplicate["id"])
            self.assertFalse(hasattr(service, "publish_accepted_rewrite"))
            result = client.post(f"/api/v1/projects/{self.project_id}/draft/dialogue-candidates/{candidate['candidate_id']}/accept")
            self.assertEqual(200, result.status_code, result.text)
            after = service.get(self.first, self.project_id)
            self.assertIn("Clear wording.", after["first_draft_md"])
            self.assertEqual(paragraph["paragraph_key"], after["paragraphs"][0]["paragraph_key"])
            self.assertEqual(0, self.accept_rewrite_model_calls)

    def test_scored_http_writes_are_retired_but_approval_needs_no_report(self):
        for retired in ("publish_optimization", "auto_apply_optimization_proposal",
                        "decide_optimization_proposal", "repair_accepted_optimization_quality",
                        "rewrite_payload", "publish_rewrite_candidate", "accept_rewrite_payload",
                        "publish_accepted_rewrite", "decide_rewrite"):
            self.assertFalse(hasattr(self.app.state.drafts_service, retired))
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            for endpoint in ("evaluation-jobs", "optimization-jobs", "paragraphs/S1-p1/rewrite-jobs"):
                response = client.post(f"/api/v1/projects/{self.project_id}/draft/{endpoint}", json={})
                self.assertEqual(410, response.status_code, response.text)
            before = client.get(f"/api/v1/projects/{self.project_id}/draft").json()
            self.assertFalse(before["quality_artifact_id"])
            response = client.post(f"/api/v1/projects/{self.project_id}/draft/approve", json={"revision": before["revision"]})
            self.assertEqual(200, response.status_code, response.text)

    def test_dialogue_continues_candidate_and_reject_invalidates_descendants(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            paragraph = service.get(self.first, self.project_id)["paragraphs"][0]
            self.start_turn(client, paragraph)
            self.start_turn(client, paragraph, "turn-2")
            turns = service.dialogue_history(self.first, self.project_id, paragraph["paragraph_key"])["messages"]
            a, b = (t["candidate"] for t in turns)
            self.assertEqual(a["candidate_id"], b["parent_candidate_id"])
            self.assertEqual(2, b["candidate_text"].count("Clear wording."))
            service.decide_dialogue(self.first, self.project_id, a["candidate_id"], decision="reject")
            with self.assertRaises(WorkflowConflict):
                service.decide_dialogue(self.first, self.project_id, b["candidate_id"], decision="accept")

    def test_batch_records_failure_and_continues(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            current = service.get(self.first, self.project_id)
            self.assertGreater(len(current["paragraphs"]), 1)
            self.fail_paragraph = current["paragraphs"][0]["paragraph_id"]
            started = client.post(f"/api/v1/projects/{self.project_id}/draft/dialogue-batch", headers=self.headers("batch-new"))
            self.assertEqual(202, started.status_code, started.text)
            job = started.json()
            for _ in range(300):
                job = client.get(f"/api/v1/jobs/{job['id']}").json()
                if job["status"] not in {"queued", "running", "cancel_requested"}:
                    break
                time.sleep(.03)
            self.assertEqual("succeeded", job["status"], job)
            results = job["result"]["paragraph_results"]
            self.assertEqual(len(current["paragraphs"]), len(results))
            self.assertEqual("failed", results[current["paragraphs"][0]["paragraph_key"]]["status"])
            self.assertTrue(any(r["status"] == "completed" for r in results.values()))
            self.assertEqual(current["draft_artifact_id"], service.get(self.first, self.project_id)["draft_artifact_id"])

    def test_accept_merges_other_paragraph_save_but_rejects_same_paragraph_conflict(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            current = service.get(self.first, self.project_id)
            a, b = current["paragraphs"][:2]
            self.start_turn(client, a)
            candidate = service.dialogue_history(self.first, self.project_id, a["paragraph_key"])["messages"][-1]["candidate"]
            service.save_paragraph(self.first, self.project_id, b["paragraph_id"], text=b["text"] + " Manual change.",
                revision=current["revision"], base_text_sha256=b["text_sha256"])
            service.decide_dialogue(self.first, self.project_id, candidate["candidate_id"], decision="accept")
            after = service.get(self.first, self.project_id)
            self.assertIn("Manual change.", after["first_draft_md"])
            self.assertIn("Clear wording.", after["first_draft_md"])
            a = after["paragraphs"][0]
            self.start_turn(client, a, "new-base")
            late = service.dialogue_history(self.first, self.project_id, a["paragraph_key"])["messages"][-1]["candidate"]
            service.save_paragraph(self.first, self.project_id, a["paragraph_id"], text=a["text"] + " My correction.",
                revision=after["revision"], base_text_sha256=a["text_sha256"])
            with self.assertRaises(WorkflowConflict):
                service.decide_dialogue(self.first, self.project_id, late["candidate_id"], decision="accept")
            self.assertIn("My correction.", service.get(self.first, self.project_id)["first_draft_md"])

    def test_queued_single_turn_occupies_only_its_paragraph(self):
        with TestClient(self.app) as client:
            self.prepare_draft(client)
            service = self.app.state.drafts_service
            repository = self.app.state.workflow_repository
            paragraphs = service.get(self.first, self.project_id)["paragraphs"]
            a, b = paragraphs[:2]
            one, _ = service.dialogue_payload(self.first, self.project_id, a["paragraph_key"],
                message="Check", base_text_sha256=a["text_sha256"], idempotency_key="held")
            held = repository.create_or_get_job(self.first.user_id, self.project_id, "project", "draft.rewrite", "held", one,
                operation_key="paragraph:" + a["paragraph_key"])
            with self.assertRaises(WorkflowConflict):
                repository.create_or_get_job(self.first.user_id, self.project_id, "project", "draft.rewrite", "another", one,
                    operation_key="paragraph:" + a["paragraph_key"])
            self.assertFalse(repository.paragraph_task_slot(self.first.user_id, self.project_id, a["paragraph_key"],
                repository.create_or_get_job(self.first.user_id, self.project_id, "project", "draft.optimize", "batch-held", {}).id))
            other, _ = service.dialogue_payload(self.first, self.project_id, b["paragraph_key"],
                message="Independent", base_text_sha256=b["text_sha256"], idempotency_key="other")
            job = repository.create_or_get_job(self.first.user_id, self.project_id, "project", "draft.rewrite", "other", other,
                operation_key="paragraph:" + b["paragraph_key"])
            self.assertNotEqual(held.id, job.id)
            self.assertEqual("queued", job.status)


for _name in vars(fixtures.DraftsV1Tests):
    if _name.startswith("test_") and _name not in ParagraphDialogueTests.__dict__:
        setattr(ParagraphDialogueTests, _name, None)
