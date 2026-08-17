from __future__ import annotations

import unittest
from types import SimpleNamespace
import json
import threading

from review_writer_api.domain_services.drafts import (
    DRAFT_DOCUMENT,
    DRAFT_OPTIMIZATIONS,
    DRAFT_OVERLAYS,
    DRAFT_QUALITY,
    DraftsService,
)


class DraftOptimizationProposalTests(unittest.TestCase):
    @staticmethod
    def _drafts_service() -> DraftsService:
        service = object.__new__(DraftsService)
        service._write_lock = threading.RLock()
        return service

    def test_candidate_contains_only_reviewable_paragraph_body_changes(self) -> None:
        current = (
            "# Current title\n\n"
            "Original first paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "## Current heading\n\n"
            "Original second paragraph [2].\n\n"
            "<!-- paragraph_id: sec2-p1 -->\n"
        )
        model = (
            "# Unreviewed changed title\n\n"
            "Improved first paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "## Unreviewed changed heading\n\n"
            "Original second paragraph [2].\n\n"
            "<!-- paragraph_id: sec2-p1 -->\n"
        )

        candidate, changes = DraftsService._optimization_candidate(current, model)

        self.assertIn("# Current title", candidate)
        self.assertIn("## Current heading", candidate)
        self.assertNotIn("Unreviewed changed", candidate)
        self.assertIn("Improved first paragraph [1].", candidate)
        self.assertEqual(
            [
                {
                    "paragraph_id": "sec1-p1",
                    "original_text": "Original first paragraph [1].",
                    "candidate_text": "Improved first paragraph [1].",
                }
            ],
            changes,
        )

    def test_publish_optimization_creates_a_proposal_without_publishing_draft(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        quality = SimpleNamespace(id="quality-v1")
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]

        def read_json(_principal, _project_id, logical_name, **_kwargs):
            if logical_name == DRAFT_QUALITY:
                return {"score": 70.0}, quality
            if logical_name == DRAFT_OPTIMIZATIONS:
                return {}, None
            raise AssertionError(logical_name)

        service._read_json = read_json  # type: ignore[method-assign]
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["kwargs"] = kwargs
            return (
                {DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-artifact")},
                SimpleNamespace(revision=8),
            )

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.publish_optimization(
            SimpleNamespace(),
            "project-1",
            {
                "source_draft_artifact_id": "draft-v1",
                "expected_revision": 7,
            },
            {
                "draft_text": current_text.replace(
                    "Original paragraph [1].", "Improved paragraph [1]."
                ),
                "score": 88.0,
                "feedback_status": {"rewrite_accepted": 1},
            },
        )

        files = captured["files"]
        self.assertEqual({DRAFT_OPTIMIZATIONS}, set(files))
        proposal_document = json.loads(files[DRAFT_OPTIMIZATIONS][0])
        proposal = next(iter(proposal_document["entries"].values()))
        self.assertEqual("pending", proposal["status"])
        self.assertEqual("Original paragraph [1].", proposal["changes"][0]["original_text"])
        self.assertEqual("Improved paragraph [1].", proposal["changes"][0]["candidate_text"])
        self.assertTrue(result["proposal_created"])
        self.assertFalse(result["draft_changed"])

    def test_accepting_proposal_publishes_draft_quality_and_overlays_together(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "candidate_draft_text": current_text.replace(
                "Original paragraph [1].", "Improved paragraph [1]."
            ),
            "candidate_quality": {"score": 88.0},
            "rewrite_overlays": {"entries": {}},
            "changes": [
                {
                    "paragraph_id": "sec1-p1",
                    "original_text": "Original paragraph [1].",
                    "candidate_text": "Improved paragraph [1].",
                }
            ],
            "candidate_score": 88.0,
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}},
            store_artifact,
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["kwargs"] = kwargs
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                DRAFT_DOCUMENT: SimpleNamespace(id="draft-v2"),
            }
            quality_content = files[DRAFT_QUALITY][0](published)
            captured["quality"] = json.loads(quality_content)
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
        )

        self.assertEqual(
            {DRAFT_OPTIMIZATIONS, DRAFT_DOCUMENT, DRAFT_QUALITY, DRAFT_OVERLAYS},
            set(captured["files"]),
        )
        self.assertEqual("draft-v2", captured["quality"]["source_draft_artifact_id"])
        self.assertEqual("draft-v2", result["draft_artifact_id"])
        self.assertTrue(captured["kwargs"]["invalidate_final"])

    def test_accepting_selected_paragraphs_keeps_unselected_text_and_updates_score(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal first [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "Original second [2].\n\n"
            "<!-- paragraph_id: sec1-p2 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")

        def evaluation(paragraph_id: str, score: float) -> dict[str, object]:
            return {
                "evaluation_scope": "single_paragraph",
                "paragraph_id": paragraph_id,
                "paragraph_score": {
                    "paragraph_id": paragraph_id,
                    "score": score,
                    "severity": "pass",
                    "route": "none",
                    "failed_dimensions": [],
                    "diagnosis": "Improved.",
                },
                "local_hard_gate_failures": [],
                "local_preflight": {"issues": [], "hard_regressions": []},
            }

        changes = [
            {
                "paragraph_id": "sec1-p1",
                "original_text": "Original first [1].",
                "candidate_text": "Improved first [1].",
                "candidate_evaluation": evaluation("sec1-p1", 90),
            },
            {
                "paragraph_id": "sec1-p2",
                "original_text": "Original second [2].",
                "candidate_text": "Improved second [2].",
                "candidate_evaluation": evaluation("sec1-p2", 95),
            },
        ]
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "source_quality": {
                "score": 70.0,
                "total_score": 70.0,
                "paragraph_scores": [
                    {"paragraph_id": "sec1-p1", "score": 60},
                    {"paragraph_id": "sec1-p2", "score": 70},
                ],
                "issues": [],
                "hard_gate_failures": [],
            },
            "source_overlays": {"entries": {}},
            "changes": changes,
            "candidate_score": 97.5,
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}},
            store_artifact,
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                DRAFT_DOCUMENT: SimpleNamespace(id="draft-v2"),
            }
            captured["draft"] = files[DRAFT_DOCUMENT][0].decode("utf-8")
            captured["quality"] = json.loads(files[DRAFT_QUALITY][0](published))
            captured["overlays"] = json.loads(files[DRAFT_OVERLAYS][0])
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]

        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
            selected_paragraph_ids=["sec1-p1"],
        )

        self.assertIn("Improved first [1].", captured["draft"])
        self.assertIn("Original second [2].", captured["draft"])
        self.assertNotIn("Improved second [2].", captured["draft"])
        self.assertEqual(85.0, captured["quality"]["score"])
        self.assertEqual(["sec1-p1"], captured["quality"]["selected_paragraph_ids"])
        self.assertEqual(["sec1-p1"], list(captured["overlays"]["entries"]))
        self.assertEqual(["sec1-p1"], result["selected_paragraph_ids"])

    def test_discarding_proposal_keeps_the_current_draft(self) -> None:
        service = self._drafts_service()
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "candidate_draft_text": "Improved paragraph.",
            "changes": [{"paragraph_id": "sec1-p1"}],
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: ("Original paragraph.\n", current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}},
            store_artifact,
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["kwargs"] = kwargs
            return (
                {DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2")},
                SimpleNamespace(revision=9),
            )

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="reject",
            revision=8,
        )

        self.assertEqual({DRAFT_OPTIMIZATIONS}, set(captured["files"]))
        self.assertEqual("draft-v1", result["draft_artifact_id"])
        self.assertFalse(captured["kwargs"]["invalidate_final"])


if __name__ == "__main__":
    unittest.main()
