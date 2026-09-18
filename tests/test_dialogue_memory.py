import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from review_writer_core.dialogue_memory import conversation_memory, candidate_response
from review_writer_core.paragraph_revision import exact_hash, revision_prompt
from review_writer_api.domain_services.actions.draft.dialogue import DraftDialogueMixin


class DialogueMemoryTests(unittest.TestCase):
    def test_compressed_history_reaches_model_prompt(self):
        records = [{"id": str(i), "user": "Keep chronological structure" if i == 0 else "Edit wording",
            "responses": []} for i in range(14)]
        memory = conversation_memory(records)
        self.assertNotIn("Keep chronological structure", str(memory["recent_turns"]))
        prompt = revision_prompt({"section_context": {"conversation_memory": memory}}, {}, {})
        self.assertIn("Keep chronological structure", prompt)
        self.assertIn("conversation_memory.summary", prompt)
        self.assertIn("unless superseded by newer requests", prompt)

    def test_recent_verbatim_summary_reuse_and_decision_invalidation(self):
        records = [{"id": str(i), "user": "Request " + str(i), "responses": [
            {"assistant": "Reply", "candidate_text": "Proposal", "decision": "pending"}]} for i in range(16)]
        first = conversation_memory(records)
        self.assertEqual(records[-12:], first["recent_turns"])
        self.assertEqual(4, first["summary"]["source_count"])
        self.assertIs(first["summary"], conversation_memory(records, first)["summary"])
        records[0]["responses"][0]["decision"] = "rejected"
        updated = conversation_memory(records, first)
        self.assertNotEqual(first["summary"]["source_sha256"], updated["summary"]["source_sha256"])
        self.assertEqual("rejected", updated["summary"]["turns"][0]["responses"][0]["decision"])

    def test_old_requests_survive_without_unadopted_candidate_prose(self):
        records = [{"id": str(i), "user": "Use chronological order " + str(i),
            "task_status": "succeeded", "responses": [{"decision": "rejected",
                "candidate_text": "DO NOT RESTORE", "assistant": "Suggestion"}]} for i in range(18)]
        result = conversation_memory(records)
        notes = result["summary"]["request_notes"]
        self.assertEqual("0", notes[0]["id"])
        self.assertEqual(["rejected"], notes[0]["decisions"])
        self.assertNotIn("candidate_excerpt", result["summary"]["turns"][0]["responses"][0])
        self.assertEqual("DO NOT RESTORE", records[0]["responses"][0]["candidate_text"])

    def test_previous_summary_format_is_rebuilt(self):
        records = [{"id": str(i), "user": "Request"} for i in range(16)]
        old = conversation_memory(records)
        old["summary"].pop("version")
        self.assertEqual(2, conversation_memory(records, old)["summary"]["version"])

    def test_large_turn_is_bounded_and_explicitly_incomplete(self):
        result = conversation_memory([{"id": "large", "user": "x" * 50000,
            "responses": [{"assistant": "y" * 50000, "candidate_text": "z" * 50000} for _ in range(40)]}])
        self.assertEqual([], result["recent_turns"])
        self.assertGreater(result["summary"]["turns"][0]["responses_omitted"], 0)
        self.assertIn("[excerpt]", result["summary"]["turns"][0]["user_excerpt"])

    def test_saved_text_and_decisions_are_distinct(self):
        candidate = {"status": "accepted", "candidate_text": "old", "reply": "Explanation"}
        self.assertFalse(candidate_response(candidate, exact_hash("edited"))["matches_saved_text"])
        self.assertTrue(candidate_response(candidate, exact_hash("old"))["matches_saved_text"])
        self.assertEqual("stale", candidate_response({**candidate, "status": "pending"}, "changed")["decision"])

    def test_chapter_history_includes_replies_excludes_other_chapter_and_current_batch(self):
        service = DraftDialogueMixin()
        def entry(key, batch, status):
            return {"paragraph_key": key, "batch_job_id": batch, "candidate_id": batch,
                "revision_mode": "dialogue", "created_at": "2026-01-01", "message": "Earlier",
                "reply": "Explanation", "candidate_text": "Proposal", "status": status}
        service._read_json = Mock(return_value=({"entries": {
            "old": entry("a", "old", "rejected"), "current": entry("a", "current", "pending"),
            "foreign": entry("b", "foreign", "accepted")}}, None))
        service.repository = Mock()
        service.repository.list_project_jobs.return_value = []
        section = {"section_id": "S01", "paragraphs": [{"paragraph_key": "a", "text_sha256": "hash"}]}
        memory = service.section_conversation_memory(SimpleNamespace(user_id="u"), "p", section, [], exclude_job_id="current")
        self.assertEqual(["old"], [r["id"] for r in memory["recent_turns"]])
        reply = memory["recent_turns"][0]["responses"][0]
        self.assertEqual("Explanation", reply["assistant"])
        self.assertEqual("Proposal", reply["candidate_text"])
        self.assertEqual("rejected", reply["decision"])


if __name__ == "__main__":
    unittest.main()
