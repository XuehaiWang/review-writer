from __future__ import annotations

import importlib.util
import json
import unittest
import tempfile
from copy import deepcopy
from unittest.mock import patch
from pathlib import Path

from review_writer_core.writing_contracts import derive_writing_scope_contract
from review_writer_core.scientific_facts import (
    attach_fact_to_evidence, build_fact_comparison, claim_assertion_ceiling,
    registered_fact_bindings,
)


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-section-drafting-figure-picking"
    / "scripts"
    / "generate_section_drafts.py"
)
SPEC = importlib.util.spec_from_file_location("section_academic_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


class SectionAcademicPipelineTests(unittest.TestCase):
    def historical_writing_artifact(self, proposed=None, evidence=None):
        """Build a saved-artifact fixture, independent of the retired planner."""
        proposed = proposed or self.proposed
        evidence = evidence or self.evidence
        facts = registered_fact_bindings(evidence, [row['paper_id'] for row in evidence])
        claim = deepcopy(proposed['paragraphs'][0]['claims'][0])
        selected = [facts[fid] for fid in claim.get('fact_ids', [])]
        keys = {ref['evidence_key'] for fact in selected for ref in fact['evidence_refs']} if selected else set(claim['evidence_keys'])
        sources = [row for row in evidence if row['evidence_key'] in keys]
        claim.update(
            claim_id='S02-p1-C01', paragraph_id='S02-p1',
            evidence_refs=[{key: row[key] for key in ('paper_id', 'chunk_id', 'evidence_key')} for row in sources],
            citation_group=[row['paper_id'] for row in sources],
            assertion_ceiling=claim_assertion_ceiling(sources, selected),
        )
        if selected:
            claim['fact_binding_status'] = 'explicit_fact_selection'
        paragraph = {**proposed['paragraphs'][0], 'paragraph_id': 'S02-p1', 'claim_ids': ['S02-p1-C01']}
        return ({'section_id': 'S02', 'components': []},
                {'section_id': 'S02', 'section_role': 'body', 'paragraphs': [paragraph], 'claims': [claim]})

    def test_empty_evidence_finishes_all_sections_without_model_and_reuses_notices(self):
        self._run_empty_evidence_case(lookup_complete=True)

    def test_unfinished_source_lookup_is_not_saved_as_completed_empty_evidence(self):
        self._run_empty_evidence_case(lookup_complete=False)

    def _run_empty_evidence_case(self, *, lookup_complete):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "review-projects" / "test"
            stage = project / "02_section_drafting"
            matrix = project / "01_matrix_outline"
            stage.mkdir(parents=True); matrix.mkdir()
            tasks = [{"section_id": sid, "heading": sid, "section_role": role,
                      "allowed_papers": ["P001"], "primary_papers": ["P001"], "supporting_papers": []}
                     for sid, role in [("S01", "body"), ("S02", "body")]]
            packages = [{"section_id": t["section_id"], "retrieval_mode": "insufficient_evidence",
                         "source_lookup_complete": lookup_complete, "hits": []} for t in tasks]
            for path, value in {stage / "section_tasks.json": tasks,
                    stage / "section_evidence.json": {"sections": packages},
                    matrix / "literature_matrix.json": {"rows": [{"paper_id": "P001", "title": "A paper"}]},
                    matrix / "section_blueprint.json": {"review_topic": "Topic", "sections": tasks}}.items():
                path.write_text(json.dumps(value), encoding="utf-8")
            with patch.object(PIPELINE.sys, "argv", [str(SCRIPT), "--review-root", str(root), "--project-id", "test", "--api-key", "test", "--model", "test"]), \
                 patch.object(PIPELINE, "load_dotenv", return_value={}), \
                 patch.object(PIPELINE, "load_blueprint_rule_pack", return_value="Use evidence"), \
                 patch.object(PIPELINE, "load_cross_study_synthesis_skill", return_value="Use evidence"), \
                 patch.object(PIPELINE, "call_structured_llm") as model:
                if not lookup_complete:
                    with self.assertRaisesRegex(SystemExit, "2 section\\(s\\) failed"):
                        PIPELINE.main()
                    model.assert_not_called()
                    return
                self.assertEqual(0, PIPELINE.main())
                checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
                self.assertEqual({"S01", "S02"}, set(checkpoint["entries"]))
                for entry in checkpoint["entries"].values():
                    self.assertEqual("pending_evidence", entry["output"]["generation_mode"])
                    self.assertEqual([], entry["output"]["paragraphs"])
                    self.assertIn("Evidence pending", entry["output"]["draft_md"])
                self.assertEqual(0, PIPELINE.main())
                model.assert_not_called()

    def test_failed_authoring_does_not_publish_source_quotes_as_fallback(self):
        source = deepcopy(self.evidence[0])
        source["content"] = "The optimized experiment gave 91% yield."
        attach_fact_to_evidence(source, {"fact_id": "F1", "field_id": "quantitative_results", "value": source["content"],
            "support_level": "direct", "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        task = {"section_id": "S02", "heading": "Results", "allowed_papers": ["P001", "P002"],
                "primary_papers": ["P001", "P002"]}
        package = {"retrieval_mode": "lexical", "hits": [source]}
        result = PIPELINE.recover_evidence_section(task, package, [source], {"P001": 1}, "P002 unavailable")
        self.assertEqual("pending_evidence", result["output"]["generation_mode"])
        self.assertNotIn("91%", result["output"]["draft_md"])
        self.assertEqual([], result["output"]["paragraphs"])

    def test_incomplete_source_check_fails_once_and_next_chapter_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "review-projects" / "test"
            stage = project / "02_section_drafting"
            matrix = project / "01_matrix_outline"
            stage.mkdir(parents=True)
            matrix.mkdir()
            tasks = [
                {"section_id": sid, "heading": sid, "section_role": "body",
                 "allowed_papers": ["P001"], "primary_papers": ["P001"],
                 "supporting_papers": []}
                for sid in ("S01", "S02")
            ]
            source = {"evidence_key": "sha256:source", "paper_id": "P001",
                      "chunk_id": "C001", "content": "A directly reported result.",
                      "claim_eligible": True}
            packages = [
                {"section_id": "S01", "retrieval_mode": "lexical",
                 "source_lookup_complete": True, "hits": [source]},
                {"section_id": "S02", "retrieval_mode": "insufficient_evidence",
                 "source_lookup_complete": True, "hits": []},
            ]
            for path, value in {
                stage / "section_tasks.json": tasks,
                stage / "section_evidence.json": {"sections": packages},
                matrix / "literature_matrix.json": {"rows": [{"paper_id": "P001", "title": "A paper"}]},
                matrix / "section_blueprint.json": {"review_topic": "Topic", "sections": tasks},
            }.items():
                path.write_text(json.dumps(value), encoding="utf-8")

            calls = []
            def incomplete_write(**kwargs):
                calls.append(kwargs["section_id"])
                kwargs["save_state"]({"proposed": {"paragraphs": [{"text": "Draft"}]}})
                return (
                    {"paragraphs": [{"paragraph_id": "S01-p1"}], "claims": []},
                    {"paragraphs": [{"paragraph_id": "S01-p1", "claim_realizations": []}]},
                    {"unresolved": [{"claim_id": "S01-p1-C01", "reason": "source_check_incomplete"}],
                     "omitted": [], "narrowed": []},
                )

            with patch.object(PIPELINE.sys, "argv", [str(SCRIPT), "--review-root", str(root),
                    "--project-id", "test", "--api-key", "test", "--model", "test",
                    "--section-concurrency", "1"]), \
                 patch.object(PIPELINE, "load_dotenv", return_value={}), \
                 patch.object(PIPELINE, "load_blueprint_rule_pack", return_value="Use evidence"), \
                 patch.object(PIPELINE, "load_cross_study_synthesis_skill", return_value="Use evidence"), \
                 patch.object(PIPELINE, "write_from_sources", side_effect=incomplete_write):
                with self.assertRaisesRegex(SystemExit, "1 section\\(s\\) failed: S01"):
                    PIPELINE.main()
                checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
                self.assertEqual({"S02"}, set(checkpoint["entries"]))
                self.assertEqual("S01", checkpoint["failed_sections"][0]["section_id"])
                self.assertIn("S01", checkpoint["authoring_states"])
                with self.assertRaisesRegex(SystemExit, "1 section\\(s\\) failed: S01"):
                    PIPELINE.main()
                checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
                self.assertEqual({"S02"}, set(checkpoint["entries"]))
                self.assertEqual(["S01"], calls)


    def test_fact_ceiling_agrees_in_planning_realization_and_publication(self):
        from review_writer_api.domain_services.sections import SectionsService
        from review_writer_api.errors import WorkflowValidationError

        source = self.evidence[0]
        source.update({"content": "The authors proposed a selective pathway.",
                       "assertion_ceiling": "direct_source_report"})
        attach_fact_to_evidence(source, {
            "fact_id": "F1", "value": source["content"], "support_level": "direct",
            "assertion_ceiling": "attributed_author_interpretation",
            "support_excerpt": source["content"],
            "evidence_refs": [{"evidence_key": source["evidence_key"]}],
        })
        self.proposed["paragraphs"][0]["claims"][0].update({"claim": source["content"], "fact_ids": ["F1"]})
        synthesis, writing = self.historical_writing_artifact()
        self.assertEqual("attributed_author_interpretation", writing["claims"][0]["assertion_ceiling"])
        generated = {"overview": "A source-attributed pathway.", "paragraphs": [{
            "paragraph_id": "S02-p1", "claim_realizations": [{"claim_id": "S02-p1-C01", "text": source["content"]}]}]}

        def realize():
            return PIPELINE.validate_and_realize_section(section_id="S02", generated=generated,
                writing_section=writing, evidence=[source], citation_map={"P001": 1})

        def publish():
            SectionsService._validate_academic_bundle(
                {"tasks": [{"section_id": "S02", "section_role": "body"}]},
                {"sections": [{"section_id": "S02", **generated}]},
                {"sections": [synthesis]}, {"planning_mode": "evidence_first", "sections": [writing]},
                {"evidence_registry": [source], "sections": [{"section_id": "S02", "hits": [source], "retrieval_mode": "lexical"}]},
            )

        realize()
        publish()
        # The fix must not silently promote an interpretation to a direct fact.
        writing["claims"][0]["assertion_ceiling"] = "direct_source_report"
        with self.assertRaisesRegex(RuntimeError, "assertion ceiling"):
            realize()
        with self.assertRaises(WorkflowValidationError) as caught:
            publish()
        self.assertEqual("S02", caught.exception.details["section_id"])
        self.assertEqual("S02-p1", caught.exception.details["paragraph_id"])
        self.assertEqual("attributed_author_interpretation", caught.exception.details["expected_assertion_ceiling"])


    def test_realization_cannot_switch_selected_experiment(self):
        source = self.evidence[0]
        source["content"] = "The optimized experiment gave 91% yield; the control gave 42% yield."
        for fid, value in (("F-opt", "91% yield"), ("F-control", "42% yield")):
            attach_fact_to_evidence(source, {"fact_id": fid, "value": value, "support_level": "direct",
                                          "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        claim = self.proposed["paragraphs"][0]["claims"][0]
        claim.update({"claim": "The optimized experiment gave 91% yield.", "fact_ids": ["F-opt"]})
        _, writing = self.historical_writing_artifact()
        generated = {"overview": "The measured outcomes depend on the experiment.", "paragraphs": [{
            "paragraph_id": "S02-p1", "claim_realizations": [{"claim_id": "S02-p1-C01", "text": "The optimized experiment gave 42% yield."}]}]}
        with self.assertRaisesRegex(RuntimeError, "selected facts"):
            PIPELINE.validate_and_realize_section(section_id="S02", generated=generated, writing_section=writing,
                                                  evidence=[source], citation_map={"P001": 1})


    def setUp(self) -> None:
        self.evidence = [
            {
                "evidence_id": "EV-A",
                "evidence_key": "sha256:a",
                "paper_id": "P001",
                "chunk_id": "C001",
                "content": "P001 reports the measured outcome.",
            },
            {
                "evidence_id": "EV-B",
                "evidence_key": "sha256:b",
                "paper_id": "P002",
                "chunk_id": "C002",
                "content": "P002 reports a contrasting measured outcome.",
            },
        ]
        self.proposed = {
            "overview_intent": "Introduce the comparison axis.",
            "synthesis_summary": "The studies support a bounded comparison.",
            "components": [
                {
                    "component_type": "comparison",
                    "purpose": "Compare outcomes.",
                    "summary": "The outcomes differ under the reported scope.",
                    "evidence_keys": ["sha256:a", "sha256:b"],
                }
            ],
            "paragraphs": [
                {
                    "theme": "Reported outcome boundary",
                    "argument_role": "comparison",
                    "objective": "Compare the two reported outcomes.",
                    "reader_takeaway": "The available studies support a bounded difference.",
                    "positive_synthesis": "A comparison is possible within the tested systems.",
                    "paper_ids": ["P001", "P002"],
                    "claims": [
                        {
                            "claim": "The reported outcomes differ within the tested systems.",
                            "claim_kind": "cross_study_comparison",
                            "synthesis_subtype": "trend",
                            "epistemic_status": "cross_source_inference",
                            "support_status": "supported",
                            "citation_group": ["P001", "P002"],
                            "evidence_keys": ["sha256:a", "sha256:b"],
                            "evidence_ceiling": "Do not generalize beyond the tested systems.",
                        }
                    ],
                }
            ],
        }

    def test_writing_scope_is_stable_and_binds_both_model_stages(self) -> None:
        scope = {
            "schema_version": 1,
            "topic": "Selective synthesis",
            "target_question": "Which strategies remain transferable?",
            "review_objective": "Build a bounded evidence map.",
            "target_readers": ["Researchers", "Graduate readers"],
            "required_reader_outcomes": ["Compare the supported strategies"],
            "time_span": {"from": 2015, "to": 2026, "basis": "user_topic"},
            "core_window": {"from": 2018, "to": 2026, "basis": "confirmed"},
            "coverage_mode": "local_bounded",
            "coverage_basis": {
                "kind": "selected_matrix",
                "selected_paper_count": 12,
                "global_literature_coverage_claimed": False,
            },
            "inclusion_criteria": ["Directly addresses the review question"],
            "exclusion_criteria": ["Outside the confirmed topic"],
            "evidence_availability_policy": "Do not exceed the available source.",
            "primary_navigation_axis": "reaction_strategy",
            "secondary_axes": ["catalyst_or_method"],
        }
        contract = derive_writing_scope_contract(scope)
        reordered = derive_writing_scope_contract(dict(reversed(list(scope.items()))))

        self.assertEqual(contract["fingerprint"], reordered["fingerprint"])
        self.assertEqual("active", contract["status"])
        planning = PIPELINE.writing_scope_prompt_block(contract, stage="planning")
        drafting = PIPELINE.writing_scope_prompt_block(contract, stage="drafting")
        for prompt in (planning, drafting):
            self.assertIn("Which strategies remain transferable?", prompt)
            self.assertIn("reaction_strategy", prompt)
            self.assertIn("local_bounded", prompt)
            self.assertIn("Directly addresses the review question", prompt)
        self.assertIn("distinct responsibility", planning)
        self.assertIn("Do not broaden the time window", drafting)

    def test_plan_is_bound_to_evidence_and_realized_exactly(self) -> None:
        _, writing = self.historical_writing_artifact()
        claim_id = writing["claims"][0]["claim_id"]
        overview, paragraphs, validations, reviews = PIPELINE.validate_and_realize_section(
            section_id="S02",
            generated={
                "overview": "This section compares the bounded source evidence.",
                "paragraphs": [
                    {
                        "paragraph_id": "S02-p1",
                        "claim_realizations": [
                            {"claim_id": claim_id, "text": "The reported outcomes differ within the tested systems."}
                        ],
                    }
                ],
            },
            writing_section=writing,
            evidence=self.evidence,
            citation_map={"P001": 1, "P002": 2},
        )
        self.assertEqual({"sha256:a", "sha256:b"}, {
            item["evidence_key"] for item in writing["claims"][0]["evidence_refs"]
        })
        self.assertIn("[1, 2]", paragraphs[0]["text"])
        self.assertEqual("pass", validations[0]["status"])
        self.assertEqual("PASS", reviews[0]["decision"])
        self.assertTrue(overview)


    def test_matrix_comparison_table_is_always_an_object(self) -> None:
        rows = {
            "P001": {
                "scientific_facts": [
                    {
                        "field_id": "yield",
                        "value": "81%",
                        "evidence_refs": [{"evidence_key": "sha256:a"}],
                    }
                ]
            },
            "P002": {"scientific_facts": None},
        }

        table = build_fact_comparison(
            "S02", ["P001", "P002"], rows
        )

        self.assertIsInstance(table, dict)
        self.assertEqual("S02", table["section_id"])
        self.assertEqual(["yield"], table["single_source_fields"])
        self.assertEqual(
            [{"paper_id": "P002", "field_id": "yield", "status": "unresolved"}],
            table["missing_cells"],
        )

    def test_matrix_comparison_table_marks_shared_fields_comparable(self) -> None:
        rows = {
            paper_id: {
                "scientific_facts": [
                    {
                        "field_id": "yield",
                        "value": value,
                        "evidence_refs": [{"evidence_key": evidence_key}],
                    }
                ]
            }
            for paper_id, value, evidence_key in (
                ("P001", "81%", "sha256:a"),
                ("P002", "76%", "sha256:b"),
            )
        }

        table = build_fact_comparison(
            "S02", ["P001", "P002"], rows
        )

        self.assertEqual(["yield"], table["comparable_fields"])
        self.assertEqual([], table["missing_cells"])
        self.assertEqual(2, len(table["cells"]))


    def test_realized_scientific_anchors_must_exist_in_the_claim_chunks(self) -> None:
        evidence = [
            {
                "evidence_id": "EV-A",
                "evidence_key": "sha256:a",
                "paper_id": "P001",
                "chunk_id": "C001",
                "content": "CuBr2 afforded the product in 81% yield.",
            }
        ]
        proposed = {
            "overview_intent": "Summarize the source.",
            "synthesis_summary": "The source reports a bounded result.",
            "components": [],
            "paragraphs": [
                {
                    "theme": "Reported outcome",
                    "argument_role": "example",
                    "objective": "Report the outcome.",
                    "reader_takeaway": "A result was reported.",
                    "positive_synthesis": "The source supports a bounded result.",
                    "paper_ids": ["P001"],
                    "claims": [
                        {
                            "claim": "CuBr2 afforded the product in 81% yield.",
                            "claim_kind": "reported_finding",
                            "epistemic_status": "direct_source_report",
                            "support_status": "supported",
                            "citation_group": ["P001"],
                            "evidence_keys": ["sha256:a"],
                            "evidence_ceiling": "Do not change the reported value.",
                        }
                    ],
                }
            ],
        }
        _, writing = self.historical_writing_artifact(proposed, evidence)
        claim_id = writing["claims"][0]["claim_id"]

        with self.assertRaisesRegex(
            RuntimeError,
            "unsupported evidence anchors.*97%",
        ):
            PIPELINE.validate_and_realize_section(
                section_id="S02",
                generated={
                    "overview": "The source reports a bounded result.",
                    "paragraphs": [
                        {
                            "paragraph_id": "S02-p1",
                            "claim_realizations": [
                                {
                                    "claim_id": claim_id,
                                    "text": "CuBr2 afforded the product in 97% yield.",
                                }
                            ],
                        }
                    ],
                },
                writing_section=writing,
                evidence=evidence,
                citation_map={"P001": 1},
            )


    def test_legacy_prefix_fallback_requires_explicit_authorization(self) -> None:
        self.assertEqual(
            "insufficient_evidence",
            PIPELINE.effective_retrieval_mode(
                {"retrieval_mode": "fixed_prefix_fallback"}
            ),
        )
        self.assertEqual(
            "fixed_prefix_fallback",
            PIPELINE.effective_retrieval_mode(
                {
                    "retrieval_mode": "fixed_prefix_fallback",
                    "legacy_fallback_authorized": True,
                }
            ),
        )

    def test_planning_evidence_is_compacted_under_a_request_budget(self) -> None:
        evidence = [
            {
                "evidence_key": f"sha256:{index}",
                "paper_id": f"P{index:03d}",
                "chunk_id": f"C{index:03d}",
                "content": "source content " * 2_000,
                "claim_eligible": True,
            }
            for index in range(50)
        ]

        compact, report = PIPELINE.bounded_evidence_payload(
            evidence,
            char_budget=20_000,
        )

        self.assertLessEqual(report["content_characters"], 20_000)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 20_000)
        self.assertEqual(len(json.dumps(compact, ensure_ascii=False)), report["serialized_characters"])
        self.assertEqual(50, len(compact))
        self.assertTrue(all(row["evidence_key"] for row in compact))

    def test_direct_provider_json_parser_accepts_extra_top_level_data(self) -> None:
        parsed = PIPELINE.parse_json_object(
            '{"paragraphs": [], "overview": "complete"}\n'
            '{"relay_diagnostic": "ignored"}',
            required_list="paragraphs",
        )

        self.assertEqual("complete", parsed["overview"])
        self.assertNotIn("relay_diagnostic", parsed)

    def test_primary_results_survive_background_and_large_fact_audits(self) -> None:
        from copy import deepcopy
        evidence = [{"paper_id": f"B{i}", "evidence_key": f"b{i}", "chunk_id": str(i),
                     "content": "background " * 700, "claim_eligible": True} for i in range(20)]
        for i in range(4):
            evidence.append({"paper_id": "PRIMARY", "evidence_key": f"p{i}", "chunk_id": f"p{i}",
                "content": "primary passage " * 80 + f"RESULT_TAIL_{i}", "claim_eligible": True,
                "fact_bindings": [{"fact_id": f"F{i}", "usage": "direct", "value": "bounded result",
                    "support_level": "direct", "support_excerpt": f"RESULT_TAIL_{i}",
                    "evidence_refs": [{"evidence_key": f"p{i}", "support_excerpt": f"RESULT_TAIL_{i}"}],
                    "verification": {"status": "supported", "reason": "source audit " * 5000}}]})
        original = deepcopy(evidence)
        projected, report = PIPELINE.bounded_evidence_payload(evidence, char_budget=14000, primary_papers=["PRIMARY"])
        primary = [row for row in projected if row["paper_id"] == "PRIMARY"]
        self.assertEqual(4, len(primary))
        self.assertTrue(all("RESULT_TAIL_" in row["content"] for row in primary))
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 14000)
        self.assertTrue(all("evidence_refs" not in row["fact_bindings"][0] for row in primary))
        self.assertEqual(original, evidence)

    def test_recovered_primary_contribution_is_not_crowded_out_by_prior_work(self) -> None:
        evidence = [{"paper_id": "P1", "evidence_key": f"prior-{i}",
                     "content": "Earlier studies supplied context. " * 25}
                    for i in range(5)]
        evidence.append({"paper_id": "P1", "evidence_key": "own-body",
                         "content": "The present study reports its own result. " * 20,
                         "retrieval_passes": ["primary_contribution_recovery"]})
        projected, report = PIPELINE.bounded_evidence_payload(
            evidence, char_budget=2200, primary_papers=["P1"])
        self.assertTrue(report["compacted"])
        self.assertIn("own-body", {row["evidence_key"] for row in projected})

    def test_evidence_that_fits_keeps_long_tail_and_input_unchanged(self) -> None:
        from copy import deepcopy
        evidence = [{"evidence_key": str(i), "paper_id": f"paper-{i % 3}",
                     "chunk_id": str(i), "content": "source " * (100 if i < 38 else 21) + "TAIL_SUPPORT"}
                    for i in range(89)]
        original = deepcopy(evidence)
        projected, report = PIPELINE.bounded_evidence_payload(evidence)
        self.assertEqual(89, len(projected))
        self.assertTrue(all(row["content"].endswith("TAIL_SUPPORT") for row in projected))
        self.assertFalse(report["compacted"])
        self.assertEqual(0, report["truncated_content_hit_count"])
        self.assertEqual(len(json.dumps(projected, ensure_ascii=False)), report["serialized_characters"])
        self.assertEqual(original, evidence)
        projected, report = PIPELINE.bounded_evidence_payload([
            {"evidence_key": "long", "paper_id": "p", "content": "source " * 800 + "TAIL_SUPPORT"}])
        self.assertTrue(projected[0]["content"].endswith("TAIL_SUPPORT"))
        self.assertFalse(report["compacted"])

    def test_known_repair_mode_reads_full_text_but_does_not_authorize_fallback(self) -> None:
        self.assertEqual("lexical", PIPELINE.effective_retrieval_mode({
            "retrieval_mode": "lexical+draft_targeted_source_recheck"}))
        self.assertEqual("unsupported_retrieval_mode", PIPELINE.effective_retrieval_mode({
            "retrieval_mode": "lexical+unknown"}))
        self.assertEqual("insufficient_evidence", PIPELINE.effective_retrieval_mode({
            "retrieval_mode": "fixed_prefix_fallback"}))


if __name__ == "__main__":
    unittest.main()
