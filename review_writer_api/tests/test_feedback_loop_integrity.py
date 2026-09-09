from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "review-first-draft-feedback-loop"
    / "scripts"
    / "feedback_loop.py"
)
SPEC = importlib.util.spec_from_file_location("feedback_loop_integrity", SCRIPT_PATH)
assert SPEC and SPEC.loader
feedback_loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feedback_loop)


class FeedbackLoopIntegrityTests(unittest.TestCase):
    def test_attribution_repairs_preserve_supported_values_and_sources(self):
        cases = [
            (
                "The CuBr method failed with aromatic aldehydes despite 93–99% ee. [13]",
                "Earlier methods failed with aromatic aldehydes; the subsequent CuBr method reported 93–99% ee. [13]",
                "The failure belongs to earlier methods, not the CuBr method.",
                "Earlier methods failed with aromatic aldehydes. Here we report a CuBr method with 93–99% ee.",
            ),
            (
                "Together these reports describe CuBr routes. One CuBr study reported 99% ee. [13] An earlier route failed. [6]",
                "These reports describe distinct routes. The CuBr study reported 99% ee. [13] An earlier route failed. [6]",
                "The collective CuBr label incorrectly includes the earlier route.",
                "The earlier route used a different catalyst; the later study used CuBr and reported 99% ee.",
            ),
        ]
        for original, candidate, diagnosis, passage in cases:
            with self.subTest(diagnosis=diagnosis):
                finding = {"unsupported_claims": [diagnosis], "diagnosis": diagnosis,
                           "source_check_status": "partially_supported", "route": "section_rewrite"}
                evidence = {"paper_ids": ["A"], "evidence": [{"paper_id": "A",
                            "original_text_available": True,
                            "original_passages": [{"ref": "A:p1", "text": passage}]}]}
                mode = feedback_loop.automatic_rewrite_mode(finding, evidence, paragraph_goal=95)
                self.assertEqual(mode, "source_recheck_cleanup")
                for prompt in (
                    feedback_loop.rewrite_prompt({"paragraph_id": "p1", "text": original}, finding, evidence,
                                                 1, 200, rewrite_mode=mode),
                    feedback_loop.rewrite_repair_prompt(original, original, [], 1, 200,
                        word_range_applicable=False, allowed_unsupported_claims=[diagnosis],
                        evidence=evidence, score=finding, rewrite_mode=mode),
                ):
                    self.assertIn("FACTUAL REPAIR takes precedence", prompt)
                    self.assertIn(passage, prompt)
                    self.assertNotIn("must never be added, replaced, or reassigned", prompt)
                errors, _ = feedback_loop.validate_rewrite_report(original, candidate, 1, 200,
                                                                  allowed_unsupported_claims=[diagnosis])
                self.assertEqual(errors, [])
                errors, _ = feedback_loop.validate_rewrite_report(original, candidate.replace("[13]", "[14]"),
                                                                  1, 200, allowed_unsupported_claims=[diagnosis])
                self.assertIn("protected_callouts_changed", errors)

    def test_attribution_repair_does_not_relax_polish_or_missing_evidence(self):
        from review_writer_core.source_attribution import attribution_repair_instruction
        finding = {"unsupported_claims": ["Misattributed result"]}
        evidence = {"evidence": [{"original_passages": [{"text": "Local context"}]}]}
        for mode in ("human_review_style_only", "final_polish"):
            self.assertEqual(attribution_repair_instruction(finding, evidence, mode), "")
        self.assertEqual(attribution_repair_instruction(finding, {}, "section_rewrite"), "")
        self.assertEqual(attribution_repair_instruction({}, evidence, "section_rewrite"), "")

    def test_verified_presentation_advice_is_not_a_release_gate(self):
        for rule in ("P02", "P04"):
            with self.subTest(rule=rule):
                finding = {"paragraph_id": "p1", "failed_dimensions": [rule],
                           "source_check_status": "verified", "severity": "major"}
                self.assertFalse(feedback_loop.paragraph_finding_is_blocking(finding))
                self.assertTrue(feedback_loop.paragraph_finding_is_blocking({
                    **finding, "unsupported_claims": ["Unreported scope extension."],
                }))
        self.assertFalse(feedback_loop.paragraph_finding_is_blocking({
            "failed_dimensions": ["P04"], "source_check_status": "partially_supported", "severity": "major",
        }))

    def test_score_projection_does_not_double_count_issues(self):
        from review_writer_core.writing_contracts import substantive_quality_findings
        finding = {"paragraph_id": "p1", "rule": "C01", "severity": "major"}
        quality = {"issues": [finding], "paragraph_scores": [dict(finding)], "paragraph_findings": [dict(finding)]}
        self.assertEqual([finding], substantive_quality_findings(quality))
        second = {**finding, "rule": "C04"}
        quality["issues"].append(second)
        self.assertEqual([finding, second], substantive_quality_findings(quality))

    def test_required_argument_survives_evidence_projection_and_omission_is_not_style(self):
        from review_writer_core.claim_contracts import verify_argument, argument_projection
        claim = {"claim_id": "C1", "claim_revision": 2, "proposition": "The method is established for this input.",
                 "argument_basis": {"mode": "synthesis"}, "fact_ids": ["F1"], "citation_group": ["P1"],
                 "evidence_refs": [{"evidence_key": "source"}], "required_for_section": True}
        verify_argument(claim, supported=True, reason="The premises support this bounded synthesis.")
        evidence = feedback_loop.source_evidence(Path("."), Path("."), {"paragraph_id": "p1"}, {}, {},
            academic_contract={"paragraph_claim_ids": {"p1": ["C1"]}, "claims": {"C1": claim},
                "evidence_by_key": {"source": {"paper_id": "P1", "claim_eligible": True, "content": "The method was tested."}}})
        compact = feedback_loop.compact_evidence_for_prompt(evidence)
        self.assertEqual(argument_projection(claim), argument_projection(compact["argument_plan"][0]))
        for ids, blocked in [(["C1"], True), (["unregistered"], False)]:
            with self.subTest(ids=ids):
                result = feedback_loop.normalize_evaluation(
                    {"dimension_scores": [{"id": "P03", "level": 4}], "paragraph_scores": [{
                        "paragraph_id": "p1", "score": 96, "severity": "none", "route": "pass",
                        "source_check_status": "verified", "source_evidence_refs": ["source"],
                        "unsupported_claims": [], "failed_dimensions": [], "missing_core_claim_ids": ids,
                    }]}, {"dimensions": [{"id": "P03", "weight": 100}]},
                    [{"paragraph_id": "p1", "text": "The method was tested. [1]"}],
                    {"paragraph_findings": [], "hard_regressions": []}, 90, 85, evidence={"p1": evidence})
                score = result["paragraph_scores"][0]
                self.assertEqual(blocked, feedback_loop.paragraph_finding_is_blocking(score))
                self.assertEqual(blocked, score["route"] == "section_rewrite")

    def test_passed_review_frame_is_not_failed_for_a_partial_label_alone(self):
        paragraph_id = "frame"
        evidence = {paragraph_id: {"paper_ids": ["P001"], "original_source_ready": True, "evidence": [{
            "paper_id": "P001", "original_passages": [{"ref": "source", "text": "A supported result."}],
        }]}}
        for role, claims, dimensions, refs, should_pass in (
            ("section_frame", [], [], ["source"], True),
            ("section_synthesis_exit", [], [], ["source"], True),
            ("cross_study_comparison", [], [], ["source"], True),
            ("mechanism_boundary", [], [], ["source"], True),
            ("scope_limitation", [], [], ["source"], True),
            ("section_frame", ["The catalyst is universally superior."], [], ["source"], False),
            ("scope_limitation", ["The catalyst is universally superior."], [], ["source"], False),
            ("section_frame", [], ["G08"], ["source"], False),
            ("section_frame", [], [], [], True),
            ("anchor_case", [], [], ["source"], True),
        ):
            with self.subTest(role=role, claims=claims, dimensions=dimensions, refs=refs):
                result = feedback_loop.normalize_evaluation(
                    {"dimension_scores": [{"id": "readability", "level": 4}, {"id": "G08", "level": 4}], "paragraph_scores": [{
                        "paragraph_id": paragraph_id, "score": 91, "severity": "none", "route": "pass",
                        "source_check_status": "partially_supported", "source_evidence_refs": refs,
                        "unsupported_claims": claims, "failed_dimensions": dimensions,
                    }]},
                    {"dimensions": [{"id": "readability", "weight": 50}, {"id": "G08", "weight": 50}]},
                    [{"paragraph_id": paragraph_id, "text": "A supported result. We organize this review by method. [1]"}],
                    {"paragraph_checks": [{"paragraph_id": paragraph_id, "paragraph_role": role}],
                     "paragraph_findings": [], "hard_regressions": []},
                    90, 85, evidence=evidence,
                )
                self.assertEqual(should_pass, result["decision"] == "PASS")
                if should_pass and refs:
                    self.assertEqual(91, result["paragraph_scores"][0]["score"])
                    self.assertEqual("partially_supported", result["paragraph_scores"][0]["source_check_status"])

    def test_single_paragraph_update_does_not_resurrect_other_paragraph_quality_gates(self):
        from review_writer_api.domain_services.drafts import DraftsService
        service = object.__new__(DraftsService)
        score = {"paragraph_id": "S01-p1", "score": 93, "severity": "none", "route": "pass"}
        other = {**score, "paragraph_id": "S01-p2"}
        for rule in ("P01", "C01"):
            with self.subTest(rule=rule):
                quality = service._incremental_quality(
                    {"score": 93, "paragraph_scores": [score, other],
                     "hard_gate_failures": ["paragraph_readability_or_source_failures"],
                     "preflight": {"paragraph_findings": [{"paragraph_id": "S01-p2", "rule": rule, "severity": "major"}]}},
                    {"paragraph_score": score, "local_preflight": {"paragraph_findings": [], "paragraph_checks": []}},
                    paragraph_id="S01-p1", source_quality_artifact_id="q1",
                )
                self.assertEqual(rule == "C01", bool(quality["hard_gate_failures"]))
                self.assertEqual(93, quality["score"])

    def test_quality_findings_keep_scores_without_becoming_integrity_failures(self):
        for rule, severity, expected_score in (("P01", "major", 93), ("P01", "minor", 93), ("M05", "minor", 93), ("C01", "major", 79)):
            with self.subTest(rule=rule):
                finding = {"paragraph_id": "S01-p1", "rule": rule, "severity": severity, "route": "section_rewrite"}
                hard = feedback_loop.paragraph_finding_is_blocking(finding)
                self.assertEqual(rule == "C01", hard)
                result = feedback_loop.normalize_evaluation(
                    {"dimension_scores": [{"id": "readability", "level": 4}],
                     "paragraph_scores": [{"paragraph_id": "S01-p1", "score": 93, "severity": "none", "route": "pass"}]},
                    {"dimensions": [{"id": "readability", "weight": 100}]},
                    [{"paragraph_id": "S01-p1", "text": "A supported narrative."}],
                    {"paragraph_findings": [finding], "hard_regressions": []},
                    90, 85,
                )
                self.assertEqual(expected_score, result["paragraph_scores"][0]["score"])
                self.assertEqual(severity, result["paragraph_scores"][0]["severity"])
                self.assertIn(rule, result["paragraph_scores"][0]["failed_dimensions"])
                self.assertEqual(hard, bool(result["hard_gate_failures"]))
                if severity == "minor":
                    self.assertEqual("PASS", result["decision"])
                    self.assertEqual("final_polish", result["paragraph_scores"][0]["route"])

    def test_preflight_keeps_length_and_repetition_advisory_but_blocks_missing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "review-projects" / "p" / "04_first_draft"
            first.mkdir(parents=True)
            (first / "first_draft.md").write_text(
                "## Results\n\nA method was studied. A method was studied. [1]\n\n"
                "<!-- paragraph_id: S01-p1 -->\n\n## References\n\n[1] Source.\n", encoding="utf-8")
            for available in (True, False):
                with self.subTest(available=available), patch.object(feedback_loop, "source_evidence", return_value={
                    "paper_ids": ["P001"], "local_source_available": available,
                    "original_source_ready": available, "evidence_scope": "test",
                }), patch.object(feedback_loop, "citation_entries", return_value=[{"callout": 1}]), patch.object(
                    feedback_loop, "paragraph_metadata", return_value={"S01-p1": {"argument_role": "anchor_case"}}
                ):
                    result = feedback_loop.deterministic_preflight(root, "p", min_words=120, max_words=300)
                    findings = {row["rule"]: row for row in result["paragraph_findings"]}
                    self.assertFalse(findings["P01"]["hard_gate"])
                    self.assertEqual("minor", findings["P01"]["severity"])
                    self.assertFalse(findings["P03"]["hard_gate"])
                    self.assertEqual(not available, "paragraph_readability_or_source_failures" in result["hard_regressions"])
                    if not available:
                        self.assertTrue(findings["C01"]["hard_gate"])

    def test_repair_reuse_depends_on_evidence_not_score_or_job(self):
        from copy import deepcopy
        paragraph = {"paragraph_id": "S1-p1", "text": "Reported yield was 91% [1]."}
        issue = {"paragraph_id": "S1-p1", "failed_dimensions": ["support"], "score": 70}
        evidence = {"evidence": [{"paper_id": "P1", "source_content_hash": "source-v1",
                                  "original_passages": [{"ref": "p2", "text": "91% yield"}]}]}
        fingerprint = lambda i, e: feedback_loop.repair_input_fingerprint(paragraph, i, e, constraints={"min_words": 30})
        initial = fingerprint(issue, evidence)
        self.assertEqual(initial, fingerprint({**issue, "score": 71, "job_id": "another", "updated_at": "later"}, evidence))
        changed = deepcopy(evidence)
        changed["evidence"][0]["source_content_hash"] = "source-v2"
        self.assertNotEqual(initial, fingerprint(issue, changed))
        changed["evidence"][0]["original_passages"][0]["text"] = "Control gave 42% yield"
        self.assertNotEqual(initial, fingerprint(issue, changed))

    @classmethod
    def setUpClass(cls) -> None:
        feedback_loop.apply_verification_profile(
            feedback_loop.load_taxonomy_verification_profile(
                Path(__file__).resolve().parents[2],
                profile="chemistry_general",
                topic_text="allene chemistry",
            )
        )

    def validate(
        self,
        original: str,
        candidate: str,
        *,
        unsupported: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        return feedback_loop.validate_rewrite_report(
            original,
            candidate,
            1,
            200,
            allowed_unsupported_claims=unsupported,
        )

    def test_claim_fact_binding_requires_exact_original_excerpt_and_numbers(self) -> None:
        evidence = {
            "paper_ids": ["P001"],
            "evidence": [
                {
                    "paper_id": "P001",
                    "original_passages": [
                        {
                            "ref": "P001:p2:b3",
                            "text": "The reaction afforded 3aa in 82% yield at 25 °C.",
                        }
                    ],
                }
            ],
        }
        accepted = feedback_loop.validated_claim_fact_bindings(
            [
                {
                    "claim_text": "The reaction afforded 3aa in 82% yield.",
                    "paper_id": "P001",
                    "source_ref": "P001:p2:b3",
                    "support_excerpt": "The reaction afforded 3aa in 82% yield at 25 °C.",
                    "subject": "the reaction",
                    "predicate": "afforded",
                    "value": "3aa in 82% yield",
                    "confidence": 0.96,
                },
                {
                    "claim_text": "The reaction afforded 3aa in 99% yield.",
                    "paper_id": "P001",
                    "source_ref": "P001:p2:b3",
                    "support_excerpt": "The reaction afforded 3aa in 82% yield at 25 °C.",
                    "subject": "the reaction",
                    "predicate": "afforded",
                    "value": "3aa in 99% yield",
                    "confidence": 0.99,
                },
            ],
            paragraph_text=(
                "The reaction afforded 3aa in 82% yield. "
                "The reaction afforded 3aa in 99% yield."
            ),
            paragraph_evidence=evidence,
        )

        self.assertEqual(1, len(accepted))
        self.assertEqual("3aa in 82% yield", accepted[0]["value"])

    def test_ordinary_int_prefix_words_are_not_required_labels(self) -> None:
        signature = feedback_loop.protected_signature(
            "This interpretation places intermolecular products into context."
        )

        self.assertEqual(signature["required_labels"], [])

    def test_explicit_intermediate_and_compound_labels_remain_hard_protected(self) -> None:
        original = "Intermediate A forms int-I before TS1 affords compound 3aa [1]."
        candidate = "Intermediate A forms int-II before TS1 affords compound 3aa [1]."

        errors, _warnings = self.validate(original, candidate)

        self.assertIn("protected_required_labels_changed", errors)

    def test_generic_chemical_singular_plural_changes_do_not_block(self) -> None:
        original = "Allenes and alcohols were compared in the review [1]."
        candidate = "An allene and an alcohol were compared in this review [1]."

        errors, warnings = self.validate(original, candidate)

        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_new_generic_term_is_a_warning_not_a_hard_failure(self) -> None:
        errors, warnings = self.validate(
            "The reaction proceeded under the reported conditions [1].",
            "The allene reaction proceeded under the reported conditions [1].",
        )

        self.assertEqual(errors, [])
        self.assertIn("soft_chemical_terms_changed", warnings)

    def test_formula_and_numeric_changes_still_block(self) -> None:
        original = "ZnI2 gave the product in 90% yield and 92% ee [1]."
        candidate = "ZnCl2 gave the product in 80% yield and 92% ee [1]."

        errors, _warnings = self.validate(original, candidate)

        self.assertIn("protected_chemical_identities_changed", errors)
        self.assertIn("protected_numbers_changed", errors)

    def test_explicitly_unsupported_hard_values_may_be_removed(self) -> None:
        original = "Pd/SEGPHOS gave 90% ee in the reported experiment [1]."
        candidate = "The reported experiment requires further source confirmation [1]."

        errors, _warnings = self.validate(
            original,
            candidate,
            unsupported=["Pd/SEGPHOS gave 90% ee."],
        )

        self.assertEqual(errors, [])

    def test_figure_path_remains_exact_without_polluting_number_signature(self) -> None:
        original = "![Figure](/artifacts/P123/figure-42.png)\nThe reaction gave 90% yield [1]."
        candidate = "![Figure](/artifacts/P124/figure-42.png)\nThe reaction gave 90% yield [1]."

        signature = feedback_loop.protected_signature(original)
        errors, _warnings = self.validate(original, candidate)

        self.assertEqual(signature["numbers"], ["90%", "1"])
        self.assertIn("protected_images_changed", errors)

    def test_repeated_run_edge_junk_is_not_a_protected_chemical_identity(self) -> None:
        original = "LLLCCCHHHTTT The CuI reaction furnished an allene [1]."
        candidate = "The CuI reaction furnished an allene [1]."

        signature = feedback_loop.protected_signature(original)
        errors, _warnings = self.validate(original, candidate)

        self.assertNotIn("lllccchhhttt", signature["chemical_identities"])
        self.assertEqual([], errors)

    def test_repeated_run_edge_junk_is_removed_and_rejected_if_retained(self) -> None:
        noisy = "LLLCCCHHHTTT Scientific paragraph [1]."

        self.assertEqual(
            "Scientific paragraph [1].",
            feedback_loop.remove_edge_junk_tokens(noisy),
        )
        errors, _warnings = self.validate(noisy, noisy)
        self.assertIn("edge_junk_text_remains", errors)

    def test_rewrite_cannot_split_one_marked_paragraph_into_multiple_blocks(self) -> None:
        original = "The reported reaction was evaluated [1]."
        candidate = (
            "The reported reaction was evaluated.\n\n"
            "The result remained within the reported evidence [1]."
        )

        errors, _warnings = self.validate(original, candidate)

        self.assertIn("multiple_prose_blocks", errors)

    def test_rewrite_cannot_emit_a_paragraph_marker(self) -> None:
        original = "The reported reaction was evaluated [1]."
        candidate = (
            "The reported reaction was evaluated [1].\n\n"
            "<!-- paragraph_id: S01-p2 -->"
        )

        errors, _warnings = self.validate(original, candidate)

        self.assertIn("paragraph_marker_in_rewrite", errors)

    def test_rewrite_below_target_keeps_a_warning_without_blocking_valid_prose(self) -> None:
        original = " ".join(["word"] * 50)
        candidate = " ".join(["word"] * 49)

        errors, warnings = feedback_loop.validate_rewrite_report(
            original,
            candidate,
            50,
            1400,
        )

        self.assertNotIn("word_count_49_outside_50_1400", errors)
        self.assertIn("word_count_49_outside_50_1400", warnings)

    def test_repair_prompt_keeps_evidence_and_varies_by_attempt(self) -> None:
        prompt = feedback_loop.rewrite_repair_prompt(
            " ".join(["word"] * 50),
            " ".join(["word"] * 49),
            ["word_count_49_outside_50_1400"],
            50,
            1400,
            word_range_applicable=True,
            evidence={
                "paragraph_id": "S01-p1",
                "paper_ids": ["P001"],
                "evidence": [
                    {
                        "paper_id": "P001",
                        "title": "Evidence title",
                        "original_passages": [
                            {"ref": "P001-C01", "page": 1, "text": "Grounded evidence."}
                        ],
                    }
                ],
            },
            score={"diagnosis": "Expand without adding facts."},
            rewrite_mode="final_polish",
            repair_attempt=3,
        )

        self.assertIn("the rejected candidate has 49 words", prompt)
        self.assertIn("Generation attempt: 3", prompt)
        self.assertIn("Grounded evidence.", prompt)
        self.assertIn("Prefer a shorter supported paragraph", prompt)

    def test_paragraph_parser_keeps_adjacent_figure_outside_next_paragraph(self) -> None:
        markdown = """# Results

First scientific paragraph [1].

<!-- paragraph_id: S01-p1 -->

<!-- inserted_figure: {\"figure_id\":\"P001-F01\",\"target_paragraph_id\":\"S01-p1\"} -->
![Scheme 1.](/artifacts/P001-F01.png)
*Figure 1. Scheme 1.*

Second scientific paragraph gave 90% yield [2].

<!-- paragraph_id: S01-p2 -->
"""

        paragraphs = feedback_loop.parse_marked_paragraphs(markdown)

        self.assertEqual(["S01-p1", "S01-p2"], [row["paragraph_id"] for row in paragraphs])
        self.assertEqual("First scientific paragraph [1].", paragraphs[0]["text"])
        self.assertEqual(
            "Second scientific paragraph gave 90% yield [2].",
            paragraphs[1]["text"],
        )
        self.assertNotIn("inserted_figure", paragraphs[1]["text"])
        self.assertNotIn("![Scheme", paragraphs[1]["text"])

        updated = feedback_loop.replace_paragraph_in_markdown(
            markdown,
            "S01-p2",
            "Rewritten scientific paragraph gave 90% yield [2].",
        )
        self.assertEqual(1, updated.count("inserted_figure"))
        self.assertEqual(1, updated.count("![Scheme 1.]"))
        self.assertNotIn("Second scientific paragraph", updated)

    def test_interactive_human_confirmation_uses_style_only_mode(self) -> None:
        finding = {
            "paragraph_id": "S01-p1",
            "score": 55,
            "route": "human_confirmation",
            "unsupported_claims": [],
        }

        mode = feedback_loop.interactive_rewrite_mode(
            finding,
            {"paper_ids": ["P001"], "evidence": []},
            paragraph_goal=85,
        )

        self.assertEqual("human_review_style_only", mode)
        self.assertEqual(
            "",
            feedback_loop.automatic_rewrite_mode(
                finding,
                {"paper_ids": ["P001"], "evidence": []},
                paragraph_goal=85,
            ),
        )

    def test_style_only_prompt_keeps_manual_issue_unresolved(self) -> None:
        prompt = feedback_loop.rewrite_prompt(
            {"paragraph_id": "S01-p1", "text": "Evidence statement [1]."},
            {"route": "human_confirmation", "diagnosis": "Check the source."},
            {"paper_ids": ["P001"], "evidence": []},
            1,
            200,
            word_range_applicable=False,
            rewrite_mode="human_review_style_only",
        )

        self.assertIn(
            "still requires manual source or figure-identity confirmation",
            prompt,
        )
        self.assertIn("do not resolve", prompt.casefold())

    def test_unsupported_claim_with_local_full_text_uses_bounded_cleanup(self) -> None:
        mode = feedback_loop.automatic_rewrite_mode(
            {
                "paragraph_id": "S01-p1",
                "score": 60,
                "route": "section_rewrite",
                "source_check_status": "unsupported",
                "unsupported_claims": ["The catalyst universally gives 99% ee."],
            },
            {
                "paper_ids": ["P001"],
                "evidence": [{"original_text_available": True}],
            },
            paragraph_goal=85,
        )

        self.assertEqual("source_recheck_cleanup", mode)

    def test_needs_human_review_is_not_requeued_by_batch_optimizer(self) -> None:
        mode = feedback_loop.automatic_rewrite_mode(
            {
                "paragraph_id": "S01-p1",
                "score": 60,
                "route": "local_source_recheck",
                "source_check_status": "needs_human_review",
                "unsupported_claims": ["Ambiguous catalyst identity."],
            },
            {
                "paper_ids": ["P001"],
                "evidence": [{"original_text_available": True}],
            },
            paragraph_goal=85,
        )

        self.assertEqual("", mode)

    def test_checked_scope_miss_can_narrow_only_the_listed_unsupported_claim(self) -> None:
        mode = feedback_loop.automatic_rewrite_mode(
            {
                "paragraph_id": "S01-p1",
                "score": 60,
                "route": "section_rewrite",
                "source_check_status": "not_found_in_checked_scope",
                "evidence_rescue_status": "not_found_in_checked_scope",
                "unsupported_claims": ["The catalyst universally gives 99% ee."],
            },
            {
                "paper_ids": ["P001"],
                "evidence": [{"original_text_available": True}],
            },
            paragraph_goal=85,
        )

        self.assertEqual("source_recheck_cleanup", mode)

    def test_bounded_rescue_outcome_updates_reusable_baseline_queue(self) -> None:
        evaluation = {
            "paragraph_scores": [
                {
                    "paragraph_id": "S01-p1",
                    "score": 70,
                    "route": "local_source_recheck",
                    "severity": "major",
                    "source_check_status": "needs_human_review",
                    "unsupported_claims": ["The catalyst universally gives 99% ee."],
                }
            ]
        }
        updated = feedback_loop.apply_evidence_rescue_outcomes(
            evaluation,
            {
                "evidence_rescue_outcomes": {
                    "S01-p1": {"status": "not_found_in_checked_scope"}
                }
            },
            paragraph_goal=85,
        )

        row = updated["paragraph_scores"][0]
        self.assertEqual("not_found_in_checked_scope", row["source_check_status"])
        self.assertEqual("section_rewrite", row["route"])
        self.assertEqual("checked_scope_no_match", row["evidence_problem_type"])

    def test_unqualified_negative_is_narrowed_instead_of_sent_to_human(self) -> None:
        mode = feedback_loop.automatic_rewrite_mode(
            {
                "paragraph_id": "S01-p1",
                "score": 60,
                "route": "local_source_recheck",
                "source_check_status": "needs_human_review",
                "unsupported_claims": [
                    "The publication does not establish the catalyst role."
                ],
                "negative_claim_policies": {
                    "The publication does not establish the catalyst role.": (
                        "scope_limited_rewrite"
                    )
                },
                "evidence_problem_type": "unqualified_negative_claim",
            },
            {
                "paper_ids": ["P001"],
                "evidence": [{"original_text_available": True}],
            },
            paragraph_goal=85,
        )

        self.assertEqual("negative_claim_scope_narrowing", mode)

    def test_evaluation_prompt_excludes_placement_callouts_from_source_check(self) -> None:
        prompt = feedback_loop.evaluation_prompt(
            {"name": "rubric", "dimensions": []},
            [
                {
                    "paragraph_id": "S01-p1",
                    "heading": "Results",
                    "text": "Figure 1 summarizes the representative reactions.",
                }
            ],
            {"S01-p1": {"paper_ids": [], "evidence": []}},
            {"paragraph_checks": [], "paragraph_findings": []},
            85,
            85,
        )

        self.assertIn("is not a paper-level scientific claim", prompt)

    def test_writing_plan_roles_do_not_all_become_case_paragraphs(self) -> None:
        self.assertEqual(
            "anchor_case",
            feedback_loop.paragraph_argument_role(
                {"argument_role": "anchor_case", "paper_ids": ["P001"]}
            ),
        )
        self.assertEqual(
            "cross_study_comparison",
            feedback_loop.paragraph_argument_role(
                {
                    "argument_role": "cross_study_comparison",
                    "paper_ids": ["P001", "P002"],
                }
            ),
        )

    def test_evaluation_prompt_includes_role_and_prior_claim_closure(self) -> None:
        prompt = feedback_loop.evaluation_prompt(
            {"dimensions": [{"id": "P01", "weight": 100}]},
            [{"paragraph_id": "S01-p2", "heading": "Scope", "text": "Text [1]."}],
            {"S01-p2": {"paper_ids": ["P001"], "evidence": []}},
            {
                "paragraph_checks": [
                    {
                        "paragraph_id": "S01-p2",
                        "paragraph_role": "scope_limitation",
                        "word_range_applicable": False,
                    }
                ]
            },
            90,
            85,
            prior_quality_context={
                "claim_dispositions": {
                    "C1": {
                        "paragraph_id": "S01-p2",
                        "outcome": "narrowed",
                        "original_unsupported_claim": "Old overclaim",
                    },
                    "C2": {
                        "paragraph_id": "S01-p2",
                        "disposition": "downgraded_due_to_insufficient_evidence",
                        "unsupported_claim": "Unresolved old diagnosis",
                    },
                }
            },
        )

        self.assertIn('"paragraph_role": "scope_limitation"', prompt)
        self.assertIn("Old overclaim", prompt)
        self.assertNotIn("Unresolved old diagnosis", prompt)


if __name__ == "__main__":
    unittest.main()
