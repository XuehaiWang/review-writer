from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from review_writer_core.latex_renderer import TEMPLATE_VERSION, latex_escape, render_tex
from review_writer_core.manuscript_state import (
    build_manuscript_state,
    choose_figure_layout,
)


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

    def test_nested_chemistry_brackets_in_image_alt_render_as_an_image(self) -> None:
        artifact_id = "f1984c55-b8bf-4c87-93c8-1e8238f9c87d"
        markdown = (
            "# Review\n\n## Introduction\n\n"
            '<!-- inserted_figure: {"figure_id":"P001-F01",'
            f'"output_artifact_id":"{artifact_id}"}} -->\n'
            "![Mechanism for the phosphine-catalyzed [3+2] cycloaddition]"
            f"(/api/v1/artifacts/{artifact_id}/content)\n"
            "*Figure 1. Mechanism for the phosphine-catalyzed [3+2] cycloaddi tion.*\n"
        )

        state = build_manuscript_state(
            markdown, artifact_paths={artifact_id: "C:/safe/mechanism.png"}
        )

        self.assertTrue(state["validation"]["valid"])
        self.assertEqual(1, state["counts"]["images"])
        image = next(block for block in state["blocks"] if block["kind"] == "image")
        self.assertEqual(
            "Mechanism for the phosphine-catalyzed [3+2] cycloaddition",
            image["alt"],
        )
        self.assertIn("cycloaddition", image["caption"])
        self.assertNotIn("cycloaddi tion", image["caption"])
        rendered = render_tex(state, profile="en", template=self.template)
        self.assertIn(r"\includegraphics", rendered)
        self.assertNotIn("![Mechanism", rendered)

    def test_malformed_image_syntax_blocks_publication(self) -> None:
        state = build_manuscript_state(
            "# Review\n\n![Mechanism [3+2](/api/v1/artifacts/broken/content)\n"
        )

        issue_types = {
            issue["type"] for issue in state["validation"]["blocking_issues"]
        }
        self.assertIn("malformed_markdown_image", issue_types)

    def test_inserted_figure_marker_must_match_a_parsed_image(self) -> None:
        artifact_id = "f1984c55-b8bf-4c87-93c8-1e8238f9c87d"
        state = build_manuscript_state(
            "# Review\n\n"
            '<!-- inserted_figure: {"figure_id":"P001-F01",'
            f'"output_artifact_id":"{artifact_id}"}} -->\n'
            "Body text without its routed image.\n"
        )

        issue_types = {
            issue["type"] for issue in state["validation"]["blocking_issues"]
        }
        self.assertIn("inserted_figure_image_mismatch", issue_types)

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
        self.assertNotIn(TEMPLATE_VERSION, english)
        self.assertNotIn("REVIEW WRITER", english)
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
        self.assertIn(r"\par\vspace{0.8em}\noindent", rendered)
        self.assertIn(r"\textbf{Keywords:} synthesis; mechanism\par", rendered)

    def test_keywords_are_split_from_abstract_without_blank_markdown_line(self) -> None:
        state = build_manuscript_state(
            "# Review\n\n## Abstract\n\n"
            "Evidence-grounded synthesis.\n"
            "Keywords: catalysis, selectivity\n\n"
            "## Introduction\n\nBody.\n"
        )

        self.assertEqual(
            "Evidence-grounded synthesis.", state["front_matter"]["abstract"]
        )
        self.assertEqual(
            ["catalysis", "selectivity"], state["front_matter"]["keywords"]
        )

    def test_emphasized_authors_and_keywords_are_front_matter_fields(self) -> None:
        state = build_manuscript_state(
            "# Review\n"
            "**Authors:** A. Author, B. Author\n"
            "## Abstract\n"
            "Evidence-grounded synthesis.\n\n"
            "**Keywords:** catalysis, selectivity\n\n"
            "## Introduction\n\nBody.\n"
        )

        self.assertEqual(
            "Evidence-grounded synthesis.", state["front_matter"]["abstract"]
        )
        self.assertEqual(
            ["catalysis", "selectivity"], state["front_matter"]["keywords"]
        )
        self.assertEqual(
            ["A. Author", "B. Author"], state["front_matter"]["authors"]
        )
        rendered = render_tex(state, profile="en", template=self.template)
        self.assertEqual(1, rendered.count("A. Author, B. Author"))
        self.assertIn(r"\par\vspace{0.8em}\noindent", rendered)
        self.assertIn(r"\textbf{Keywords:} catalysis; selectivity\par", rendered)

    def test_safe_math_is_preserved_but_tex_injection_is_escaped(self) -> None:
        value = latex_escape(r"The $S_N^2$ path differs from $\input{secret}$.")
        self.assertIn("$S_N^2$", value)
        self.assertNotIn(r"$\input{secret}$", value)
        self.assertIn(r"\textbackslash{}input", value)

    def test_scientific_symbols_use_extractable_math_glyphs(self) -> None:
        value = latex_escape(
            "The C≡C bond, Pd₂(dba)₃, speciesᵢ, x²⁺, kⁿ, "
            "non-equivalent pathways (A≠B), and 3′ terminus are preserved."
        )

        self.assertIn(r"C\ensuremath{\equiv}C", value)
        self.assertIn(r"Pd\ensuremath{_{2}}(dba)\ensuremath{_{3}}", value)
        self.assertIn(r"A\ensuremath{\neq}B", value)
        self.assertIn(r"3\ensuremath{^{\prime}}", value)
        self.assertIn(r"species\ensuremath{_{i}}", value)
        self.assertIn(r"x\ensuremath{^{2}}\ensuremath{^{+}}", value)
        self.assertIn(r"k\ensuremath{^{n}}", value)
        self.assertNotIn("≡", value)
        self.assertNotIn("′", value)
        self.assertNotIn("ᵢ", value)

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

    def test_figure_layout_uses_geometry_then_semantic_role(self) -> None:
        self.assertEqual(
            "double",
            choose_figure_layout(width=1800, height=900)["span"],
        )
        self.assertEqual(
            "single",
            choose_figure_layout(
                representative_role="workflow", width=800, height=1000
            )["span"],
        )
        overview = choose_figure_layout(
            representative_role="conceptual_overview",
            width=600,
            height=900,
            requested_span="single",
            review_overview=True,
        )
        self.assertEqual("double", overview["span"])
        self.assertEqual("review_overview_required", overview["reason"])
        self.assertEqual(
            "double",
            choose_figure_layout(representative_role="conceptual_overview")["span"],
        )
        self.assertEqual(
            "single",
            choose_figure_layout(representative_role="structure_image")["span"],
        )

    def test_pdf_figures_can_mix_single_and_double_column_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compact_id = "12345678-1234-1234-1234-123456789abc"
            wide_id = "22345678-1234-1234-1234-123456789abc"
            compact = root / "compact.png"
            wide = root / "wide.png"
            Image.new("RGB", (800, 1000), "white").save(compact)
            Image.new("RGB", (1800, 900), "white").save(wide)
            markdown = (
                "# Review\n\n## Results\n\n"
                '<!-- inserted_figure: {"representative_role":"structure_image",'
                f'"output_artifact_id":"{compact_id}"}} -->\n'
                f"![Structure](/api/v1/artifacts/{compact_id}/content)\n"
                "*Figure 1. Representative structure.*\n\n"
                '<!-- inserted_figure: {"representative_role":"scope_samples",'
                f'"output_artifact_id":"{wide_id}"}} -->\n'
                f"![Scope](/api/v1/artifacts/{wide_id}/content)\n"
                "*Figure 2. Representative substrate scope.*\n"
            )
            state = build_manuscript_state(
                markdown,
                artifact_paths={compact_id: str(compact), wide_id: str(wide)},
            )

        images = [block for block in state["blocks"] if block["kind"] == "image"]
        self.assertEqual(["single", "double"], [block["layout_span"] for block in images])
        rendered = render_tex(state, profile="en", template=self.template)
        self.assertIn(r"\begin{figure}[!htbp]", rendered)
        self.assertIn(r"width=\columnwidth,height=0.42\textheight", rendered)
        self.assertIn(r"\begin{figure*}[!tbp]", rendered)
        self.assertIn(r"width=\textwidth,height=0.56\textheight", rendered)

    def test_figure_layout_metadata_can_override_automatic_choice(self) -> None:
        artifact_id = "12345678-1234-1234-1234-123456789abc"
        state = build_manuscript_state(
            "# Review\n\n"
            '<!-- inserted_figure: {"representative_role":"workflow",'
            '"layout_span":"single",'
            f'"output_artifact_id":"{artifact_id}"}} -->\n'
            f"![Workflow](/api/v1/artifacts/{artifact_id}/content)\n"
            "*Figure 1. Study workflow.*\n",
            artifact_paths={artifact_id: "C:/safe/workflow.png"},
        )

        image = next(block for block in state["blocks"] if block["kind"] == "image")
        self.assertEqual("single", image["layout_span"])
        self.assertEqual("explicit_override", image["layout_reason"])

    def test_final_review_overview_is_always_double_column(self) -> None:
        artifact_id = "12345678-1234-1234-1234-123456789abc"
        with tempfile.TemporaryDirectory() as temporary:
            overview = Path(temporary) / "overview.png"
            Image.new("RGB", (600, 900), "white").save(overview)
            state = build_manuscript_state(
                "# Review\n\n"
                f"![Overview figure](/api/v1/artifacts/{artifact_id}/content)\n"
                "*Review overview. Classification and evidence flow.*\n",
                artifact_paths={artifact_id: str(overview)},
            )

        image = next(block for block in state["blocks"] if block["kind"] == "image")
        self.assertTrue(image["review_overview"])
        self.assertEqual("double", image["layout_span"])
        self.assertEqual("review_overview_required", image["layout_reason"])

    def test_references_drain_figures_without_forcing_a_new_page(self) -> None:
        state = build_manuscript_state(
            "# Review\n\n## Results\n\nText [1].\n\n"
            "## References\n\n[1] Source.\n"
        )

        rendered = render_tex(state, profile="en", template=self.template)

        self.assertIn("\\FloatBarrier\n\\balance\n\\section{References}", rendered)
        self.assertNotIn("\\clearpage\n\\balance\n\\section{References}", rendered)

    def test_template_compacts_single_and_double_column_float_queues(self) -> None:
        self.assertIn(r"\usepackage{placeins}", self.template)
        self.assertIn(r"\setcounter{totalnumber}{6}", self.template)
        self.assertIn(r"\setcounter{dbltopnumber}{3}", self.template)
        self.assertIn(r"\renewcommand{\textfraction}{0.06}", self.template)
        self.assertIn(r"\renewcommand{\dbltopfraction}{0.92}", self.template)
        self.assertIn(r"\setlength{\dblfloatsep}{9pt plus 2pt minus 2pt}", self.template)
        self.assertIn(r"\setlength{\@fpsep}{10pt plus 2pt minus 2pt}", self.template)
        self.assertIn(r"\setlength{\@fptop}{0pt}", self.template)
        self.assertIn(r"\setlength{\@dblfpsep}{12pt plus 2pt minus 2pt}", self.template)
        self.assertIn(r"\setlength{\@dblfptop}{0pt}", self.template)


if __name__ == "__main__":
    unittest.main()
