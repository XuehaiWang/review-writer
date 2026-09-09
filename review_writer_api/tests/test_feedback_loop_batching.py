from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
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
    def test_changed_paragraph_evaluation_scores_only_the_requested_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "project-1"
            first = project / "04_first_draft"
            first.mkdir(parents=True)
            (first / "first_draft.md").write_text(
                "# Review\n\nChanged [1].\n\n<!-- paragraph_id: p1 -->\n\n"
                "Unchanged [2].\n\n<!-- paragraph_id: p2 -->\n",
                encoding="utf-8",
            )
            preflight = {
                "hard_regressions": [],
                "paragraph_checks": [
                    {"paragraph_id": "p1", "word_range_applicable": False},
                    {"paragraph_id": "p2", "word_range_applicable": False},
                ],
                "paragraph_findings": [],
            }
            args = SimpleNamespace(
                project_id="project-1",
                goal=90,
                paragraph_goal=85,
                min_case_words=1,
                max_case_words=100,
            )

            with mock.patch.object(
                feedback_loop, "deterministic_preflight", return_value=preflight
            ), mock.patch.object(
                feedback_loop, "paragraph_metadata", return_value={}
            ), mock.patch.object(
                feedback_loop, "matrix_rows", return_value=[]
            ), mock.patch.object(
                feedback_loop, "claim_evidence_contract", return_value={}
            ), mock.patch.object(
                feedback_loop,
                "source_evidence",
                return_value={"paper_ids": [], "evidence": []},
            ), mock.patch.object(
                feedback_loop,
                "call_json_model",
                return_value=batch_result(["p1"], 3, 4),
            ) as model_call:
                _preflight, evaluation, _evidence = (
                    feedback_loop.evaluate_changed_paragraphs(
                        root,
                        project,
                        args,
                        rubric(),
                        {"p1"},
                        first / "incremental",
                        global_dimension_scores=[],
                    )
                )

        self.assertEqual(
            ["p1"],
            [row["paragraph_id"] for row in evaluation["paragraph_scores"]],
        )
        self.assertEqual(1, model_call.call_count)
        self.assertIn("Changed-paragraph", model_call.call_args.kwargs["label"])

    def test_feedback_loop_reuses_an_exact_full_draft_baseline(self) -> None:
        self._run_baseline_case(mismatch=False)

    def test_expired_baseline_is_evaluated_once_and_continues(self) -> None:
        self._run_baseline_case(mismatch=True)

    def _run_baseline_case(self, *, mismatch):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "review-projects" / "project-1" / "04_first_draft"
            first.mkdir(parents=True)
            source_markdown = (
                "# Review\n\nStable paragraph [1].\n\n"
                "<!-- paragraph_id: p1 -->\n"
            )
            draft_path = first / "first_draft.md"
            draft_path.write_text(source_markdown, encoding="utf-8")
            baseline = {
                "quality_scope": feedback_loop.FULL_DRAFT_QUALITY_SCOPE,
                "evaluation_rule_version": feedback_loop.DRAFT_QUALITY_RULE_VERSION,
                "evaluation_input_sha256": feedback_loop.sha256_file(draft_path),
                "paragraph_coverage": ["p1"],
                "total_score": 95,
                "pass_threshold": 90,
                "decision": "PASS",
                "hard_gate_failures": [],
                "paragraph_scores": [
                    {
                        "paragraph_id": "p1",
                        "score": 95,
                        "route": "pass",
                        "severity": "pass",
                    }
                ],
                "paragraph_failures": [],
                "blocking_paragraph_failures": [],
                "preflight": {"hard_regressions": [], "paragraph_checks": []},
                "source_check": {"entries": []},
            }
            if mismatch:
                baseline["evaluation_input_sha256"] = "previous-manuscript"
            (first / "baseline_quality.json").write_text(
                json.dumps(baseline), encoding="utf-8"
            )
            args = SimpleNamespace(
                review_root=str(root),
                project_id="project-1",
                goal=90.0,
                paragraph_goal=85.0,
                max_iterations=1,
                min_improvement=1.0,
                min_case_words=1,
                max_case_words=100,
                evaluate_only=True,
                reuse_baseline=True,
            )

            with mock.patch.object(
                feedback_loop,
                "ensure_prose_paragraph_markers",
                return_value=(
                    source_markdown,
                    {"prose_paragraph_count": 1, "changed": False},
                ),
            ), mock.patch.object(feedback_loop, "evaluate_current_draft") as evaluate:
                evaluate.return_value = (baseline["preflight"], baseline,
                    {"gate_decision": "GATE_RELEASE"}, [paragraph("p1")], {})
                result = feedback_loop.run_feedback_loop(args)

            status = json.loads(
                (first / "feedback_loop_status.json").read_text(encoding="utf-8")
            )

        if mismatch:
            evaluate.assert_called_once()
            self.assertEqual(1, status["baseline_full_evaluation_count"])
            self.assertEqual("baseline_expired", status["run_mode"])
        else:
            evaluate.assert_not_called()
        self.assertEqual("released", result["status"])
        self.assertEqual(not mismatch, status["quality_reused"])

    def test_global_dimensions_are_scored_once_and_local_dimensions_are_batched(self) -> None:
        mixed_rubric = {
            "dimensions": [
                {"id": "M01", "weight": 40},
                {"id": "P01", "weight": 60},
            ]
        }
        paragraphs = [paragraph("p1"), paragraph("p2"), paragraph("p3")]
        merged = feedback_loop.merge_batched_evaluations(
            mixed_rubric,
            [
                (
                    paragraphs[:2],
                    {
                        "dimension_scores": [
                            {"id": "P01", "level": 4, "evidence": "local one"}
                        ],
                        "paragraph_scores": [
                            {"paragraph_id": "p1", "score": 90},
                            {"paragraph_id": "p2", "score": 90},
                        ],
                    },
                ),
                (
                    paragraphs[2:],
                    {
                        "dimension_scores": [
                            {"id": "P01", "level": 1, "evidence": "local two"}
                        ],
                        "paragraph_scores": [
                            {"paragraph_id": "p3", "score": 70}
                        ],
                    },
                ),
            ],
            global_dimension_scores=[
                {"id": "M01", "level": 3, "evidence": "whole draft"}
            ],
        )

        self.assertEqual(
            {"M01": 3.0, "P01": 3.0},
            {item["id"]: item["level"] for item in merged["dimension_scores"]},
        )

    def test_global_prompt_contains_each_paragraph_once_under_a_bound(self) -> None:
        paragraphs = [
            {**paragraph(f"p{index}"), "text": "evidence " * 2_000}
            for index in range(100)
        ]
        global_rubric = {
            "dimensions": [{"id": "M01", "weight": 100, "scope": "global"}]
        }

        prompt = feedback_loop.global_evaluation_prompt(
            global_rubric,
            paragraphs,
            {"checks": {}},
            90,
        )

        self.assertLess(len(prompt), 80_000)
        for index in range(100):
            self.assertEqual(1, prompt.count(f'"paragraph_id": "p{index}"'))

    def test_internal_gateway_uses_task_token_without_provider_key(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps({"output_text": '{"score": 95}'}).encode("utf-8")

        environment = {
            "REVIEW_WRITER_MODEL_GATEWAY_URL": "http://127.0.0.1:8770/api/internal/v1/model-responses",
            "REVIEW_WRITER_TASK_TOKEN": "scoped-task-token",
            "OPENAI_API_KEY": "",
            "REVIEW_WRITING_API_KEY": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            feedback_loop.urllib.request, "urlopen", return_value=Response()
        ) as urlopen:
            result = feedback_loop.call_json_model("score this", label="evaluation")

        self.assertEqual(95, result["score"])
        request = urlopen.call_args.args[0]
        self.assertEqual("Bearer scoped-task-token", request.get_header("Authorization"))
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual("evaluation", sent["stage"])
        self.assertTrue(sent["request_key"].startswith("evaluation-"))

    def test_batch_gateway_reuses_original_result_recovery(self):
        from review_writer_core import model_gateway_client
        environment = {"REVIEW_WRITER_MODEL_GATEWAY_URL": "http://gateway/model-responses",
                       "REVIEW_WRITER_TASK_TOKEN": "task-token"}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "status": "succeeded", "result": {"output_text": '{"score": 95}'},
        }).encode()
        with mock.patch.dict(os.environ, environment), mock.patch.object(
            model_gateway_client.urllib.request, "urlopen", side_effect=[TimeoutError("timed out"), response],
        ) as opened:
            self.assertEqual({"score": 95}, feedback_loop.call_json_model("score this", label="evaluation"))
        self.assertEqual(["POST", "GET"], [call.args[0].get_method() for call in opened.call_args_list])
        self.assertTrue(feedback_loop.recoverable_paragraph_provider_failure(
            model_gateway_client.GatewayRequestError("暂不可用", status_code=503)))
        self.assertFalse(feedback_loop.recoverable_paragraph_provider_failure(
            model_gateway_client.GatewayRequestError("授权失效", status_code=401)))

    def test_internal_gateway_recovers_latex_slash_in_model_json(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                output = (
                    "<think>Internal reasoning with {draft notes}.</think>\n"
                    r'{"score": 95, "excerpt": "$25^{\\\mathrm{C}}$"}'
                )
                return json.dumps({"output_text": output}).encode("utf-8")

        environment = {
            "REVIEW_WRITER_MODEL_GATEWAY_URL": "http://gateway/model-responses",
            "REVIEW_WRITER_TASK_TOKEN": "scoped-task-token",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            feedback_loop.urllib.request, "urlopen", return_value=Response()
        ) as urlopen:
            result = feedback_loop.call_json_model("score this", label="evaluation")

        self.assertEqual(r"$25^{\\mathrm{C}}$", result["excerpt"])
        self.assertEqual(1, urlopen.call_count)

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

    def test_source_check_prefers_exact_claim_bound_chunk_over_lexical_reretrieval(self) -> None:
        complete_passage = (
            "The optimization study examined the reaction conditions. " * 25
            + "NaI improved the yield; 4ua gave 27% at 25 C and 83% at 60 C."
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            section_dir = project / "02_section_drafting"
            section_dir.mkdir(parents=True)
            (section_dir / "writing_plan.json").write_text(
                json.dumps(
                    {
                        "sections": [
                            {
                                "claims": [
                                    {
                                        "claim_id": "S02-p1-C01",
                                        "paragraph_id": "S02-p1",
                                        "citation_group": ["P001"],
                                        "evidence_refs": [
                                            {"evidence_key": "sha256:exact"}
                                        ],
                                    }
                                ],
                                "paragraphs": [
                                    {
                                        "paragraph_id": "S02-p1",
                                        "claim_ids": ["S02-p1-C01"],
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (section_dir / "section_evidence.json").write_text(
                json.dumps(
                    {
                        "evidence_registry": [
                            {
                                "evidence_key": "sha256:exact",
                                "paper_id": "P001",
                                "chunk_id": "C0042",
                                "page_start": 7,
                                "content": complete_passage,
                                "claim_eligible": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            contract = feedback_loop.claim_evidence_contract(project)
            with mock.patch.object(
                feedback_loop,
                "retrieve_original_passages",
            ) as lexical_fallback:
                result = feedback_loop.source_evidence(
                    project,
                    project,
                    {
                        "paragraph_id": "S02-p1",
                        "heading": "Method",
                        "text": "A claim [1].",
                    },
                    {
                        "claim_realizations": [
                            {"claim_id": "S02-p1-C01"}
                        ]
                    },
                    {"P001": {"title": "Paper one"}},
                    {},
                    contract,
                )

        lexical_fallback.assert_not_called()
        self.assertEqual("claim_bound_indexed_evidence", result["evidence_scope"])
        passage = result["evidence"][0]["original_passages"][0]
        self.assertEqual("C0042", passage["chunk_id"])
        self.assertEqual(7, passage["page"])
        self.assertEqual(complete_passage, passage["text"])
        for minimal in (False, True):
            prompt = feedback_loop.rewrite_prompt(
                {"paragraph_id": "S02-p1", "text": "4ua gave 27% at 25 C [1]."},
                {"diagnosis": "Check the temperature comparison."}, result, 1, 280,
                minimal_evidence=minimal,
            )
            self.assertIn("NaI improved the yield; 4ua gave 27% at 25 C and 83% at 60 C.", prompt)
        evaluation_prompt = feedback_loop.evaluation_prompt(
            rubric(), [paragraph("S02-p1")], {"S02-p1": result}, {}, 90, 85,
        )
        self.assertIn(complete_passage, evaluation_prompt)

    def test_evidence_projection_preserves_late_passages_and_source_identity(self) -> None:
        passages = [
            {"ref": f"source-{i}", "page": i, "claim_id": f"claim-{i}",
             "text": f"Experiment {i}. " * 100 + f"Yield {i + 70}% at the end."}
            for i in range(5)
        ]
        raw = {"evidence": [
            {"paper_id": "P001", "original_passages": passages},
            {"paper_id": "P002", "original_passages": [
                {**passages[4], "ref": "other-source", "claim_id": "other-claim"},
            ]},
        ]}
        for minimal in (False, True):
            with self.subTest(minimal=minimal):
                projection = feedback_loop.compact_evidence_for_prompt(raw, minimal=minimal)
                self.assertEqual(5, len(projection["passage_texts"]))
                projected = projection["evidence"][0]["original_passages"]
                self.assertEqual(5, len(projected))
                for original, item in zip(passages, projected):
                    self.assertEqual(original["text"], projection["passage_texts"][item["text_ref"]])
                    self.assertEqual(original["ref"], item["ref"])
                    self.assertEqual(original["claim_id"], item["claim_id"])
                other = projection["evidence"][1]["original_passages"][0]
                self.assertEqual("other-source", other["ref"])
                self.assertEqual("other-claim", other["claim_id"])
                self.assertEqual(projected[4]["text_ref"], other["text_ref"])

    def test_request_budget_splits_scoring_without_discarding_evidence(self) -> None:
        batch = [paragraph("p1"), paragraph("p2")]
        tail = "Late result: 53% yield and 98% ee."
        evidence = {p["paragraph_id"]: {"evidence": [{
            "paper_id": "P001", "original_passages": [
                {"ref": "source", "text": "Evidence context. " * 100 + tail},
            ],
        }]} for p in batch}
        calls = []

        def model(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                raise feedback_loop.ProviderRequestBodyBudgetExceeded("request too large")
            ids = [p["paragraph_id"] for p in batch if f'"paragraph_id": "{p["paragraph_id"]}"' in prompt]
            return batch_result(ids, 4, 4)

        with tempfile.TemporaryDirectory() as raw, mock.patch.object(feedback_loop, "update_status"), mock.patch.object(
            feedback_loop, "call_json_model", side_effect=model,
        ):
            result = feedback_loop.request_paragraph_score_batches(
                Path(raw), rubric=rubric(), paragraphs=batch, evidence=evidence, preflight={},
                goal=90, paragraph_goal=85, draft_structure=[], prior_quality_context={}, label_prefix="Test",
            )
        self.assertEqual([["p1"], ["p2"]], [[p["paragraph_id"] for p in b] for b, _ in result])
        self.assertEqual(3, len(calls))
        self.assertTrue(all(tail in prompt for prompt in calls))

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

    def test_http_503_retries_the_failed_provider_call_with_bounded_backoff(self) -> None:
        failures = [
            urllib.error.HTTPError(
                "https://provider.example/v1/chat/completions",
                503,
                "Service Unavailable",
                {},
                io.BytesIO(b'{"error":{"code":"model_not_found"}}'),
            )
            for _ in range(5)
        ]
        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://provider.example/v1",
            "REVIEW_WRITING_WIRE_API": "chat-completions",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            feedback_loop.urllib.request,
            "urlopen",
            side_effect=failures,
        ) as urlopen, mock.patch.object(feedback_loop.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                RuntimeError,
                "HTTP 503 after 5 provider attempts.*model_not_found",
            ):
                feedback_loop.call_json_model("score this batch", label="batch")

        self.assertEqual(urlopen.call_count, 5)
        self.assertEqual(sleep.call_count, 4)

    def test_request_body_budget_failure_does_not_repeat_the_same_payload(self) -> None:
        failure = urllib.error.HTTPError(
            "https://provider.example/v1/chat/completions",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(
                b'{"error":{"code":"request_body_budget_exhausted",'
                b'"message":"relay request body budget is exhausted"}}'
            ),
        )
        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://provider.example/v1",
            "REVIEW_WRITING_WIRE_API": "chat-completions",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            feedback_loop.urllib.request,
            "urlopen",
            side_effect=failure,
        ) as urlopen, mock.patch.object(feedback_loop.time, "sleep") as sleep:
            with self.assertRaises(feedback_loop.ProviderRequestBodyBudgetExceeded):
                feedback_loop.call_json_model("oversized prompt", label="rewrite")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_concurrency_saturation_uses_extended_retry_window(self) -> None:
        failures = [
            urllib.error.HTTPError(
                "https://provider.example/v1/chat/completions",
                503,
                "Service Unavailable",
                {},
                io.BytesIO(
                    b'{"error":{"code":"too_many_concurrent_requests",'
                    b'"message":"relay is handling too many concurrent requests"}}'
                ),
            )
            for _ in range(8)
        ]
        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://provider.example/v1",
            "REVIEW_WRITING_WIRE_API": "chat-completions",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            feedback_loop.urllib.request,
            "urlopen",
            side_effect=failures,
        ) as urlopen, mock.patch.object(feedback_loop.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                RuntimeError,
                "HTTP 503 after 8 provider attempts.*too_many_concurrent_requests",
            ):
                feedback_loop.call_json_model("score this batch", label="batch")

        self.assertEqual(urlopen.call_count, 8)
        self.assertEqual(sleep.call_count, 7)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [5.0, 10.0, 20.0, 30.0, 30.0, 30.0, 30.0],
        )

    def test_only_transient_paragraph_provider_failures_are_deferred(self) -> None:
        self.assertTrue(
            feedback_loop.recoverable_paragraph_provider_failure(
                RuntimeError(
                    "Paragraph rewrite S02-p5 failed with HTTP 503: "
                    "too_many_concurrent_requests"
                )
            )
        )
        self.assertTrue(
            feedback_loop.recoverable_paragraph_provider_failure(
                feedback_loop.ProviderDeadlineExceeded("HTTP 524")
            )
        )
        self.assertFalse(
            feedback_loop.recoverable_paragraph_provider_failure(
                RuntimeError("Paragraph rewrite failed with HTTP 401: Unauthorized")
            )
        )

    def test_rewrite_queue_checkpoint_counts_terminal_paragraph_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rewrite_checkpoint.json"
            payload = feedback_loop.write_rewrite_queue_checkpoint(
                path,
                project_id="project-1",
                run_id="run-1",
                iteration=2,
                source_draft_sha256="source",
                current_draft_sha256="current",
                rewrite_items=[
                    {"paragraph_id": "p1", "status": "completed"},
                    {"paragraph_id": "p2", "status": "deferred"},
                    {"paragraph_id": "p3", "status": "rewriting"},
                ],
                accepted=1,
                rejected=0,
                deferred=1,
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(2, payload["rewrite_completed"])
        self.assertEqual(1, payload["rewrite_deferred"])
        self.assertEqual(payload, persisted)

    def test_batch_continues_after_one_paragraph_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "review-projects" / "project-1" / "04_first_draft"
            first.mkdir(parents=True)
            source_markdown = (
                "# Review\n\n"
                "Original first paragraph [1].\n\n"
                "<!-- paragraph_id: p1 -->\n\n"
                "Original second paragraph [2].\n\n"
                "<!-- paragraph_id: p2 -->\n"
            )
            (first / "first_draft.md").write_text(source_markdown, encoding="utf-8")
            preflight = {
                "hard_regressions": [],
                "paragraph_checks": [
                    {"paragraph_id": "p1", "word_range_applicable": False},
                    {"paragraph_id": "p2", "word_range_applicable": False},
                ],
            }
            source_evaluation = {
                "total_score": 70,
                "pass_threshold": 90,
                "decision": "FAIL",
                "hard_gate_failures": [],
                "paragraph_scores": [
                    {
                        "paragraph_id": "p1",
                        "score": 60,
                        "route": "section_rewrite",
                        "failed_dimensions": ["scientific_synthesis"],
                    },
                    {
                        "paragraph_id": "p2",
                        "score": 60,
                        "route": "section_rewrite",
                        "failed_dimensions": ["scientific_synthesis"],
                    },
                ],
                "paragraph_failures": [
                    {
                        "paragraph_id": "p1",
                        "score": 60,
                        "route": "section_rewrite",
                        "diagnosis": "Improve p1.",
                        "failed_dimensions": ["P02"],
                    },
                    {
                        "paragraph_id": "p2",
                        "score": 60,
                        "route": "section_rewrite",
                        "diagnosis": "Improve p2.",
                        "failed_dimensions": ["P02"],
                    },
                ],
            }
            final_evaluation = {
                **source_evaluation,
                "total_score": 80,
                "paragraph_scores": [
                    {
                        "paragraph_id": "p1",
                        "score": 60,
                        "route": "section_rewrite",
                        "failed_dimensions": ["scientific_synthesis"],
                    },
                    {
                        "paragraph_id": "p2",
                        "score": 90,
                        "route": "pass",
                        "failed_dimensions": [],
                    },
                ],
            }
            paragraphs = feedback_loop.parse_marked_paragraphs(source_markdown)
            evaluations = [
                (preflight, source_evaluation, {}, paragraphs, {"p1": {}, "p2": {}}),
                (preflight, final_evaluation, {}, paragraphs, {"p1": {}, "p2": {}}),
            ]
            args = SimpleNamespace(
                review_root=str(root),
                project_id="project-1",
                goal=90.0,
                paragraph_goal=85.0,
                max_iterations=1,
                min_improvement=1.0,
                min_case_words=1,
                max_case_words=100,
                evaluate_only=False,
            )

            def rewrite_response(_prompt: str, *, label: str):
                if "p1" in label:
                    raise RuntimeError(
                        "Paragraph rewrite p1 failed with HTTP 503 after 8 provider "
                        "attempts: too_many_concurrent_requests"
                    )
                return {"text": "Improved second paragraph [2]."}

            with mock.patch.object(
                feedback_loop,
                "ensure_prose_paragraph_markers",
                return_value=(
                    source_markdown,
                    {"prose_paragraph_count": 2, "changed": False},
                ),
            ), mock.patch.object(
                feedback_loop,
                "evaluate_current_draft",
                side_effect=evaluations,
            ) as full_evaluate, mock.patch.object(
                feedback_loop,
                "evaluate_changed_paragraphs",
                return_value=(preflight, final_evaluation, {"p2": {}}),
            ) as changed_evaluate, mock.patch.object(
                feedback_loop,
                "call_json_model",
                side_effect=rewrite_response,
            ), mock.patch.object(feedback_loop, "record_paragraph_history"):
                result = feedback_loop.run_feedback_loop(args)

            checkpoint = json.loads(
                (first / "feedback_loop_rewrite_checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            status = json.loads(
                (first / "feedback_loop_status.json").read_text(encoding="utf-8")
            )
            review = json.loads(
                (first / "batch_review_candidates.json").read_text(encoding="utf-8")
            )

        self.assertEqual("released", result["status"])
        self.assertEqual(1, checkpoint["rewrite_deferred"])
        self.assertEqual("deferred", checkpoint["items"][0]["status"])
        self.assertEqual("completed", checkpoint["items"][1]["status"])
        self.assertEqual(1, status["rewrite_deferred"])
        self.assertEqual(["p1"], status["deferred_paragraph_ids"])
        self.assertEqual(["p2"], [item["paragraph_id"] for item in review["changes"]])
        self.assertEqual(2, full_evaluate.call_count)
        changed_evaluate.assert_called_once()

    def test_rewrite_prompt_compacts_many_papers_to_a_bounded_payload(self) -> None:
        evidence = {
            "paragraph_id": "S02-p1",
            "paper_ids": [f"P{index:03d}" for index in range(30)],
            "local_source_available": True,
            "original_source_ready": True,
            "evidence_scope": "retrieved_original_full_text",
            "evidence": [
                {
                    "paper_id": f"P{index:03d}",
                    "title": "Title " + ("T" * 300),
                    "abstract": "UNBOUNDED_ABSTRACT " + ("A" * 2_000),
                    "main_content": "M" * 2_000,
                    "local_source_available": True,
                    "original_text_available": True,
                    "original_passages": [
                        {
                            "ref": f"P{index:03d}:p1:b1",
                            "page": 1,
                            "text": "E" * 700,
                        }
                    ],
                }
                for index in range(30)
            ],
        }

        prompt = feedback_loop.rewrite_prompt(
            {"paragraph_id": "S02-p1", "text": "Synthesis paragraph [1-30]."},
            {"route": "section_rewrite", "diagnosis": "Improve readability."},
            evidence,
            1,
            280,
            word_range_applicable=False,
        )

        self.assertLess(len(prompt.encode("utf-8")), 30_000)
        self.assertNotIn("UNBOUNDED_ABSTRACT", prompt)
        for paper_id in evidence["paper_ids"]:
            self.assertIn(paper_id, prompt)

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
                {
                    "paragraph_id": "sec1-p1",
                    "score": 70,
                    "route": "section_rewrite",
                    "failed_dimensions": ["scientific_synthesis"],
                },
                {
                    "paragraph_id": "sec1-p2",
                    "score": 80,
                    "route": "section_rewrite",
                },
            ],
        }
        candidate_evaluation = {
            "total_score": 80,
            "paragraph_scores": [
                {
                    "paragraph_id": "sec1-p1",
                    "score": 90,
                    "route": "pass",
                    "failed_dimensions": [],
                },
                {
                    "paragraph_id": "sec1-p2",
                    "score": 70,
                    "route": "section_rewrite",
                },
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
                "paragraph_score_regression" in row.get("reasons", [])
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

    def test_batch_review_keeps_a_lower_scored_candidate_when_evidence_improves(self) -> None:
        source = (
            "# Review\n\n"
            "Unsupported detailed claim [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        candidate = source.replace(
            "Unsupported detailed claim [1].",
            "The available source supports only a limited interpretation [1].",
        )
        source_evaluation = {
            "paragraph_scores": [
                {
                    "paragraph_id": "sec1-p1",
                    "score": 90,
                    "source_check_status": "unsupported",
                    "unsupported_claims": ["Unsupported detailed claim"],
                }
            ]
        }
        candidate_evaluation = {
            "paragraph_scores": [
                {
                    "paragraph_id": "sec1-p1",
                    "score": 89.5,
                    "source_check_status": "partially_supported",
                    "unsupported_claims": [],
                    "source_evidence_refs": ["P001:p1:b2"],
                }
            ]
        }
        preflight = {
            "paragraph_checks": [
                {
                    "paragraph_id": "sec1-p1",
                    "word_range_applicable": False,
                    "issues": [],
                }
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
            candidate_evidence={
                "sec1-p1": {
                    "paper_ids": ["P001"],
                    "evidence_scope": "local_full_text",
                }
            },
            min_words=1,
            max_words=200,
            iteration=1,
        )

        self.assertEqual([], excluded)
        self.assertTrue(best["sec1-p1"]["accuracy_improved"])
        self.assertEqual(-0.5, best["sec1-p1"]["score_delta"])

    def test_batch_review_rejects_accuracy_candidate_outside_score_tolerance(self) -> None:
        source = (
            "# Review\n\nUnsupported detail [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        candidate = source.replace(
            "Unsupported detail [1].",
            "The source supports a narrower interpretation [1].",
        )
        source_evaluation = {
            "paragraph_scores": [
                {
                    "paragraph_id": "sec1-p1",
                    "score": 90,
                    "route": "local_source_recheck",
                    "source_check_status": "unsupported",
                    "unsupported_claims": ["Unsupported detail"],
                }
            ]
        }
        candidate_evaluation = {
            "paragraph_scores": [
                {
                    "paragraph_id": "sec1-p1",
                    "score": 88,
                    "route": "pass",
                    "source_check_status": "verified",
                    "unsupported_claims": [],
                }
            ]
        }
        preflight = {
            "paragraph_checks": [
                {
                    "paragraph_id": "sec1-p1",
                    "word_range_applicable": False,
                    "issues": [],
                }
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

        self.assertFalse(best)
        self.assertIn("paragraph_score_regression", excluded[0]["reasons"])


if __name__ == "__main__":
    unittest.main()
