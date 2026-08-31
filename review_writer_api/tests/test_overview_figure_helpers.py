from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from review_writer_core.review_titles import (
    build_publication_overview_text,
    overview_text_needs_rewrite,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "skills"
    / "review-figure-style-redraw"
    / "scripts"
    / "generate_overview_figure.py"
)
SPEC = importlib.util.spec_from_file_location("review_overview_figure_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
overview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overview)


class OverviewFigureHelperTests(unittest.TestCase):
    def test_blueprint_body_sections_override_legacy_metal_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            blueprint_dir = project / "01_matrix_outline"
            blueprint_dir.mkdir(parents=True)
            (blueprint_dir / "section_blueprint.json").write_text(
                json.dumps(
                    {
                        "review_topic": "Allene synthesis by substrate class",
                        "classification_basis": {
                            "primary_axis": "substrate",
                            "overview_axis": "substrate",
                        },
                        "sections": [
                            {
                                "section_id": "S02",
                                "section_role": "body",
                                "title": "Propargylic alcohol substrates",
                            },
                            {
                                "section_id": "S03",
                                "section_role": "body",
                                "title": "Terminal alkyne substrates",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            features = overview.extract_review_features(project)

        contract = features["overview_content_contract"]
        self.assertEqual("substrate", contract["primary_axis"])
        self.assertEqual(
            ["Propargylic alcohol", "Terminal alkyne"], contract["modules"]
        )
        self.assertNotIn("Fe", contract["approved_labels"])

    def test_overview_title_rewrites_instruction_topic_as_academic_title(self) -> None:
        topic = (
            'Please write a review on the topic “allenation-of-terminal-alkynes (ATA)”, '
            'focusing on the development of terminal alkyne allenation with different '
            'substrates. Organize the review by reaction type and catalytic/promoting '
            'system, and separately discuss racemic ATA and enantioselective ATA.'
        )

        title = overview.build_overview_display_title({"review_title": topic})

        self.assertEqual(
            "Allenation of Terminal Alkynes (ATA): Reaction Classes and Catalytic Strategies",
            title,
        )
        self.assertNotIn("Please write", title)
        self.assertLessEqual(len(title), 110)

    def test_manuscript_title_wins_over_search_instruction(self) -> None:
        title = overview.build_overview_display_title(
            {
                "review_title": "Please write a review about a long search query",
                "manuscript_title": "Terminal Alkyne Allenation: Catalysis and Selectivity",
            }
        )

        self.assertEqual(
            "Terminal Alkyne Allenation: Catalysis and Selectivity", title
        )

    def test_image_prompt_never_contains_the_raw_search_instruction(self) -> None:
        raw_topic = (
            "Please write a review on terminal alkyne allenation, focusing on "
            "reaction types and catalytic systems."
        )
        features = {
            "review_title": raw_topic,
            "display_title": overview.build_overview_display_title(
                {"review_title": raw_topic}
            ),
            "taxonomy_profile": "allene",
            "group_by": ["reaction_type"],
            "metal_categories": ["Cu", "Zn", "Cd", "Ti", "Other"],
            "classification_rule": "By reaction type",
            "product_keywords": ["allenes"],
            "substrate_keywords": ["terminal alkynes"],
            "catalyst_keywords": ["Cu", "Zn", "Cd", "Ti"],
            "has_chirality": True,
            "has_reaction_focus": True,
            "time_window": "recent years",
            "_outline_text": "",
            "_project_dir": None,
        }

        prompt = overview.build_adapted_prompt(
            {"prompt": "", "layout_type": "module-cards-crosscut-sidebar"},
            features,
            composite_mode=True,
        )

        self.assertNotIn(raw_topic, prompt)
        self.assertNotIn("Original topic", prompt)
        self.assertIn(features["display_title"], prompt)

    def test_publication_caption_never_exposes_topic_or_layout_contract(self) -> None:
        raw_topic = (
            'Please write a review on the topic “allenation-of-terminal-alkynes (ATA)”, '
            'focusing on different substrates to access mono-, 1,3-di-, and '
            'trisubstituted allenes. Organize the review by reaction type and '
            'catalytic/promoting system, and separately discuss racemic ATA and EATA.'
        )

        caption = build_publication_overview_text(
            raw_topic,
            group_by=["reaction_type"],
            classification_rule="By reaction type",
            has_chirality=True,
            has_reaction_focus=True,
        )
        rendered = " ".join(
            [caption["title"], caption["subtitle"], *caption["labels"]]
        )

        self.assertNotIn("Please write", rendered)
        self.assertNotIn("reaction_type", rendered)
        self.assertNotIn("module-cards-crosscut-sidebar", rendered)
        self.assertIn("reaction class", caption["subtitle"])
        self.assertIn("catalytic or promoting system", caption["subtitle"])
        self.assertIn("stereochemical control", caption["subtitle"])

    def test_legacy_overview_caption_with_internal_residue_is_rewritten(self) -> None:
        raw_topic = "Please write a review on catalytic transformations and their scope."
        legacy = {
            "title": raw_topic,
            "subtitle": "module-cards-crosscut-sidebar",
            "labels": ["Cu", "reaction_type"],
        }

        self.assertTrue(overview_text_needs_rewrite(legacy, raw_topic))

    def test_footer_whitespace_is_not_a_structure_panel(self) -> None:
        self.assertFalse(
            overview._looks_like_structure_panel((724, 920, 1024, 1024), 1024, 1024)
        )
        self.assertTrue(
            overview._looks_like_structure_panel((28, 120, 790, 370), 1024, 1024)
        )

    def test_panel_refinement_is_bounded_to_requested_error_budget(self) -> None:
        from PIL import Image

        image = Image.new("RGB", (120, 120), "white")
        refined = overview._refine_panel_box(
            image,
            (40, 40, 80, 80),
            max_dx=12,
            max_dy=12,
            whiteness_threshold=0.85,
        )
        self.assertEqual((28, 28, 92, 92), refined)

    def test_skeleton_bbox_discards_a_tiny_distant_raster_speck(self) -> None:
        from PIL import Image, ImageDraw

        mask = Image.new("L", (100, 100), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((35, 35, 65, 65), fill=255)
        draw.rectangle((2, 2, 4, 4), fill=255)

        self.assertEqual((35, 35, 66, 66), overview._skeleton_content_bbox(mask))

    def test_symbol_allowlist_is_derived_from_project_categories(self) -> None:
        symbols = overview._approved_figure_symbols(
            {
                "taxonomy_profile": "chemistry_general",
                "metal_categories": ["Cu", "Organocatalysis", "Ni catalyst", "Fe"],
            }
        )

        self.assertEqual(["Cu", "Fe", "ee", "R1", "R2", "R3", "R4"], symbols)
        self.assertNotIn("Pd", symbols)
        self.assertNotIn("Ni", symbols)

    def test_category_name_does_not_create_performance_claims(self) -> None:
        self.assertEqual("Au system", overview._derive_strategy("Au"))
        self.assertEqual("—", overview._derive_selectivity("Au"))
        self.assertEqual("—", overview._derive_highlight("Cu"))
        take_home = overview._build_take_home_text(
            {
                "has_chirality": True,
                "has_reaction_focus": True,
                "classification_rule": "By reaction type",
            }
        )
        self.assertNotIn("High enantioselectivity", take_home)
        self.assertNotIn("sustainable", take_home.casefold())

    def test_existing_integrity_fallbacks_and_blueprint_contract_remain(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('skeleton_source = "programmatic_fallback"', source)
        self.assertIn('return True, "", "appended-dock"', source)
        self.assertIn('"overview_axis_contract": {}', source)
        self.assertIn("visible = len(atoms)", source)
        self.assertIn("output_path.stat().st_size", source)


if __name__ == "__main__":
    unittest.main()
