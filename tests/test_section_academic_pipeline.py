from __future__ import annotations

import importlib.util
import json
import unittest
import tempfile
from copy import deepcopy
from unittest.mock import patch
from pathlib import Path

from review_writer_core.writing_contracts import derive_writing_scope_contract
from review_writer_core.scientific_facts import attach_fact_to_evidence
from review_writer_core.stages.sections.plan_repair import complete_primary_claim_coverage


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




    def test_normalization_never_silently_truncates_paragraphs_or_claims(self):
        proposal = deepcopy(self.proposed)
        paragraph = proposal["paragraphs"][0]
        paragraph["claims"] = [deepcopy(paragraph["claims"][0]) for _ in range(10)]
        proposal["paragraphs"] = [deepcopy(paragraph) for _ in range(10)]
        _, writing = self.normalize(proposed=proposal)
        self.assertEqual(10, len(writing["paragraphs"]))
        self.assertEqual(100, len(writing["claims"]))
        self.assertEqual("S02-p10-C10", writing["claims"][-1]["claim_id"])







    def normalize(self, proposed=None, evidence=None, strict=True):
        return PIPELINE.normalize_section_plan(section_id="S02", role="body", primary=["P001"], supporting=[],
            allowed=["P001"], evidence=evidence or [self.evidence[0]], retrieval_mode="lexical",
            generated=proposed or self.proposed, synthesis_requirements=[], strict=strict)

    def test_fact_ids_resolve_canonical_refs_without_model_copying(self):
        source = self.evidence[0]
        source["content"] = "Product 1m gave 69% yield and 1:1 dr."
        attach_fact_to_evidence(source, {"fact_id": "F1", "value": "69% yield and 1:1 dr", "support_level": "direct",
            "support_excerpt": source["content"], "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        claim = self.proposed["paragraphs"][0]["claims"][0]
        claim.update({"claim": source["content"], "fact_ids": ["F1"], "evidence_keys": ["mistyped"], "citation_group": ["wrong"]})
        _, writing = self.normalize()
        self.assertEqual("sha256:a", writing["claims"][0]["evidence_refs"][0]["evidence_key"])
        self.assertEqual(["P001"], writing["claims"][0]["citation_group"])
        claim["fact_ids"] = []
        claim["evidence_keys"] = ["sha256:a"]
        synthesis, writing = self.normalize(strict=False)
        self.assertFalse(writing["claims"])
        self.assertEqual(["registered_fact_selection_required"], synthesis["normalization_diagnostics"]["rejected_claims"][0]["reasons"])

    def test_fact_binding_must_be_attached_to_its_exact_selected_evidence_row(self):
        binding_row = {
            "evidence_id": "EV-A",
            "evidence_key": "sha256:a",
            "paper_id": "P001",
            "chunk_id": "C001",
            "content": "Adjacent context.",
            "claim_eligible": True,
        }
        referenced_row = {
            "evidence_id": "EV-B",
            "evidence_key": "sha256:b",
            "paper_id": "P001",
            "chunk_id": "C002",
            "content": "The selected experiment gave 81% yield.",
            "claim_eligible": True,
        }
        attach_fact_to_evidence(
            binding_row,
            {
                "fact_id": "F1",
                "value": referenced_row["content"],
                "support_level": "direct",
                "evidence_refs": [{"evidence_key": "sha256:b"}],
            },
        )
        proposed = deepcopy(self.proposed)
        proposed["paragraphs"][0]["claims"][0].update(
            {"claim": referenced_row["content"], "fact_ids": ["F1"]}
        )

        synthesis, writing = self.normalize(
            proposed=proposed,
            evidence=[binding_row, referenced_row],
            strict=False,
        )

        self.assertFalse(writing["claims"])
        self.assertIn(
            "fact_identity_outside_selected_evidence:F1",
            synthesis["normalization_diagnostics"]["rejected_claims"][0]["reasons"],
        )

    def test_bound_abstract_context_is_visible_but_cannot_supply_detail(self):
        source = self.evidence[0]
        source.update({"claim_eligible": False, "content": "The authors report a selective method with 91% yield.",
                       "assertion_ceiling": "abstract_report_only"})
        attach_fact_to_evidence(source, {"fact_id": "A1", "value": source["content"], "support_level": "abstract_limited",
            "assertion_ceiling": "abstract_report_only", "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        claim = self.proposed["paragraphs"][0]["claims"][0]
        claim.update({"claim": "The authors report a selective method.", "fact_ids": ["A1"], "claim_kind": "historical_transition"})
        synthesis, writing = self.normalize(strict=False)
        self.assertEqual(["P001"], synthesis["normalization_diagnostics"]["missing_primary_papers"])
        self.assertEqual("abstract_report_only", writing["claims"][0]["assertion_ceiling"])
        generated = {"overview": "The study introduced a selective method.", "paragraphs": [{
            "paragraph_id": "S02-p1", "claim_realizations": [{"claim_id": "S02-p1-C01", "text": claim["claim"]}]}]}
        PIPELINE.validate_and_realize_section(section_id="S02", generated=generated, writing_section=writing,
                                              evidence=[source], citation_map={"P001": 1})
        claim["claim"] = "The method gave 91% yield."
        synthesis, _ = self.normalize(strict=False)
        self.assertIn("abstract_detail_requires_full_text", synthesis["normalization_diagnostics"]["rejected_claims"][0]["reasons"])

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
        synthesis, writing = self.normalize()
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

    def test_surviving_experiment_role_is_not_relabelled_to_hide_missing_frame(self):
        source = self.evidence[0]
        source["content"] = "The experiment gave 91% yield."
        attach_fact_to_evidence(source, {"fact_id": "F1", "value": source["content"], "support_level": "direct",
            "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        paragraph = self.proposed["paragraphs"][0]
        paragraph["argument_role"] = "anchor_case"
        paragraph["claims"][0].update({"claim": source["content"], "fact_ids": ["F1"]})
        _, writing = self.normalize(strict=False)
        self.assertEqual("anchor_case", writing["paragraphs"][0]["argument_role"])

    def test_appended_frame_and_exit_are_ordered_without_changing_claim_identity(self):
        original = deepcopy(self.proposed["paragraphs"][0])
        original["claims"][0]["claim_kind"] = "reported_finding"
        self.proposed["paragraphs"] = []
        for role in ("anchor_case", "section_synthesis_exit", "section_frame"):
            paragraph = deepcopy(original)
            paragraph["argument_role"] = role
            self.proposed["paragraphs"].append(paragraph)
        _, writing = self.normalize(strict=False)
        self.assertEqual(["S02-p3", "S02-p1", "S02-p2"], [p["paragraph_id"] for p in writing["paragraphs"]])
        self.assertEqual(["S02-p1-C01", "S02-p2-C01", "S02-p3-C01"], [c["claim_id"] for c in writing["claims"]])
        self.assertTrue(all(c["claim"] == original["claims"][0]["claim"] for c in writing["claims"]))

    def test_unverified_source_absence_is_reported_as_a_repairable_rejection(self):
        claim = self.proposed["paragraphs"][0]["claims"][0]
        claim["claim"] = "The original paper did not report the experimental temperature."
        synthesis, _ = self.normalize(strict=False)
        self.assertEqual(["source_absence_not_verified"], synthesis["normalization_diagnostics"]["rejected_claims"][0]["reasons"])

    def test_targeted_repair_preserves_safe_claim_and_repairs_only_failed_slot(self):
        source = self.evidence[0]
        source["content"] = "The experiment gave 91% yield."
        attach_fact_to_evidence(source, {"fact_id": "F1", "value": source["content"], "support_level": "direct",
            "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        valid = {**self.proposed["paragraphs"][0]["claims"][0], "claim": "The experiment gave 91% yield.", "fact_ids": ["F1"]}
        invalid = {**valid, "claim": "The experiment gave 99% yield."}
        self.proposed["paragraphs"][0]["claims"] = [valid, invalid]
        original = deepcopy(self.proposed)
        synthesis, writing = self.normalize(strict=False)
        self.assertEqual(1, len(writing["claims"]))
        patch = {"claim_repairs": [
            {"claim_id": "S02-p1-C01", "replacement": {**valid, "claim": "Overwrite safe content"}},
            {"claim_id": "S02-p1-C02", "replacement": valid},
            {"claim_id": "S99-p1-C01", "replacement": invalid},
        ], "additional_paragraphs": []}
        merged = PIPELINE.merge_plan_repair("S02", self.proposed, synthesis["normalization_diagnostics"], patch)
        self.assertEqual(original, self.proposed)
        self.assertEqual(valid, merged["paragraphs"][0]["claims"][0])
        synthesis, writing = self.normalize(merged)
        self.assertEqual(2, len(writing["claims"]))
        self.assertFalse(synthesis["normalization_diagnostics"]["rejected_claims"])

        diagnostics = {"rejected_claims": [], "unsupported_components": ["mechanism"]}
        patched = PIPELINE.merge_plan_repair("S02", original, diagnostics, {
            "claim_repairs": [], "additional_paragraphs": [], "component_repairs": [
                {"component_type": "comparison", "summary": "Overwrite supported component"},
                {"component_type": "mechanism", "summary": "Source-attributed proposal", "evidence_keys": ["sha256:a"]},
            ]})
        self.assertEqual(original["components"][0], patched["components"][0])
        self.assertEqual("mechanism", patched["components"][1]["component_type"])

    def test_realization_cannot_switch_selected_experiment(self):
        source = self.evidence[0]
        source["content"] = "The optimized experiment gave 91% yield; the control gave 42% yield."
        for fid, value in (("F-opt", "91% yield"), ("F-control", "42% yield")):
            attach_fact_to_evidence(source, {"fact_id": fid, "value": value, "support_level": "direct",
                                          "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        claim = self.proposed["paragraphs"][0]["claims"][0]
        claim.update({"claim": "The optimized experiment gave 91% yield.", "fact_ids": ["F-opt"]})
        _, writing = self.normalize()
        generated = {"overview": "The measured outcomes depend on the experiment.", "paragraphs": [{
            "paragraph_id": "S02-p1", "claim_realizations": [{"claim_id": "S02-p1-C01", "text": "The optimized experiment gave 42% yield."}]}]}
        with self.assertRaisesRegex(RuntimeError, "selected facts"):
            PIPELINE.validate_and_realize_section(section_id="S02", generated=generated, writing_section=writing,
                                                  evidence=[source], citation_map={"P001": 1})

    def test_explicit_claim_fact_selection_does_not_inherit_another_experiment(self):
        source = self.evidence[0]
        source["content"] = "The optimized experiment gave 91% yield and the control gave 42% yield."
        for fid, value in (("F-opt", "91% yield"), ("F-control", "42% yield")):
            attach_fact_to_evidence(source, {"fact_id": fid, "value": value, "support_level": "direct",
                "evidence_refs": [{"evidence_key": source["evidence_key"]}]})
        claim = self.proposed["paragraphs"][0]["claims"][0]
        claim.update({"claim": "The control gave 42% yield.", "fact_ids": ["F-control"],
                      "evidence_keys": [source["evidence_key"]], "citation_group": ["P001"]})
        _, writing = PIPELINE.normalize_section_plan(section_id="S02", role="body", primary=["P001"], supporting=[],
            allowed=["P001"], evidence=[source], retrieval_mode="lexical", generated=self.proposed, synthesis_requirements=[])
        self.assertEqual(["F-control"], writing["claims"][0]["fact_ids"])
        self.assertEqual("42% yield", writing["claims"][0]["allowed_assertion"])
        self.assertEqual("explicit_fact_selection", writing["claims"][0]["fact_binding_status"])
        claim["fact_ids"] = ["F-opt"]
        with self.assertRaisesRegex(RuntimeError, "no supported paragraph"):
            PIPELINE.normalize_section_plan(section_id="S02", role="body", primary=["P001"], supporting=[],
                allowed=["P001"], evidence=[source], retrieval_mode="lexical", generated=self.proposed, synthesis_requirements=[])

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
        synthesis, writing = PIPELINE.normalize_section_plan(
            section_id="S02",
            role="body",
            primary=["P001", "P002"],
            supporting=[],
            allowed=["P001", "P002"],
            evidence=self.evidence,
            retrieval_mode="lexical",
            generated=self.proposed,
            synthesis_requirements=[
                {"component": "comparison", "necessity": "required", "reason": "compare"}
            ],
        )
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
        self.assertEqual("supported", synthesis["components"][0]["status"])
        self.assertEqual({"sha256:a", "sha256:b"}, {
            item["evidence_key"] for item in writing["claims"][0]["evidence_refs"]
        })
        self.assertIn("[1, 2]", paragraphs[0]["text"])
        self.assertEqual("pass", validations[0]["status"])
        self.assertEqual("PASS", reviews[0]["decision"])
        self.assertTrue(overview)

    def test_current_plan_preserves_only_registered_blueprint_claim_ids(self) -> None:
        source = self.evidence[0]
        source["assertion_ceiling"] = "direct_source_report"
        fact = {
            "fact_id": "F-P001-RESULT",
            "field_id": "quantitative_results",
            "value": source["content"],
            "support_level": "direct",
            "assertion_ceiling": "direct_source_report",
            "evidence_refs": [{"evidence_key": source["evidence_key"]}],
        }
        attach_fact_to_evidence(source, fact)
        declared = {
            "claim_id": "S02-SC001",
            "proposition": source["content"],
            "claim_type": "reported_result",
            "primary_papers": ["P001"],
            "fact_ids": [fact["fact_id"]],
            "evidence_refs": [{"evidence_key": source["evidence_key"]}],
            "support_status": "supported",
            "allowed_assertion": source["content"],
            "assertion_ceiling": "direct_source_report",
            "source": "blueprint_fact_card",
            "coverage": {
                "subject": True,
                "predicate": True,
                "value": True,
                "qualifiers": True,
                "paper_identity": True,
            },
        }
        proposed = deepcopy(self.proposed)
        proposed_claim = proposed["paragraphs"][0]["claims"][0]
        from review_writer_core.claim_contracts import verify_argument, argument_projection
        declared.update(claim_revision=2, epistemic_status="direct_source_report", argument_basis={"mode": "reported", "comparison_basis": "Reported outcome",
                        "reasoning_summary": "The conclusion follows from this measured result."})
        verify_argument(declared, supported=True, reason="The premises support the stated conclusion.")
        proposed_claim.update(
            {
                "claim_id": "S02-SC001",
                "claim": "A broader model-authored statement that must be ignored.",
            }
        )
        _synthesis, writing = PIPELINE.normalize_section_plan(
            section_id="S02",
            role="body",
            primary=["P001"],
            supporting=[],
            allowed=["P001"],
            evidence=[source],
            retrieval_mode="lexical",
            generated=proposed,
            synthesis_requirements=[],
            declared_claims=[declared],
        )
        self.assertEqual(["S02-SC001"], writing["paragraphs"][0]["claim_ids"])
        self.assertEqual("S02-SC001", writing["claims"][0]["claim_id"])
        self.assertEqual(argument_projection(declared), argument_projection(writing["claims"][0]))
        self.assertEqual(source["content"], writing["claims"][0]["claim"])

        proposed_claim["claim_id"] = "S02-NEW-CLAIM"
        with self.assertRaisesRegex(RuntimeError, "no supported paragraph"):
            PIPELINE.normalize_section_plan(
                section_id="S02",
                role="body",
                primary=["P001"],
                supporting=[],
                allowed=["P001"],
                evidence=[source],
                retrieval_mode="lexical",
                generated=proposed,
                synthesis_requirements=[],
                declared_claims=[declared],
            )

    def test_missing_primary_papers_are_routed_from_supported_blueprint_claims(self) -> None:
        evidence = []
        declared = []
        for index, paper_id in enumerate(("P001", "P002", "P003"), start=1):
            evidence_key = f"sha256:{index}"
            content = f"{paper_id} reports supported result {index}."
            source = {
                "evidence_id": f"EV-{index}",
                "evidence_key": evidence_key,
                "paper_id": paper_id,
                "chunk_id": f"C-{index}",
                "content": content,
                "claim_eligible": True,
            }
            fact = {
                "fact_id": f"F-{index}",
                "field_id": "quantitative_results",
                "value": content,
                "support_level": "direct",
                "assertion_ceiling": "direct_source_report",
                "evidence_refs": [{"evidence_key": evidence_key}],
            }
            attach_fact_to_evidence(source, fact)
            evidence.append(source)
            declared.append(
                {
                    "claim_id": f"S02-SC{index:03d}",
                    "proposition": content,
                    "claim_type": "reported_result",
                    "primary_papers": [paper_id],
                    "fact_ids": [fact["fact_id"]],
                    "evidence_refs": [{"evidence_key": evidence_key}],
                    "support_status": "supported",
                    "allowed_assertion": content,
                    "assertion_ceiling": "direct_source_report",
                    "source": "blueprint_fact_card",
                }
            )
        proposed = deepcopy(self.proposed)
        proposed["paragraphs"][0]["claims"] = [
            {"claim_id": declared[0]["claim_id"]}
        ]
        synthesis, _writing = PIPELINE.normalize_section_plan(
            section_id="S02",
            role="body",
            primary=["P001", "P002", "P003"],
            supporting=[],
            allowed=["P001", "P002", "P003"],
            evidence=evidence,
            retrieval_mode="lexical",
            generated=proposed,
            synthesis_requirements=[],
            declared_claims=declared,
            strict=False,
        )
        self.assertEqual(
            ["P002", "P003"],
            synthesis["normalization_diagnostics"]["missing_primary_papers"],
        )

        completed, repair = complete_primary_claim_coverage(
            "S02",
            proposed,
            synthesis["normalization_diagnostics"]["missing_primary_papers"],
            declared,
        )
        synthesis, writing = PIPELINE.normalize_section_plan(
            section_id="S02",
            role="body",
            primary=["P001", "P002", "P003"],
            supporting=[],
            allowed=["P001", "P002", "P003"],
            evidence=evidence,
            retrieval_mode="lexical",
            generated=completed,
            synthesis_requirements=[],
            declared_claims=declared,
            strict=False,
        )

        self.assertEqual([], synthesis["normalization_diagnostics"]["missing_primary_papers"])
        self.assertEqual(
            {"S02-SC001", "S02-SC002", "S02-SC003"},
            {claim["claim_id"] for claim in writing["claims"]},
        )
        self.assertEqual(["S02-SC002", "S02-SC003"], repair["added_claim_ids"])
        self.assertEqual([], repair["unresolved_primary_papers"])

    def test_missing_supported_claims_are_routed_even_when_paper_is_covered(self) -> None:
        declared = [
            {
                "claim_id": claim_id,
                "primary_papers": ["P001"],
                "fact_ids": [fact_id],
            }
            for claim_id, fact_id in (
                ("S02-SC001", "F1"),
                ("S02-SC002", "F2"),
                ("S02-SC003", "F3"),
            )
        ]
        proposed = {
            "paragraphs": [
                {
                    "paper_ids": ["P001"],
                    "claims": [{"claim_id": "S02-SC001"}],
                }
            ]
        }

        completed, repair = complete_primary_claim_coverage(
            "S02",
            proposed,
            [],
            declared,
            ["S02-SC002", "S02-SC003"],
        )

        routed = {
            claim["claim_id"]
            for paragraph in completed["paragraphs"]
            for claim in paragraph.get("claims") or []
        }
        self.assertEqual(
            {"S02-SC001", "S02-SC002", "S02-SC003"}, routed
        )
        self.assertEqual(
            ["S02-SC002", "S02-SC003"], repair["added_claim_ids"]
        )
        self.assertEqual([], repair["unresolved_claim_ids"])

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

        table = PIPELINE.build_matrix_comparison_table(
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

        table = PIPELINE.build_matrix_comparison_table(
            "S02", ["P001", "P002"], rows
        )

        self.assertEqual(["yield"], table["comparable_fields"])
        self.assertEqual([], table["missing_cells"])
        self.assertEqual(2, len(table["cells"]))


    def test_unknown_evidence_cannot_form_an_indexed_claim(self) -> None:
        proposed = dict(self.proposed)
        proposed["paragraphs"] = [dict(self.proposed["paragraphs"][0])]
        proposed["paragraphs"][0]["claims"] = [
            {**self.proposed["paragraphs"][0]["claims"][0], "evidence_keys": ["sha256:outside"]}
        ]
        with self.assertRaisesRegex(RuntimeError, "no supported paragraph"):
            PIPELINE.normalize_section_plan(
                section_id="S02",
                role="body",
                primary=["P001", "P002"],
                supporting=[],
                allowed=["P001", "P002"],
                evidence=self.evidence,
                retrieval_mode="lexical",
                generated=proposed,
                synthesis_requirements=[],
            )

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
        _synthesis, writing = PIPELINE.normalize_section_plan(
            section_id="S02",
            role="body",
            primary=["P001"],
            supporting=[],
            allowed=["P001"],
            evidence=evidence,
            retrieval_mode="lexical",
            generated=proposed,
            synthesis_requirements=[],
        )
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
