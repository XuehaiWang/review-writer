from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "review-metadata-prep" / "scripts" / "prepare_metadata.py"
SPEC = importlib.util.spec_from_file_location("review_metadata_prepare_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prepare_metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_metadata)


class MetadataTaxonomyMatchingTests(unittest.TestCase):
    def test_h2_article_title_replaces_mineru_p001_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown = Path(temporary) / "p001.md"
            markdown.write_text(
                "## Buta-2,3-dien-1-ol\n\nHongwen Luo and Shengming Ma\n\n"
                "## Procedure\n\nPropargyl alcohol was converted to the product.\n",
                encoding="utf-8",
            )
            metadata, _blocks, _markdown, _registry = prepare_metadata.build_metadata(
                "P001",
                {"slug": "p001", "pdf_name": "v94p0153.pdf"},
                None,
                markdown,
                None,
                None,
                ROOT,
            )

        self.assertEqual("Buta-2,3-dien-1-ol", metadata["title"]["value"])
        self.assertEqual(
            "mineru_markdown_h2_front_matter", metadata["title"]["source"]
        )
        self.assertGreaterEqual(metadata["title"]["confidence"], 0.85)

    def test_title_recovery_skips_organic_syntheses_safety_boilerplate(self) -> None:
        markdown = (
            "## Working with Hazardous Chemicals\n\nSafety boilerplate.\n\n"
            "## Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol\n\n"
            "Juntao Ye and Shengming Ma\n\n## Procedure\n"
        )

        title = prepare_metadata.extract_title([], markdown, "p001")

        self.assertEqual(
            "Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol",
            title["value"],
        )

    def test_title_recovery_normalizes_pdf_typographic_hyphens(self) -> None:
        markdown = (
            "## Preparation of (R)-‐4-‐Cyclohexyl-‐2,3-‐butadien-‐1-‐ol\n\n"
            "## Procedure\n\nThe reaction afforded the allene product.\n"
        )

        title = prepare_metadata.extract_title([], markdown, "p001")

        self.assertEqual(
            "Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol",
            title["value"],
        )

    def test_default_metadata_build_keeps_reusable_tags_project_neutral(self) -> None:
        metadata, _blocks, _markdown, _registry = prepare_metadata.build_metadata(
            "P001",
            {"slug": "paper", "pdf_name": "paper.pdf"},
            None,
            None,
            None,
            None,
            ROOT,
        )

        tag_field = metadata["structured_tags"]
        self.assertEqual("project_neutral_unverified", tag_field["source"])
        self.assertFalse(tag_field["human_checked"])
        self.assertEqual({"not specified"}, set(tag_field["value"].values()))
        self.assertFalse(
            any(
                warning.startswith("structured_tag_not_specified_")
                for warning in metadata["quality"]["warnings"]
            )
        )

    def test_short_metal_alias_does_not_match_inside_an_ordinary_word(self) -> None:
        tags = prepare_metadata.structured_tags_from_classification_rules(
            ROOT,
            "Molecular design and calculations for stereoselective allene synthesis",
            profile="allene",
        )

        self.assertEqual("not specified", tags["catalyst_or_method"])

    def test_explicit_metal_phrase_uses_the_correct_catalyst_label(self) -> None:
        gold = prepare_metadata.structured_tags_from_classification_rules(
            ROOT,
            "Gold-catalyzed synthesis of axially chiral allenes",
            profile="allene",
        )
        nickel = prepare_metadata.structured_tags_from_classification_rules(
            ROOT,
            "A Ni-catalyzed stereoselective allenylation method",
            profile="allene",
        )

        self.assertEqual("gold catalysis", gold["catalyst_or_method"])
        self.assertEqual("nickel catalysis", nickel["catalyst_or_method"])


if __name__ == "__main__":
    unittest.main()
