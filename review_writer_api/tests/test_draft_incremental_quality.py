from __future__ import annotations

import unittest

from review_writer_api.domain_services.drafts import DraftsService


class DraftIncrementalQualityTests(unittest.TestCase):


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
                    {"paragraph_id": "p1", "severity": "major", "rule": "C01"},
                    {"paragraph_id": "p2", "severity": "major", "rule": "C01"},
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
