from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from review_writer_core.latex_renderer import latex_escape, render_tex
from review_writer_core.manuscript_state import build_manuscript_state


class ManuscriptStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = (
            Path(__file__).resolve().parents[1]
            / "review_writer_core"
            / "resources"
            / "pdf"
            / "modern-survey.tex"
        ).read_text(encoding="utf-8")

    def test_state_preserves_blocks_citations_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "scheme.png"
            image.write_bytes(b"image")
            artifact_id = "12345678-1234-1234-1234-123456789abc"
            markdown = f"""# Selective Allenation

## Introduction

The reported systems support a bounded comparison [1, 2].

![General reaction](/api/v1/artifacts/{artifact_id}/content)
*Figure 1. General reaction and comparison axis.*

| System | ee |
|---|---:|
| Cu | 95% |

*Table 1. Representative system and selectivity.*

## References

[1] Author A. Journal 2024, 1, 1-10.
[2] Author B. Journal 2025, 2, 11-20.
"""
            state = build_manuscript_state(
                markdown, artifact_paths={artifact_id: str(image)}
            )
        self.assertTrue(state["validation"]["valid"])
        self.assertEqual("Selective Allenation", state["title"])
        self.assertEqual([1, 2], state["citation_numbers"])
        self.assertEqual([1, 2], state["reference_numbers"])
        self.assertEqual(1, state["counts"]["images"])
        self.assertEqual(1, state["counts"]["tables"])
        table = next(block for block in state["blocks"] if block["kind"] == "table")
        self.assertEqual("Table 1. Representative system and selectivity.", table["caption"])

    def test_raw_html_and_undefined_citation_are_blockers(self) -> None:
        state = build_manuscript_state(
            "# Review\n\nA result <sup>2</sup> was reported [3].\n\n## References\n\n[1] Source.\n"
        )
        issue_types = {
            issue["type"] for issue in state["validation"]["blocking_issues"]
        }
        self.assertIn("html_residue", issue_types)
        self.assertIn("undefined_citation", issue_types)

    def test_workflow_html_comments_are_ignored_by_pdf_semantics(self) -> None:
        state = build_manuscript_state(
            "# Review\n\nEvidence paragraph [1].\n\n"
            "<!-- paragraph_id: S01-p1 -->\n\n"
            '<!-- inserted_figure: {"figure_id":"P001-F01",'
            '"target_paragraph_id":"S01-p1"} -->\n\n'
            "## References\n\n[1] Source.\n"
        )

        self.assertTrue(state["validation"]["valid"])
        self.assertEqual(2, state["counts"]["comments_ignored"])
        semantic_text = "\n".join(
            str(block.get("text") or "") for block in state["blocks"]
        )
        self.assertNotIn("paragraph_id", semantic_text)
        self.assertNotIn("inserted_figure", semantic_text)
        rendered = render_tex(state, profile="en", template=self.template)
        self.assertNotIn("paragraph\\_id", rendered)
        self.assertNotIn("inserted\\_figure", rendered)

    def test_malformed_html_comment_blocks_pdf_publication(self) -> None:
        state = build_manuscript_state(
            "# Review\n\nEvidence.\n\n<!-- paragraph_id: S01-p1\n"
        )

        issue_types = {
            issue["type"] for issue in state["validation"]["blocking_issues"]
        }
        self.assertIn("malformed_html_comment", issue_types)

    def test_citation_ranges_are_expanded_and_reference_list_is_not_a_callout(self) -> None:
        state = build_manuscript_state(
            "# Review\n\nEvidence spans the series [1–3].\n\n## References\n\n"
            "[1] A.\n[2] B.\n[3] C.\n[4] Uncited.\n"
        )
        self.assertEqual([1, 2, 3], state["citation_numbers"])
        self.assertEqual([1, 2, 3, 4], state["reference_numbers"])
        warnings = state["validation"]["warning_issues"]
        self.assertEqual([4], warnings[0]["reference_numbers"])

    def test_duplicate_reference_numbers_block_publication(self) -> None:
        state = build_manuscript_state(
            "# Review\n\nFinding [1].\n\n## References\n\n[1] A.\n[1] B.\n"
        )
        issue_types = {
            issue["type"] for issue in state["validation"]["blocking_issues"]
        }
        self.assertIn("duplicate_reference_number", issue_types)

    def test_en_and_zh_profiles_share_one_template(self) -> None:
        state = build_manuscript_state("# Review\n\n## Introduction\n\nText.\n")
        english = render_tex(state, profile="en", template=self.template)
        chinese = render_tex(state, profile="zh-CN", template=self.template)
        self.assertIn("TeX Gyre Termes", english)
        self.assertIn("ctex", chinese)
        self.assertIn("modern-survey/2", english)
        self.assertIn("twocolumn", english)
        self.assertIn("journalpanel", english)
        self.assertNotIn("titlepage", english)
        self.assertNotIn("tableofcontents", english)
        self.assertNotIn("%__DOCUMENT_BODY__%", chinese)

    def test_explicit_abstract_and_keywords_move_into_title_panel_once(self) -> None:
        state = build_manuscript_state(
            "# Review\n\nAuthors: A. Author, B. Author\n\n"
            "Affiliation: Example Institute\n\n## Abstract\n\n"
            "A bounded synthesis of the field [1].\n\n"
            "Keywords: synthesis, mechanism\n\n## Introduction\n\nBody [1].\n\n"
            "## References\n\n[1] Source.\n"
        )
        self.assertEqual("A bounded synthesis of the field [1].", state["front_matter"]["abstract"])
        self.assertEqual(["synthesis", "mechanism"], state["front_matter"]["keywords"])
        self.assertEqual(["A. Author", "B. Author"], state["front_matter"]["authors"])
        rendered = render_tex(state, profile="en", template=self.template)
        self.assertEqual(1, rendered.count("A bounded synthesis of the field"))
        self.assertEqual(1, rendered.count(r"\section{Introduction}"))
        self.assertNotIn(r"\section{Abstract}", rendered)

    def test_safe_math_is_preserved_but_tex_injection_is_escaped(self) -> None:
        value = latex_escape(r"The $S_N^2$ path differs from $\input{secret}$.")
        self.assertIn("$S_N^2$", value)
        self.assertNotIn(r"$\input{secret}$", value)
        self.assertIn(r"\textbackslash{}input", value)

    def test_scientific_symbols_use_extractable_math_glyphs(self) -> None:
        value = latex_escape("The C≡C bond, Pd₂(dba)₃, non-equivalent pathways (A≠B), and 3′ terminus are preserved.")

        self.assertIn(r"C\ensuremath{\equiv}C", value)
        self.assertIn(r"Pd\ensuremath{_{2}}(dba)\ensuremath{_{3}}", value)
        self.assertIn(r"A\ensuremath{\neq}B", value)
        self.assertIn(r"3\ensuremath{^{\prime}}", value)
        self.assertNotIn("≡", value)
        self.assertNotIn("′", value)

    def test_latex_owns_figure_and_table_numbering(self) -> None:
        artifact_id = "12345678-1234-1234-1234-123456789abc"
        state = build_manuscript_state(
            f"# Review\n\n![Scheme](/api/v1/artifacts/{artifact_id}/content)\n"
            "*Scheme 7. General catalytic cycle.*\n\n"
            "*Table 4. Shared comparison dimensions.*\n\n"
            "| Method | Boundary |\n|---|---|\n| Cu | Hindered substrates |\n",
            artifact_paths={artifact_id: "C:/safe/scheme.pdf"},
        )
        rendered = render_tex(state, profile="en", template=self.template)
        self.assertIn(r"\captionsetup{name=Scheme}", rendered)
        self.assertIn(r"\caption{General catalytic cycle.}", rendered)
        self.assertIn(r"\caption{Shared comparison dimensions.}", rendered)
        self.assertNotIn("Scheme 7", rendered)
        self.assertNotIn("Table 4", rendered)

    def test_references_flush_pending_double_column_figures(self) -> None:
        state = build_manuscript_state(
            "# Review\n\n## Results\n\nText [1].\n\n"
            "## References\n\n[1] Source.\n"
        )

        rendered = render_tex(state, profile="en", template=self.template)

        self.assertIn("\\clearpage\n\\balance\n\\section{References}", rendered)


if __name__ == "__main__":
    unittest.main()
