from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "review-first-draft-feedback-loop"
    / "scripts"
    / "feedback_loop.py"
)
SPEC = importlib.util.spec_from_file_location("feedback_loop_batching", SCRIPT_PATH)
assert SPEC and SPEC.loader
feedback_loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feedback_loop)


def paragraph(paragraph_id: str) -> dict[str, str]:
    return {"paragraph_id": paragraph_id, "heading": "Section", "text": "Draft paragraph."}


def rubric() -> dict[str, object]:
    return {
        "dimensions": [
            {"id": "D1", "weight": 50},
            {"id": "D2", "weight": 50},
        ]
    }


def batch_result(ids: list[str], d1: float, d2: float) -> dict[str, object]:
    return {
        "dimension_scores": [
            {"id": "D1", "level": d1, "evidence": f"D1 evidence {d1}"},
            {"id": "D2", "level": d2, "evidence": f"D2 evidence {d2}"},
        ],
        "paragraph_scores": [
            {"paragraph_id": paragraph_id, "score": 80}
            for paragraph_id in ids
        ],
    }


class FeedbackLoopBatchingTests(unittest.TestCase):
    def test_paragraph_batches_bound_each_provider_request(self) -> None:
        paragraphs = [paragraph(f"p{index}") for index in range(17)]

        batches = feedback_loop.paragraph_batches(paragraphs, batch_size=8)

        self.assertEqual([len(batch) for batch in batches], [8, 8, 1])
        self.assertEqual(
            [item["paragraph_id"] for batch in batches for item in batch],
            [item["paragraph_id"] for item in paragraphs],
        )

    def test_merge_batched_evaluations_preserves_paragraphs_and_weights_dimensions(self) -> None:
        first = [paragraph("p1"), paragraph("p2")]
        second = [paragraph("p3")]

        merged = feedback_loop.merge_batched_evaluations(
            rubric(),
            [
                (first, batch_result(["p1", "p2"], 4, 2)),
                (second, batch_result(["p3"], 1, 4)),
            ],
        )

        levels = {item["id"]: item["level"] for item in merged["dimension_scores"]}
        self.assertEqual(levels, {"D1": 3.0, "D2": 2.667})
        self.assertEqual(
            [item["paragraph_id"] for item in merged["paragraph_scores"]],
            ["p1", "p2", "p3"],
        )

    def test_merge_batched_evaluations_rejects_missing_rubric_dimension(self) -> None:
        paragraphs = [paragraph("p1")]
        result = batch_result(["p1"], 4, 2)
        result["dimension_scores"] = result["dimension_scores"][:1]

        with self.assertRaisesRegex(RuntimeError, "every rubric dimension exactly once"):
            feedback_loop.merge_batched_evaluations(rubric(), [(paragraphs, result)])

    def test_prompt_compaction_keeps_passages_and_filters_other_preflight_rows(self) -> None:
        paragraphs = [paragraph("p1")]
        evidence = {
            "p1": {
                "paragraph_id": "p1",
                "paper_ids": ["P001"],
                "original_source_ready": True,
                "evidence": [
                    {
                        "paper_id": "P001",
                        "title": "Paper",
                        "abstract": "duplicated abstract should not be sent",
                        "main_content": "duplicated content should not be sent",
                        "original_passages": [
                            {"ref": "P001:p1:b1", "page": 1, "text": "verifiable source passage"}
                        ],
                    }
                ],
            }
        }
        preflight = {
            "checks": {"paragraph_count": 2},
            "paragraph_checks": [
                {"paragraph_id": "p1", "issues": []},
                {"paragraph_id": "p2", "issues": ["P01"]},
            ],
            "paragraph_findings": [
                {"paragraph_id": "p2", "rule": "P01"},
            ],
        }

        prompt = feedback_loop.evaluation_prompt(
            rubric(), paragraphs, evidence, preflight, 90, 80
        )

        self.assertIn("verifiable source passage", prompt)
        self.assertNotIn("duplicated abstract", prompt)
        self.assertNotIn('"paragraph_id": "p2"', prompt)

    def test_http_524_is_returned_to_adaptive_split_without_identical_retries(self) -> None:
        timeout = urllib.error.HTTPError(
            "https://provider.example/v1/chat/completions",
            524,
            "Origin Time-out",
            {},
            io.BytesIO(b"upstream deadline"),
        )
        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://provider.example/v1",
            "REVIEW_WRITING_WIRE_API": "chat-completions",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            feedback_loop.urllib.request,
            "urlopen",
            side_effect=timeout,
        ) as urlopen:
            with self.assertRaises(feedback_loop.ProviderDeadlineExceeded):
                feedback_loop.call_json_model("score this batch", label="batch")

        self.assertEqual(urlopen.call_count, 1)

    def test_batch_review_keeps_only_paragraphs_that_improved(self) -> None:
        source = (
            "# Review\n\n"
            "Original first paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "Original second paragraph [2].\n\n"
            "<!-- paragraph_id: sec1-p2 -->\n"
        )
        candidate = source.replace(
            "Original first paragraph [1].",
            "Improved first paragraph [1].",
        ).replace(
            "Original second paragraph [2].",
            "Weaker second paragraph [2].",
        )
        source_evaluation = {
            "total_score": 75,
            "paragraph_scores": [
                {"paragraph_id": "sec1-p1", "score": 70},
                {"paragraph_id": "sec1-p2", "score": 80},
            ],
        }
        candidate_evaluation = {
            "total_score": 80,
            "paragraph_scores": [
                {"paragraph_id": "sec1-p1", "score": 90},
                {"paragraph_id": "sec1-p2", "score": 70},
            ],
        }
        preflight = {
            "paragraph_checks": [
                {
                    "paragraph_id": "sec1-p1",
                    "word_range_applicable": False,
                    "issues": [],
                },
                {
                    "paragraph_id": "sec1-p2",
                    "word_range_applicable": False,
                    "issues": [],
                },
            ]
        }
        best: dict[str, dict[str, object]] = {}

        excluded = feedback_loop.update_best_paragraph_candidates(
            best,
            source_markdown=source,
            candidate_markdown=candidate,
            source_evaluation=source_evaluation,
            candidate_evaluation=candidate_evaluation,
            source_preflight=preflight,
            candidate_preflight=preflight,
            candidate_evidence={},
            min_words=1,
            max_words=200,
            iteration=1,
        )

        self.assertEqual(["sec1-p1"], list(best))
        self.assertTrue(
            any(
                "candidate_score_not_improved" in row.get("reasons", [])
                for row in excluded
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "batch_review_candidates.json"
            payload = feedback_loop.write_batch_review_candidates(
                path,
                project_id="project-1",
                source_markdown=source,
                source_evaluation=source_evaluation,
                best_candidates=best,
                excluded=excluded,
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(85.0, payload["candidate_score"])
        self.assertIn("Improved first paragraph [1].", payload["candidate_draft_text"])
        self.assertIn("Original second paragraph [2].", payload["candidate_draft_text"])
        self.assertEqual(payload, persisted)


if __name__ == "__main__":
    unittest.main()
