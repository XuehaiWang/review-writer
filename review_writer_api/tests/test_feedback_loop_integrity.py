from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


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

    def test_rewrite_below_configured_minimum_remains_blocking(self) -> None:
        original = " ".join(["word"] * 50)
        candidate = " ".join(["word"] * 49)

        errors, _warnings = feedback_loop.validate_rewrite_report(
            original,
            candidate,
            50,
            1400,
        )

        self.assertIn("word_count_49_outside_50_1400", errors)

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

        self.assertIn("The rejected candidate contains 49 words", prompt)
        self.assertIn("Generation attempt: 3", prompt)
        self.assertIn("Grounded evidence.", prompt)
        self.assertIn("never return a candidate below", prompt)

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


if __name__ == "__main__":
    unittest.main()
