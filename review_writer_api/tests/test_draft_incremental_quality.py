from __future__ import annotations

import unittest
from types import SimpleNamespace

from review_writer_api.domain_services.drafts import DraftsService


class DraftIncrementalQualityTests(unittest.TestCase):
    def test_accept_payload_reuses_candidate_evaluation_created_before_review(self) -> None:
        service = DraftsService(None, None)  # type: ignore[arg-type]
        evaluation = {
            "evaluation_scope": "single_paragraph",
            "evaluation_mode": "accepted_candidate",
            "paragraph_id": "p1",
            "paragraph_score": {"paragraph_id": "p1", "score": 91.5},
        }
        original = "Current paragraph."
        candidate = "Improved current paragraph."
        service.get = lambda _principal, _project_id: {  # type: ignore[method-assign]
            "quality_artifact_id": "quality-v1",
            "draft_artifact_id": "draft-v1",
            "revision": 7,
            "first_draft_md": f"# Draft\n\n{original}\n\n<!-- paragraph_id: p1 -->\n",
            "quality": {
                "score": 72,
                "goal": 90,
                "issues": [{"paragraph_id": "p1"}],
                "paragraph_scores": [{"paragraph_id": "p1", "score": 60}],
            },
        }
        service.compatibility_payload = (  # type: ignore[method-assign]
            lambda _principal, _project_id: {}
        )
        service._read_json = (  # type: ignore[method-assign]
            lambda _principal, _project_id, _logical_name: (
                {
                    "entries": {
                        "candidate-1": {
                            "candidate_id": "candidate-1",
                            "paragraph_id": "p1",
                            "source_draft_artifact_id": "draft-v1",
                            "source_quality_artifact_id": "quality-v1",
                            "original_text": original,
                            "candidate_text": candidate,
                            "candidate_text_sha256": service._text_sha256(candidate),
                            "candidate_evaluation": evaluation,
                            "status": "pending",
                        }
                    }
                },
                SimpleNamespace(id="rewrites-v1"),
            )
        )

        payload = service.accept_rewrite_payload(
            None, "project-1", "candidate-1", revision=7  # type: ignore[arg-type]
        )

        self.assertEqual(evaluation, payload["candidate_evaluation"])
        self.assertIn(candidate, payload["candidate_draft_text"])

    def test_rewrite_payload_allows_targeted_reevaluation_when_full_score_is_stale(self) -> None:
        service = DraftsService(None, None)  # type: ignore[arg-type]
        service.get = lambda _principal, _project_id: {  # type: ignore[method-assign]
            "quality_artifact_id": "quality-v1",
            "draft_artifact_id": "draft-v2",
            "revision": 7,
            "first_draft_md": "Current paragraph.\n\n<!-- paragraph_id: p1 -->\n",
            "paragraphs": [{"paragraph_id": "p1", "text": "Current paragraph."}],
            "quality": {
                "current": False,
                "score": 72,
                "goal": 90,
                "paragraph_pass_threshold": 85,
                "issues": [
                    {
                        "issue_id": "issue-1",
                        "paragraph_id": "p1",
                        "score": 60,
                        "route": "section_rewrite",
                    }
                ],
                "preflight": {
                    "case_word_range": [120, 260],
                    "paragraph_checks": [
                        {"paragraph_id": "p1", "word_range_applicable": False}
                    ],
                },
            },
        }
        service.compatibility_payload = (  # type: ignore[method-assign]
            lambda _principal, _project_id: {}
        )

        payload = service.rewrite_payload(None, "project-1", "p1")  # type: ignore[arg-type]

        self.assertEqual("p1", payload["paragraph_id"])
        self.assertEqual("draft-v2", payload["source_draft_artifact_id"])
        self.assertEqual(120, payload["min_case_words"])
        self.assertEqual(260, payload["max_case_words"])
        self.assertFalse(payload["word_range_applicable"])

    def test_replaces_one_paragraph_and_updates_overall_by_equal_weight_delta(self) -> None:
        service = DraftsService(None, None)  # type: ignore[arg-type]
        current = {
            "score": 80.0,
            "goal": 90.0,
            "paragraph_pass_threshold": 85.0,
            "paragraph_scores": [
                {
                    "paragraph_id": "p1",
                    "score": 60.0,
                    "severity": "major",
                    "route": "section_rewrite",
                },
                {
                    "paragraph_id": "p2",
                    "score": 90.0,
                    "severity": "none",
                    "route": "pass",
                },
            ],
            "paragraph_failures": [],
            "blocking_paragraph_failures": [],
            "issues": [{"paragraph_id": "p1", "issue_id": "old"}],
            "hard_gate_failures": ["paragraph_readability_or_source_failures"],
            "preflight": {
                "paragraph_checks": [{"paragraph_id": "p1", "issues": ["P08"]}],
                "paragraph_findings": [
                    {"paragraph_id": "p1", "severity": "major", "rule": "P08"}
                ],
            },
        }
        built = {
            "paragraph_score": {
                "paragraph_id": "p1",
                "score": 90.0,
                "severity": "none",
                "route": "pass",
                "failed_dimensions": [],
            },
            "local_preflight": {
                "paragraph_checks": [{"paragraph_id": "p1", "issues": []}],
                "paragraph_findings": [],
            },
            "local_hard_gate_failures": [],
            "local_dimension_scores": [{"id": "P03", "level": 4}],
            "evaluated_at": "2026-08-16T00:00:00+00:00",
        }

        updated = service._incremental_quality(
            current,
            built,
            paragraph_id="p1",
            source_quality_artifact_id="quality-v1",
        )

        self.assertEqual(95.0, updated["score"])
        self.assertEqual(90.0, updated["paragraph_scores"][-1]["score"])
        self.assertEqual([], updated["issues"])
        self.assertEqual([], updated["hard_gate_failures"])
        self.assertEqual("incremental_paragraph", updated["quality_scope"])
        self.assertEqual(15.0, updated["incremental_evaluations"][-1]["overall_score_delta"])

    def test_preserves_unrelated_hard_gates_and_other_paragraph_failures(self) -> None:
        service = DraftsService(None, None)  # type: ignore[arg-type]
        current = {
            "score": 70.0,
            "paragraph_pass_threshold": 85.0,
            "paragraph_scores": [
                {"paragraph_id": "p1", "score": 60, "severity": "major", "route": "section_rewrite"},
                {"paragraph_id": "p2", "score": 70, "severity": "major", "route": "section_rewrite"},
            ],
            "issues": [
                {"paragraph_id": "p1", "issue_id": "one"},
                {"paragraph_id": "p2", "issue_id": "two"},
            ],
            "hard_gate_failures": [
                "citation_reference_map_mismatch",
                "paragraph_readability_or_source_failures",
            ],
            "preflight": {
                "paragraph_checks": [],
                "paragraph_findings": [
                    {"paragraph_id": "p1", "severity": "major"},
                    {"paragraph_id": "p2", "severity": "major"},
                ],
            },
        }
        built = {
            "paragraph_score": {
                "paragraph_id": "p1",
                "score": 90,
                "severity": "none",
                "route": "pass",
            },
            "local_preflight": {
                "paragraph_checks": [],
                "paragraph_findings": [],
            },
        }

        updated = service._incremental_quality(
            current,
            built,
            paragraph_id="p1",
            source_quality_artifact_id="quality-v1",
        )

        self.assertIn("citation_reference_map_mismatch", updated["hard_gate_failures"])
        self.assertIn("paragraph_readability_or_source_failures", updated["hard_gate_failures"])
        self.assertEqual(["p2"], [item["paragraph_id"] for item in updated["issues"]])
        self.assertEqual("REGENERATE_SECTIONS", updated["decision"])


if __name__ == "__main__":
    unittest.main()
