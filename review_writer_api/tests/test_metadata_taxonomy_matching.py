from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "review-metadata-prep" / "scripts" / "prepare_metadata.py"
SPEC = importlib.util.spec_from_file_location("review_metadata_prepare_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prepare_metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_metadata)
VALIDATE_SCRIPT = (
    ROOT / "skills" / "review-metadata-prep" / "scripts" / "validate_metadata.py"
)
VALIDATE_SPEC = importlib.util.spec_from_file_location(
    "review_metadata_validate_script", VALIDATE_SCRIPT
)
assert VALIDATE_SPEC is not None and VALIDATE_SPEC.loader is not None
validate_metadata = importlib.util.module_from_spec(VALIDATE_SPEC)
VALIDATE_SPEC.loader.exec_module(validate_metadata)


class MetadataPreparationTests(unittest.TestCase):
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

    def test_llm_payload_does_not_request_or_expose_structured_tags(self) -> None:
        metadata, _blocks, _markdown, _registry = prepare_metadata.build_metadata(
            "P001",
            {"slug": "paper", "pdf_name": "paper.pdf"},
            None,
            None,
            None,
            None,
            ROOT,
        )
        payload = prepare_metadata.build_llm_payload(
            metadata,
            [],
            "",
            "Extract bibliographic metadata only.",
            "test-model",
        )

        schema = payload["text"]["format"]["schema"]
        self.assertNotIn("structured_tags", schema["required"])
        self.assertNotIn("structured_tags", schema["properties"])
        user_content = json.loads(payload["input"][1]["content"])
        self.assertNotIn(
            "structured_tags", user_content["rule_extracted_initial_metadata"]
        )
        self.assertNotIn("classification_rules", user_content)

    def test_llm_merge_ignores_unexpected_automatic_structured_tags(self) -> None:
        metadata, _blocks, _markdown, _registry = prepare_metadata.build_metadata(
            "P001",
            {"slug": "paper", "pdf_name": "paper.pdf"},
            None,
            None,
            None,
            None,
            ROOT,
        )
        automatic_tags = {
            key: "automatically classified"
            for key in prepare_metadata.STRUCTURED_TAG_KEYS
        }

        prepare_metadata.merge_llm(
            metadata,
            {
                "structured_tags": {
                    "value": automatic_tags,
                    "source": "llm",
                    "confidence": 1.0,
                    "human_checked": False,
                },
                "warnings": [],
            },
        )

        self.assertEqual(
            {"not specified"}, set(metadata["structured_tags"]["value"].values())
        )
        self.assertEqual(
            "project_neutral_unverified", metadata["structured_tags"]["source"]
        )

    def test_validation_rejects_persisted_unverified_tag_values(self) -> None:
        metadata, _blocks, _markdown, _registry = prepare_metadata.build_metadata(
            "P001",
            {"slug": "paper", "pdf_name": "paper.pdf"},
            None,
            None,
            None,
            None,
            ROOT,
        )
        metadata["structured_tags"]["value"]["product"] = "automatic product"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "P001.metadata.json"
            prepare_metadata.write_json(path, metadata)
            report = validate_metadata.validate_one(path, {})

        self.assertIn(
            "unverified_structured_tag_must_be_neutral_product",
            report["blocking_issues"],
        )


if __name__ == "__main__":
    unittest.main()
