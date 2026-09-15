import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from review_writer_core.dialogue_memory import conversation_memory, candidate_response
from review_writer_core.paragraph_revision import exact_hash
from review_writer_api.domain_services.actions.draft.dialogue import DraftDialogueMixin


class DialogueMemoryTests(unittest.TestCase):
    def test_recent_verbatim_summary_reuse_and_decision_invalidation(self):
        records = [{"id": str(i), "user": "Request " + str(i), "responses": [
            {"assistant": "Reply", "candidate_text": "Proposal", "decision": "pending"}]} for i in range(10)]
        first = conversation_memory(records)
        self.assertEqual(records[-6:], first["recent_turns"])
        self.assertEqual(4, first["summary"]["source_count"])
        self.assertIs(first["summary"], conversation_memory(records, first)["summary"])
        records[0]["responses"][0]["decision"] = "rejected"
        updated = conversation_memory(records, first)
        self.assertNotEqual(first["summary"]["source_sha256"], updated["summary"]["source_sha256"])
        self.assertEqual("rejected", updated["summary"]["turns"][0]["responses"][0]["decision"])

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
