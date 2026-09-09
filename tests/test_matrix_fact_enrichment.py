from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-literature-matrix-outline"
    / "scripts"
    / "enrich_matrix_facts.py"
)
SPEC = importlib.util.spec_from_file_location("matrix_fact_enrichment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


class MatrixFactEnrichmentTests(unittest.TestCase):
    def test_correction_retires_damaged_fact_only_after_same_experiment_verification(self):
        for correction_supported in (True, False):
            with self.subTest(correction_supported=correction_supported):
                self.setUp()
                result, state = self._agent_result(), {"max_supplement_rounds": 1}
                original_id = result["facts"][0]["fact_id"]
                source = {"evidence_key": "restored", "chunk_id": "pdf-text:current:p4", "question_ids": ["quantitative_results"],
                          "content": "The optimized protocol gave 91% yield and 96% ee.", "source_lineage_hash": "lineage"}
                def model(prompt, **kwargs):
                    if kwargs["required_list"] == "facts":
                        return {"facts": [{"field_id": "quantitative_results", "value": "91% yield and 96% ee",
                            "support_excerpt": source["content"], "evidence_key": "restored", "confidence": 0.99,
                            "correction_of_fact_id": original_id}]}
                    return {"verdicts": [{"fact_id": f["fact_id"], "status": "supported", "reason": "Same experiment verified against restored text.",
                        "source_damage": f["fact_id"] == original_id, "correction_supported": correction_supported}
                        for f in result["facts"]]}
                PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=lambda _: [source], state=state, report=lambda _: None)
                self.assertEqual(2, len(result["facts"]))
                old, new = result["facts"]
                self.assertFalse(PIPELINE.fact_is_usable(old))
                self.assertTrue(PIPELINE.fact_is_usable(new))
                if correction_supported:
                    self.assertEqual(new["fact_id"], old["superseded_by_fact_id"])
                    self.assertFalse(state.get("unresolved_requests"))
                    self.paper["required_fact_roles"] = ["quantitative_results"]
                    self.assertEqual("complete", PIPELINE.refresh_fact_status(self.paper, result, state)["status"])
                    self.assertEqual(1, result["fact_coverage"]["fact_count"])
                    self.assertEqual(1, result["fact_coverage"]["superseded_fact_count"])
                else:
                    self.assertNotIn("superseded_by_fact_id", old)

    def test_resume_keeps_unresolved_questions_alongside_pending_questions(self):
        result = self._agent_result()
        result["facts"][0]["verification"] = {"contract": PIPELINE.FACT_VALIDATION_VERSION, "status": "supported"}
        state = {"pending_requests": [{"field_id": "scope", "query": "external cohort"}],
                 "unresolved_requests": [{"field_id": "method_conditions", "query": "annealing temperature"}]}
        seen = []
        PIPELINE.run_fact_agent(self.paper, result, model_call=lambda *a, **k: self.fail("No extraction needed"),
            retrieve=lambda request: seen.append(request["questions"][0]["field_id"]) or [], state=state, report=lambda _: None)
        self.assertEqual(["scope", "method_conditions"], seen)

    def test_failed_lookup_does_not_stop_other_problem_or_poison_no_progress_cache(self):
        result, state = self._agent_result(), {"max_supplement_rounds": 1}
        failed = {"field_id": "scope", "query": "external validation", "experiment_id": "cohort-A"}
        good = {"field_id": "quantitative_results", "query": "control yield", "experiment_id": "control-B"}
        result["evidence_requests"] = [failed, good]
        source = {"evidence_key": "new", "chunk_id": "control", "question_ids": ["quantitative_results"],
                  "content": "The control gave 42% yield.", "source_lineage_hash": "lineage"}
        called = []
        def retrieve(request):
            question = request["questions"][0]
            called.append(question["experiment_id"])
            if question["experiment_id"] == "cohort-A":
                raise RuntimeError("index temporarily unavailable")
            # This is how an in-process trusted retriever updates the registry.
            self.paper["evidence_candidates"].append(source)
            return [source]
        def model(prompt, **kwargs):
            if kwargs["required_list"] == "facts":
                return {"facts": [{"field_id": "quantitative_results", "value": "42% yield",
                    "support_excerpt": source["content"], "evidence_key": "new", "confidence": 0.99}]}
            return {"verdicts": [{"fact_id": f["fact_id"], "status": "supported", "reason": "Exact experiment matched."}
                                 for f in result["facts"]]}
        PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=retrieve, state=state, report=lambda _: None)
        self.assertEqual(["cohort-A", "control-B"], called)
        self.assertEqual(2, len(result["facts"]))
        self.assertTrue(all(PIPELINE.fact_is_usable(f) for f in result["facts"]))
        self.assertTrue(state["pending_requests"])
        self.assertFalse(state.get("no_progress_requests"))

    def test_source_damage_cannot_be_overridden_by_supported_model_verdict(self):
        result, state = self._agent_result(), {}
        def model(*a, **k):
            return {"verdicts": [{"fact_id": result["facts"][0]["fact_id"], "status": "supported",
                                  "source_damage": True, "reason": "Identifier is truncated in parsed text."}]}
        PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=lambda _: [], state=state, report=lambda _: None)
        self.assertFalse(PIPELINE.fact_is_usable(result["facts"][0]))
        self.assertTrue(state["unresolved_requests"])

    def test_known_source_can_be_reread_once_for_an_unextracted_experiment(self):
        result, state = self._agent_result(), {}
        source = {"evidence_key": "known", "chunk_id": "control", "question_ids": ["quantitative_results"],
                  "content": "The control gave 42% yield.", "source_lineage_hash": "lineage"}
        self.paper["evidence_candidates"].append(source)
        request = {"field_id": "quantitative_results", "query": "control yield", "target_terms": ["control"], "experiment_id": "control"}
        result["evidence_requests"] = [request]
        extractions = []
        def model(prompt, **kwargs):
            if kwargs["required_list"] == "facts":
                extractions.append(prompt)
                return {"facts": [{"field_id": "quantitative_results", "value": "42% yield",
                    "support_excerpt": source["content"], "evidence_key": "known", "confidence": 0.99}],
                    "evidence_requests": [{**request, "query": "Please examine the control result"}]}
            return {"verdicts": [{"fact_id": f["fact_id"], "status": "supported", "reason": "Exact experiment matched."}
                                 for f in result["facts"]]}
        PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=lambda _: [source], state=state, report=lambda _: None)
        self.assertEqual(1, len(extractions))
        self.assertEqual(2, len(result["facts"]))
        self.assertEqual("supplement_budget_reached", state["stop_reason"])

        # A new attempt with the same source cannot restart a completed lookup.
        PIPELINE.run_fact_agent(self.paper, result,
            model_call=lambda *a, **k: self.fail("Completed supplement must be reused"),
            retrieve=lambda _: self.fail("Only one supplement round is allowed"), state=state, report=lambda _: None)

    def test_empty_supplement_does_not_clear_a_question_when_other_roles_are_complete(self):
        result, state = self._agent_result(), {"max_supplement_rounds": 1}
        self.paper["required_fact_roles"] = ["quantitative_results"]
        result["facts"][0]["verification"] = {"contract": PIPELINE.FACT_VALIDATION_VERSION, "status": "supported"}
        result["evidence_requests"] = [{"field_id": "quantitative_results", "query": "control result", "experiment_id": "control"}]
        source = {"evidence_key": "unanswered", "content": "The control experiment is discussed elsewhere.",
                  "question_ids": ["quantitative_results"]}
        PIPELINE.run_fact_agent(self.paper, result, model_call=lambda *a, **k: {"facts": [], "evidence_requests": []},
            retrieve=lambda _: [source], state=state, report=lambda _: None)
        self.assertTrue(state["pending_requests"])
        self.assertEqual("partial", result["status"])

    def test_failed_role_and_classification_cannot_make_extraction_complete(self):
        paper = {**self.paper, "required_fact_roles": ["quantitative_results"]}
        result = self._agent_result()
        result["facts"][0]["verification"] = {"contract": PIPELINE.FACT_VALIDATION_VERSION, "status": "supported"}
        result["facts"].append({"field_id": "topic_partition", "value": "category", "evidence_refs": [{"evidence_key": "k"}]})
        result["failed_fields"] = ["scope"]
        self.assertEqual("partial", PIPELINE.refresh_fact_status(paper, result)["status"])
        self.assertEqual(1, result["fact_coverage"]["scientific_fact_count"])
        result["failed_fields"] = []
        self.assertEqual("complete", PIPELINE.refresh_fact_status(paper, result)["status"])
        result["facts"] = result["facts"][1:]
        self.assertNotEqual("complete", PIPELINE.refresh_fact_status(paper, result)["status"])

    def test_rejected_fact_has_a_reason_and_bounded_retrieval_request(self):
        result = PIPELINE.normalize_result(self.paper, {"facts": [{
            "field_id": "quantitative_results", "value": "99% yield", "support_excerpt": "91% yield and 96% ee",
            "evidence_key": "sha256:abc", "confidence": 0.95}]})
        self.assertEqual("numeric_token_mismatch", result["normalization_rejections"][0]["reasons"][0])
        requests, state = [], {}
        for _ in range(2):
            PIPELINE.run_fact_agent(self.paper, result, model_call=lambda *a, **k: self.fail("No fact survived for audit"),
                                    retrieve=lambda request: requests.append(request) or [], state=state, report=lambda _: None)
        self.assertEqual(1, len(requests))
        self.assertEqual("quantitative_results", requests[0]["questions"][0]["field_id"])

    def test_unsuccessful_local_supplement_is_not_reported_as_complete_or_repeated(self):
        result, state = self._agent_result(), {}
        result["evidence_requests"] = [{"field_id": "quantitative_results", "query": "control yield"}]
        attempts = []
        def retrieve(request):
            attempts.append(request)
            return []
        def model(*args, **kwargs):
            return {"verdicts": [{"fact_id": result["facts"][0]["fact_id"], "status": "supported", "reason": "Exact reported metric."}]}
        for _ in range(2):
            PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=retrieve, state=state, report=lambda _: None)
        self.assertEqual(1, len(attempts))
        self.assertEqual("partial", result["status"])
        self.assertTrue(state["unresolved_requests"])

    def _agent_result(self):
        return PIPELINE.normalize_result(self.paper, {"facts": [{
            "field_id": "quantitative_results", "value": "91% yield and 96% ee",
            "support_excerpt": "91% yield and 96% ee", "evidence_key": "sha256:abc",
            "epistemic_status": "direct_source_report", "confidence": 0.95,
        }]})

    def test_fact_agent_audits_relation_and_reuses_completed_verdict(self):
        result, state, phases = self._agent_result(), {}, []
        calls = []
        def model(prompt, **kwargs):
            calls.append(prompt)
            return {"verdicts": [{"fact_id": result["facts"][0]["fact_id"], "status": "supported",
                                  "reason": "Same optimized protocol: yield 91%, ee 96%."}]}
        for _ in range(2):
            PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=lambda _: [],
                                    state=state, report=phases.append)
        self.assertEqual(1, len(calls))
        self.assertEqual("supported", result["facts"][0]["verification"]["status"])
        self.assertIn("verified", phases)

    def test_registered_custom_field_enters_the_same_semantic_audit(self):
        field = "photocatalyst_excited_state_behavior"
        excerpt = "Photocatalyst PC-A was reported as a strong excited-state oxidant."
        self.paper["required_fact_roles"] = [field]
        self.paper["evidence_candidates"][0].update(
            content=excerpt,
            question_ids=[field],
        )
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": field,
                        "value": excerpt,
                        "support_excerpt": excerpt,
                        "evidence_key": "sha256:abc",
                        "confidence": 0.95,
                    }
                ]
            },
        )
        calls = []

        def model(prompt, **_kwargs):
            calls.append(prompt)
            return {
                "verdicts": [
                    {
                        "fact_id": result["facts"][0]["fact_id"],
                        "status": "supported",
                        "reason": "The subject and reported relation match the quotation.",
                    }
                ]
            }

        PIPELINE.run_fact_agent(
            self.paper,
            result,
            model_call=model,
            retrieve=lambda _request: [],
            state={},
            report=lambda _phase: None,
        )
        self.assertEqual(1, len(calls))
        self.assertEqual("supported", result["facts"][0]["verification"]["status"])
        self.assertTrue(PIPELINE.fact_is_usable(result["facts"][0], purpose="detail"))

    def test_non_fact_question_field_gets_an_explicit_audit_rejection(self):
        excerpt = "The study compares the reported catalyst systems."
        self.paper["evidence_candidates"][0].update(
            content=excerpt,
            question_ids=["section_focus"],
        )
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "section_focus",
                        "value": excerpt,
                        "support_excerpt": excerpt,
                        "evidence_key": "sha256:abc",
                        "confidence": 0.95,
                    }
                ]
            },
        )

        PIPELINE.run_fact_agent(
            self.paper,
            result,
            model_call=lambda *_args, **_kwargs: self.fail(
                "A non-fact query field must not enter semantic fact verification."
            ),
            retrieve=lambda _request: [],
            state={},
            report=lambda _phase: None,
        )
        verification = result["facts"][0]["verification"]
        self.assertEqual("rejected", verification["status"])
        self.assertIn("not registered", verification["reason"])
        self.assertFalse(PIPELINE.fact_is_usable(result["facts"][0]))

    def test_fact_agent_does_not_use_omitted_or_rejected_verdicts(self):
        result = self._agent_result()
        PIPELINE.run_fact_agent(self.paper, result, model_call=lambda *a, **k: {"verdicts": []},
                                retrieve=lambda _: [], state={}, report=lambda _: None)
        self.assertFalse(PIPELINE.fact_is_usable(result["facts"][0]))
        self.assertEqual("context_only", result["facts"][0]["support_level"])

    def test_fact_agent_failure_resumes_without_keeping_stale_error(self):
        result, state = self._agent_result(), {}
        def unavailable(*a, **k):
            raise RuntimeError("temporary outage")
        PIPELINE.run_fact_agent(self.paper, result, model_call=unavailable, retrieve=lambda _: [],
                                state=state, report=lambda _: None)
        self.assertFalse(PIPELINE.fact_is_usable(result["facts"][0]))
        self.assertIn("error", state)
        PIPELINE.run_fact_agent(self.paper, result, model_call=lambda *a, **k: {"verdicts": [
            {"fact_id": result["facts"][0]["fact_id"], "status": "supported", "reason": "Matched both reported metrics."}
        ]}, retrieve=lambda _: [], state=state, report=lambda _: None)
        self.assertNotIn("error", state)
        self.assertEqual("complete", result["status"])
        self.assertIn("object_input", result["fact_coverage"]["missing_fact_roles"])

    def test_fact_agent_supplement_is_bounded_and_keeps_previous_verification(self):
        result, state = self._agent_result(), {"max_supplement_rounds": 1}
        initial_id = result["facts"][0]["fact_id"]
        result["evidence_requests"] = [{"field_id": "quantitative_results", "query": "control experiment yield"}]
        supplement = {"evidence_key": "sha256:control", "content": "The control gave 42% yield.",
                      "source_lineage_hash": "lineage", "chunk_id": "control", "question_ids": ["quantitative_results"]}
        def model(prompt, **kwargs):
            if kwargs["required_list"] == "facts":
                return {"facts": [{"field_id": "quantitative_results", "value": "42% yield",
                    "support_excerpt": supplement["content"], "evidence_key": supplement["evidence_key"],
                    "confidence": 0.95, "epistemic_status": "direct_source_report"}],
                    "evidence_requests": [{"field_id": "quantitative_results", "query": "more controls"}]}
            return {"verdicts": [{"fact_id": f["fact_id"], "status": "supported", "reason": "Matched the specified experiment."}
                                 for f in result["facts"]]}
        retrieved = []
        def retrieve(request):
            retrieved.append(request)
            return [supplement]
        PIPELINE.run_fact_agent(self.paper, result, model_call=model, retrieve=retrieve, state=state, report=lambda _: None)
        self.assertEqual(1, len(retrieved))
        self.assertEqual(2, len(result["facts"]))
        self.assertEqual(initial_id, result["facts"][0]["fact_id"])
        self.assertTrue(all(PIPELINE.fact_is_usable(f) for f in result["facts"]))
        self.assertEqual("supplement_budget_reached", state["stop_reason"])

    def setUp(self) -> None:
        self.paper = {
            "paper_id": "P001",
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:abc",
                    "chunk_id": "chunk-1",
                    "page_start": 4,
                    "page_end": 4,
                    "section_path": ["Results"],
                    "content_type": "text",
                    "content": "The optimized protocol afforded the product in 91% yield and 96% ee.",
                    "source_lineage_hash": "lineage",
                    "question_ids": ["quantitative_results"],
                }
            ],
            "partition_evidence_candidates": [
                {
                    "evidence_key": "sha256:partition",
                    "chunk_id": "chunk-2",
                    "page_start": 2,
                    "page_end": 2,
                    "section_path": ["Abstract"],
                    "content_type": "text",
                    "content": "An enantioselective protocol furnished the allene in 96% ee.",
                    "source_lineage_hash": "lineage",
                }
            ],
        }

    def test_fact_requires_exact_source_excerpt_and_keeps_page_reference(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The reported optimized result was 91% yield and 96% ee.",
                        "support_excerpt": "afforded the product in 91% yield and 96% ee",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.93,
                        "evidence_ceiling": "Only the optimized experiment is supported.",
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(result["facts"]))
        fact = result["facts"][0]
        self.assertEqual(4, fact["evidence_refs"][0]["page_start"])
        self.assertEqual("sha256:abc", fact["evidence_refs"][0]["evidence_key"])
        self.assertTrue(fact["evidence_ceiling"])
        self.assertEqual("body", fact["source_channel"])
        self.assertEqual("direct", fact["support_level"])
        self.assertEqual("not_required", result["review_status"])
        self.assertEqual("scientific-fact/2", fact["fact_schema_version"])
        self.assertEqual("reported_result", fact["fact_type"])
        self.assertEqual("sha256:abc", fact["source_span"]["evidence_key"])
        self.assertEqual(
            "baseline_plus_targeted_recheck",
            result["fact_extraction_profile"]["mode"],
        )

    def test_hallucinated_support_excerpt_is_rejected(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The result was quantitative.",
                        "support_excerpt": "The reaction gave a quantitative yield.",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 1,
                        "evidence_ceiling": "",
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual([], result["facts"])

    def test_topic_partition_classification_requires_bounded_source_evidence(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_partition_classification": {
                    "partition": "enantioselective evidence (ESE)",
                    "confidence": 0.94,
                    "evidence_key": "sha256:partition",
                    "support_excerpt": "enantioselective protocol furnished the allene in 96% ee",
                    "rationale": "The source explicitly reports an enantioselective outcome.",
                    "evidence_ceiling": "Only the reported experiment is classified.",
                },
            },
            ["racemic evidence", "enantioselective evidence (ESE)"],
        )

        classification = result["topic_partition_classification"]
        self.assertEqual("classified", classification["status"])
        self.assertEqual(
            "enantioselective evidence (ESE)", classification["partition"]
        )
        self.assertEqual(2, classification["evidence_refs"][0]["page_start"])

    def test_topic_partition_classification_does_not_infer_from_absence(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_partition_classification": {
                    "partition": "racemic evidence",
                    "confidence": 0.91,
                    "evidence_key": "sha256:partition",
                    "support_excerpt": "The paper does not mention an enantioselective protocol.",
                    "rationale": "No ee was found.",
                },
            },
            ["racemic evidence", "enantioselective evidence (ESE)"],
        )

        classification = result["topic_partition_classification"]
        self.assertEqual("insufficient_evidence", classification["status"])
        self.assertEqual("", classification["partition"])
        self.assertEqual([], classification["evidence_refs"])

    def test_numeric_fact_cannot_introduce_value_absent_from_quote(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The product was obtained in 99% yield.",
                        "support_excerpt": "afforded the product in 91% yield and 96% ee",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.95,
                    }
                ],
                "failed_fields": [],
            },
        )
        self.assertEqual([], result["facts"])

    def test_partition_candidate_can_supply_a_required_fact_role(self) -> None:
        paper = {
            **self.paper,
            "required_fact_roles": ["object_input", "method_conditions"],
            "partition_evidence_candidates": [
                {
                    "evidence_key": "sha256:partition-role",
                    "chunk_id": "chunk-role",
                    "page_start": 5,
                    "page_end": 5,
                    "section_path": ["Results"],
                    "content_type": "text",
                    "content": (
                        "Terminal alkynes and aldehydes were reacted with "
                        "0.4 mol catalyst at room temperature."
                    ),
                    "source_lineage_hash": "lineage",
                }
            ],
        }
        result = PIPELINE.normalize_result(
            paper,
            {
                "facts": [
                    {
                        "field_id": "object_input",
                        "value": "The reported inputs were terminal alkynes and aldehydes.",
                        "support_excerpt": "Terminal alkynes and aldehydes were reacted",
                        "evidence_key": "sha256:partition-role",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.94,
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual(1, len(result["facts"]))
        self.assertEqual("object_input", result["facts"][0]["field_id"])

    def test_chemical_locants_do_not_fail_the_numeric_guard(self) -> None:
        paper = {
            **self.paper,
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:locants",
                    "chunk_id": "chunk-locants",
                    "page_start": 6,
                    "page_end": 6,
                    "section_path": ["Scope"],
                    "content_type": "text",
                    "content": "The product was isolated in 45% yield.",
                    "source_lineage_hash": "lineage",
                    "question_ids": ["quantitative_results"],
                }
            ],
        }
        result = PIPELINE.normalize_result(
            paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "Buta-2,3-dien-1-ol was isolated in 45% yield.",
                        "support_excerpt": "The product was isolated in 45% yield.",
                        "evidence_key": "sha256:locants",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.92,
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual(1, len(result["facts"]))

    def test_tex_spacing_variants_still_match_the_exact_source(self) -> None:
        paper = {
            **self.paper,
            "evidence_candidates": [
                {
                    "evidence_key": "sha256:tex",
                    "chunk_id": "chunk-tex",
                    "page_start": 7,
                    "page_end": 7,
                    "section_path": ["Conditions"],
                    "content_type": "text",
                    "content": r"The reaction was maintained at $25\,\mathrm { C }$ for 2 h.",
                    "source_lineage_hash": "lineage",
                    "question_ids": ["method_conditions"],
                }
            ],
        }
        result = PIPELINE.normalize_result(
            paper,
            {
                "facts": [
                    {
                        "field_id": "method_conditions",
                        "value": r"The reported conditions were $25\,\mathrm { C}$ for 2 h.",
                        "support_excerpt": r"maintained at $25\,\mathrm { C}$ for 2 h",
                        "evidence_key": "sha256:tex",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.93,
                    }
                ],
                "failed_fields": [],
            },
        )

        self.assertEqual(1, len(result["facts"]))

    def test_failed_field_objects_are_normalized_without_stringifying(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [
                    {
                        "field_id": "scope",
                        "reason": "The bounded candidates contain no scope passage.",
                    }
                ],
            },
        )

        self.assertEqual(["scope"], result["failed_fields"])
        self.assertEqual("scope", result["failed_field_details"][0]["field_id"])
        self.assertIn("no scope passage", result["failed_field_details"][0]["reason"])

    def test_formal_tag_is_bound_to_classification_fact_and_evidence(self) -> None:
        axes = [
            {
                "axis_id": "method",
                "label": "Method family",
                "axis_role": "primary_organization",
                "partitions": [
                    {"partition_id": "enantio", "label": "Enantioselective method"}
                ],
            }
        ]
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_classification_assignments": [
                    {
                        "axis_id": "method",
                        "partition_id": "enantio",
                        "relation_to_paper": "primary_contribution",
                        "confidence": 0.94,
                        "evidence_key": "sha256:partition",
                        "support_excerpt": "An enantioselective protocol furnished the allene in 96% ee.",
                    }
                ],
                "classification_outcomes": [],
            },
            classification_axes=axes,
        )
        tag = result["evidence_backed_tags"]["method"][0]
        self.assertTrue(tag["fact_ids"])
        self.assertEqual("sha256:partition", tag["evidence_refs"][0]["evidence_key"])
        facts = {fact["fact_id"]: fact for fact in result["facts"]}
        self.assertEqual("topic_partition", facts[tag["fact_ids"][0]]["field_id"])

    def test_medium_confidence_fact_is_context_only_without_user_warning(self) -> None:
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [
                    {
                        "field_id": "quantitative_results",
                        "value": "The optimized result was 91% yield and 96% ee.",
                        "support_excerpt": "afforded the product in 91% yield and 96% ee",
                        "evidence_key": "sha256:abc",
                        "epistemic_status": "direct_source_report",
                        "confidence": 0.68,
                        "evidence_ceiling": "Context only.",
                    }
                ],
                "failed_fields": ["optional_scope_detail"],
            },
        )

        self.assertEqual("context_only", result["facts"][0]["support_level"])
        self.assertEqual("auto_resolved", result["review_status"])
        self.assertFalse(result["automatic_resolution"]["user_action_required"])

    def test_same_user_cache_is_revalidated_and_survives_provider_failure(self) -> None:
        cached_fact = {
            "fact_id": "MF-CACHED",
            "fact_schema_version": "scientific-fact/2",
            "field_id": "quantitative_results",
            "fact_type": "reported_result",
            "value": "The reported result was 91% yield and 96% ee.",
            "support_excerpt": "the product in 91% yield and 96% ee",
            "evidence_refs": [{"evidence_key": "sha256:abc"}],
        }
        paper = {
            **self.paper,
            "reused_fact_cache": {
                "facts": [cached_fact],
                "source_project_id": "project-old",
            },
        }

        merged = PIPELINE.merge_reused_fact_cache(
            {
                "paper_id": "P001",
                "status": "failed",
                "facts": [],
                "failed_fields": ["all"],
                "error": "provider unavailable",
            },
            paper,
            provider_failed=True,
        )

        self.assertEqual("partial", merged["status"])
        self.assertEqual("MF-CACHED", merged["facts"][0]["fact_id"])
        self.assertTrue(merged["fact_extraction_profile"]["cache_reused"])
        self.assertTrue(
            merged["fact_extraction_profile"]["provider_supplement_failed"]
        )

    def test_multiple_results_are_retained_but_metric_swaps_are_rejected(self) -> None:
        quote = self.paper["evidence_candidates"][0]["content"]
        base = {
            "field_id": "quantitative_results", "evidence_key": "sha256:abc",
            "support_excerpt": quote, "confidence": 0.95,
        }
        result = PIPELINE.normalize_result(self.paper, {"facts": [
            {**base, "value": "91% yield"}, {**base, "value": "96% ee"},
            {**base, "value": "96% yield and 91% ee"}, {**base, "value": "91% yield"},
        ]})
        self.assertEqual(["91% yield", "96% ee"], [fact["value"] for fact in result["facts"]])

    def test_cache_cannot_reuse_an_unverifiable_or_changed_quote(self) -> None:
        paper = {**self.paper, "reused_fact_cache": {"facts": [{
            "fact_id": "old", "field_id": "quantitative_results", "value": "99% ee",
            "support_excerpt": "99% ee", "evidence_refs": [{"evidence_key": "sha256:abc"}],
        }]}}
        result = PIPELINE.merge_reused_fact_cache({"facts": []}, paper)
        self.assertEqual([], result["facts"])

    def test_formal_axis_tag_can_supply_duplicate_topic_partition_route(self) -> None:
        axes = [
            {
                "axis_id": "stereochemical_regime",
                "label": "Stereochemical regime",
                "axis_role": "required_independent_discussion",
                "partitions": [
                    {
                        "partition_id": "eata",
                        "label": "Enantioselective ATA",
                    }
                ],
            }
        ]
        result = PIPELINE.normalize_result(
            self.paper,
            {
                "facts": [],
                "failed_fields": [],
                "topic_classification_assignments": [
                    {
                        "axis_id": "stereochemical_regime",
                        "partition_id": "eata",
                        "relation_to_paper": "primary_contribution",
                        "confidence": 0.94,
                        "evidence_key": "sha256:partition",
                        "support_excerpt": "An enantioselective protocol furnished the allene in 96% ee.",
                    }
                ],
            },
            ["racemic ATA", "enantioselective ATA (EATA)"],
            axes,
        )
        derived = PIPELINE.derive_topic_partition_from_formal_tags(
            result, ["racemic ATA", "enantioselective ATA (EATA)"]
        )

        self.assertEqual(
            "classified", derived["topic_partition_classification"]["status"]
        )
        self.assertEqual(
            "enantioselective ATA (EATA)",
            derived["topic_partition_classification"]["partition"],
        )


if __name__ == "__main__":
    unittest.main()
