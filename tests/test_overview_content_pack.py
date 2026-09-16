from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "review-figure-style-redraw"
    / "scripts"
    / "generate_overview_figure.py"
)
SPEC = importlib.util.spec_from_file_location("overview_content_pack", SCRIPT)
overview = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = overview
SPEC.loader.exec_module(overview)


def _project_with_draft(body: str) -> Path:
    root = Path(tempfile.mkdtemp())
    draft = root / "04_first_draft" / "first_draft.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(body, encoding="utf-8")
    return root


DRAFT = "\n\n".join(
    [
        "# Topic",
        "## Introduction\n\nScope of the review. <!-- paragraph_id: S01-p1 -->",
        "## Route one\n\nConditions A with catalyst-A give the product.",
        "## Route two\n\nConditions B with catalyst-B give the product.",
        "## References\n\n[1] Author. Title. Journal.",
    ]
)


class DraftExcerptTests(unittest.TestCase):
    def test_oversized_summary_is_rewritten_once_with_source_binding(self):
        features = {"review_title": "Synthesis", "overview_evidence_bindings": {"Route": {"section_id": "S1"}},
                    "argument_execution": {"sections": [{"section_id": "S1", "claims": [{"claim_id": "C1"}]}]}}
        long_row = {"section_id": "S1", "summary": "word " * 20, "claim_ids": ["C1"]}
        short_row = {**long_row, "summary": "Catalysis enables selective allene synthesis."}
        with patch.object(overview, "_text_gateway_configured", return_value=True), \
             patch.object(overview, "_draft_excerpt", return_value="Source evidence."), \
             patch.object(overview, "_cached_overview_json", side_effect=[
                 {"module_summaries": [long_row]}, {"module_summaries": [short_row]}]) as model:
            pack = overview._llm_content_pack(features)
        self.assertEqual({"S1": short_row["summary"]}, pack["module_summaries"])
        self.assertEqual(2, model.call_count)
        self.assertEqual("overview-summary-rewrite", model.call_args.kwargs["label"])

    def test_unsuccessful_summary_rewrite_does_not_publish_truncated_text(self):
        features = {"review_title": "Synthesis", "overview_evidence_bindings": {"Route": {"section_id": "S1"}},
                    "argument_execution": {"sections": [{"section_id": "S1", "claims": [{"claim_id": "C1"}]}]}}
        data = {"module_summaries": [{"section_id": "S1", "summary": "word " * 20, "claim_ids": ["C1"]}]}
        with patch.object(overview, "_text_gateway_configured", return_value=True), \
             patch.object(overview, "_draft_excerpt", return_value="Source evidence."), \
             patch.object(overview, "_cached_overview_json", return_value=data) as model:
            with self.assertRaisesRegex(ValueError, "display budget"):
                overview._llm_content_pack(features)
        self.assertEqual(2, model.call_count)

    def test_completed_steps_cache_but_failures_do_not(self):
        with tempfile.TemporaryDirectory() as temp:
            features = {"_project_dir": Path(temp)}
            with patch.object(overview, "call_gateway_json", return_value={"value": "ok"}) as call:
                first = overview._cached_overview_json(features, "prompt", label="test", timeout_seconds=1)
                self.assertEqual(first, overview._cached_overview_json(features, "prompt", label="test", timeout_seconds=1))
                self.assertEqual(1, call.call_count)
                overview._cached_overview_json(features, "changed", label="test", timeout_seconds=1)
                self.assertEqual(2, call.call_count)
            with patch.object(overview, "call_gateway_json", side_effect=RuntimeError("provider offline")) as call:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "offline"):
                        overview._cached_overview_json(features, "failure", label="test", timeout_seconds=1)
                self.assertEqual(2, call.call_count)

    def test_unavailable_provider_does_not_create_concept_pack(self):
        with patch.object(overview, "_text_gateway_configured", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                overview._llm_content_pack({"review_title": "Topic"})

    def test_overview_summarizes_without_rendering_provenance_or_result_tables(self):
        import copy
        features = {"overview_modules": ["Method"],
                    "overview_evidence_bindings": {"Method": {"section_id": "S1"}},
                    "argument_execution": {"sections": [{"section_id": "S1", "claims": [{
                        "claim": "A method supports selective product formation.",
                        "paper_ids": ["P123456789"], "result_context": [{"unit": "ee", "value": "99"}],
                    }]}]}}
        original = copy.deepcopy(features)
        text = overview._build_metal_rows_text(features)
        self.assertIn("at most 12 words and 84 characters", text)
        self.assertNotIn("Supporting assertion:", text)
        self.assertIn("Use the category label only", text)
        self.assertNotIn("source studies:", text)
        self.assertNotIn("P123456789", text)
        self.assertNotIn('"unit":', text)
        self.assertEqual(features, original)  # Provenance remains intact outside the image.
        notes = overview._build_take_home_text({"_content_pack": {"take_home": ["A", "B", "C", "D"]}})
        self.assertIn("omit duplicates", notes)
        self.assertNotIn("4. D", notes)

    def test_module_summaries_are_short_and_bound_to_the_correct_section(self):
        features = {"overview_modules": ["Old method", "New method"],
                    "overview_evidence_bindings": {"Old method": {"section_id": "S1"},
                                                   "New method": {"section_id": "S2"}},
                    "argument_execution": {"sections": [
                        {"section_id": "S1", "claims": [{"claim_id": "C1"}]},
                        {"section_id": "S2", "claims": [{"claim_id": "C2"}]}]}}
        data = {"module_summaries": [
            {"section_id": "S1", "summary": "Enables controlled conversion.", "claim_ids": ["C1"]},
            {"section_id": "S2", "summary": "Wrong source.", "claim_ids": ["C1"]}]}
        self.assertEqual({"S1": "Enables controlled conversion."},
                         overview._validated_module_summaries(data, features))
        data["module_summaries"][0]["summary"] = "word " * 19
        self.assertEqual({}, overview._validated_module_summaries(data, features))

    def test_five_modules_keep_order_without_copying_long_claims(self):
        labels = [f"Complete scientific heading {n}" for n in range(5)]
        features = {"overview_modules": labels, "argument_execution": {"sections": []},
                    "overview_evidence_bindings": {label: {"section_id": str(n)} for n, label in enumerate(labels)},
                    "_content_pack": {"module_summaries": {str(n): "Concise finding." for n in range(5)}}}
        text = overview._build_metal_rows_text(features)
        self.assertEqual(5, text.count("Module:"))
        self.assertEqual(5, text.count("Summary: Concise finding."))
        self.assertEqual(sorted(text.index(label) for label in labels), [text.index(label) for label in labels])

    def test_excerpt_reaches_later_sections(self) -> None:
        body = "# Topic\n\n## Introduction\n\n" + "intro filler. " * 300 + "\n\n## Deep section\n\nNEEDLE catalyst conditions."
        excerpt = overview._draft_excerpt({"_project_dir": _project_with_draft(body)})
        self.assertIn("NEEDLE", excerpt)
        self.assertNotIn("<!--", excerpt)

    def test_bibliography_section_is_not_sampled(self) -> None:
        excerpt = overview._draft_excerpt({"_project_dir": _project_with_draft(DRAFT)})
        self.assertIn("catalyst-A", excerpt)
        self.assertIn("catalyst-B", excerpt)
        self.assertNotIn("Journal", excerpt)

    def test_excerpt_stays_within_budget(self) -> None:
        excerpt = overview._draft_excerpt({"_project_dir": _project_with_draft(DRAFT)}, limit=600)
        self.assertLessEqual(len(excerpt), 600)

    def test_missing_draft_reports_reason(self) -> None:
        self.assertEqual(overview._draft_excerpt({}), "(no draft available)")


class StereoMotifTests(unittest.TestCase):
    def test_stereo_descriptor_survives_validation(self) -> None:
        smiles = "*C#C[C@@H](*)O"
        self.assertEqual(overview._clean_motif_smiles(smiles), smiles)

    def test_stereo_motif_draws_a_wedge(self) -> None:
        try:
            from rdkit import Chem
        except ImportError:
            self.skipTest("rdkit not installed")
        mol = Chem.MolFromSmiles("*C#C[C@@H](*)O")
        self.assertIsNotNone(mol)
        tags = [
            atom.GetChiralTag()
            for atom in mol.GetAtoms()
            if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED
        ]
        self.assertEqual(len(tags), 1)


class SectionSliceTests(unittest.TestCase):
    NEUTRAL = (
        "Neutral opening text about the scope of the route and the classes of "
        "products it covers, with no conditions vocabulary at all. "
    )
    FILLER = "A framing account discusses the ligand MARKERWORD in general terms without numbers. "
    METRIC = "The product was obtained in 96% ee at 40 degrees C."

    def test_measurement_sentences_are_taken_before_word_only_ones(self) -> None:
        out = overview._section_slice(self.NEUTRAL + self.FILLER * 3 + self.METRIC, 300)
        self.assertIn("96% ee", out)
        self.assertLess(out.index("96% ee"), out.index("MARKERWORD"))

    def test_short_section_is_returned_whole(self) -> None:
        section = "## Route\n\nShort section reporting 95% yield."
        self.assertEqual(
            overview._section_slice(section, 300),
            "## Route Short section reporting 95% yield.",
        )

    def test_slice_stays_within_budget(self) -> None:
        out = overview._section_slice(
            self.NEUTRAL * 4 + self.FILLER * 20 + self.METRIC * 5, 400)
        self.assertLessEqual(len(out), 420)
        self.assertIn("96% ee", out)


class MultiReactantSchemeTests(unittest.TestCase):
    def test_reactant_pair_is_validated_fragment_by_fragment(self) -> None:
        smiles, problem = overview._clean_reaction_side("*C#C.*C=O", 3)
        self.assertEqual(smiles, "*C#C.*C=O")
        self.assertEqual(problem, "")

    def test_too_many_reactants_are_rejected_with_a_count(self) -> None:
        smiles, problem = overview._clean_reaction_side("*C#C.*C=O.*C=O.*CC", 3)
        self.assertEqual(smiles, "")
        self.assertIn("4 dot-separated molecules", problem)

    def test_invalid_component_is_rejected_on_its_own(self) -> None:
        smiles, problem = overview._clean_reaction_side("*C#C.*C(*)#CC(*)OC(*)=O", 3)
        self.assertEqual(smiles, "")
        self.assertIn("component", problem)

    def test_product_must_be_a_single_molecule(self) -> None:
        scheme, problem = overview._scheme_from_data(
            {"substrate_smiles": "*C#C.*C=O", "product_smiles": "*C=C=C*.*O"}
        )
        self.assertIsNone(scheme)
        self.assertIn("exactly one molecule", problem)

    def test_two_reactant_scheme_renders_and_cleans_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "scheme.png"
            rendered = overview.render_reaction_scheme(
                {
                    "substrate_smiles": "*C#C.*C=O",
                    "substrate_name": "terminal alkyne and aldehyde",
                    "product_smiles": "*C(O)=C=C*",
                    "product_name": "axially chiral allenol",
                    "catalyst_label": "",
                    "reaction_name": "allenol formation",
                },
                out,
            )
            self.assertEqual(rendered, out)
            self.assertTrue(out.exists())
            leftovers = list(Path(tmp).glob("*.scheme_sub*.png")) + list(
                Path(tmp).glob("*.scheme_prod.png")
            )
            self.assertEqual(leftovers, [])


class AutomaticChemistryDecisionTests(unittest.TestCase):
    SCHEME = {
        "substrate_smiles": "*C#C",
        "substrate_name": "terminal alkyne",
        "product_smiles": "*C(*)=C=C(*)*",
        "product_name": "allene",
        "catalyst_label": "CuI, base",
        "reaction_name": "allenation",
    }

    def test_unavailable_review_does_not_masquerade_as_skeleton_evidence(self) -> None:
        with patch.object(overview, "_text_gateway_configured", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                overview._automatic_chemistry_decision(
                    {"review_title": "Allene synthesis", "product_keywords": ["allenes"]}, self.SCHEME)

    def test_low_confidence_second_review_uses_concept_overview(self) -> None:
        with patch.object(overview, "_text_gateway_configured", return_value=True), patch.object(
            overview,
            "call_gateway_json",
            return_value={"supported": False, "confidence": 20, "reason": "No matching evidence."},
        ):
            decision = overview._automatic_chemistry_decision(
                {"review_title": "Allene synthesis", "product_keywords": ["allenes"]},
                self.SCHEME,
            )
        self.assertEqual("concept", decision["mode"])
        self.assertEqual(20, decision["confidence"])

    def test_invalid_explicit_smiles_is_not_used_as_an_override(self) -> None:
        self.assertEqual(
            "",
            overview.resolve_skeleton_smiles({"skeleton_smiles": "C#C(C)(C)C"}),
        )


class TemplateSelectionTests(unittest.TestCase):
    def test_ai_selects_from_declared_capabilities_not_a_fixed_template_id(self) -> None:
        templates = [
            {
                "id": 41, "name": "matrix", "layout_type": "matrix",
                "description": "comparison matrix", "prompt": "classification",
                "layout_capabilities": {"reaction_slot": "none", "module_count_range": [2, 6]},
            },
            {
                "id": 99, "name": "hero", "layout_type": "hero",
                "description": "reaction-led overview", "prompt": "reaction scheme top",
                "layout_capabilities": {"reaction_slot": "hero-horizontal", "module_count_range": [2, 6]},
            },
        ]
        features = {
            "group_by": ["reaction_type"], "metal_categories": ["A", "B"],
            "has_chirality": True, "has_reaction_focus": True, "num_sections": 3,
            "_chemistry_decision": {"mode": "reaction"},
        }
        with patch.object(overview, "_text_gateway_configured", return_value=True), patch.object(
            overview, "call_gateway_json", return_value={"template_id": 99, "reason": "Hero slot fits."}
        ):
            selected = overview.select_best_template(templates, features)
        self.assertEqual(99, selected["id"])
        self.assertEqual("ai", features["_template_selection"]["mode"])


class TwoDimensionalChemistryTests(unittest.TestCase):
    def test_product_motif_never_uses_legacy_3d_renderer(self):
        path = Path("motif.png")
        with patch.object(overview, "resolve_skeleton_smiles", return_value="CCO"), patch.object(
            overview, "_render_motif_2d", return_value=path
        ) as render, patch.object(overview, "render_smiles_ball_and_stick") as legacy:
            self.assertEqual(path, overview.render_skeleton_model({}, path, style="ai3d"))
            render.assert_called_once()
            legacy.assert_not_called()

    def test_missing_2d_renderer_does_not_substitute_ball_and_stick(self):
        with patch.object(overview, "_render_motif_2d", return_value=None), patch.object(
            overview, "render_smiles_ball_and_stick"
        ) as legacy:
            self.assertIsNone(overview._render_scheme_molecule("CCO", Path("a.png"), (200, 200), "3d"))
            legacy.assert_not_called()

    def test_chemistry_prompt_reserves_reaction_band(self):
        features = {"has_reaction_focus": True, "product_keywords": ["ethanol"],
                    "_chemistry_decision": {"mode": "reaction"},
                    "_composite_layout": "module-cards-crosscut-sidebar", "_skeleton_is_scheme": True}
        with patch.object(overview, "is_chemistry_context", return_value=True):
            prompt = overview._build_skeleton_description(features)
        self.assertIn("horizontal reaction band", prompt)
        self.assertNotIn("3D", prompt)


if __name__ == "__main__":
    unittest.main()
