from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "review-reference-outline-template" / "scripts" / "analyze_reference_review.py"


def load_module():
    spec = importlib.util.spec_from_file_location("reference_outline_ai_check_module", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load reference-outline analysis script.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReferenceOutlineAiChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_style_prompt_explicitly_forbids_source_content_reuse(self) -> None:
        prompt = self.module.build_style_prompt(
            "reference.pdf",
            "Introduction\nA source-specific catalytic claim appears here. " * 12,
            [{"level": 1, "title": "Source Specific Catalytic Classification"}],
            {"heading_depth": 1},
        )
        self.assertIn("STRICT CONTENT FIREWALL", prompt)
        self.assertIn("Do not output source section titles", prompt)

    def test_transfer_prompt_never_receives_reference_text_or_headings(self) -> None:
        source_phrase = "Source Specific Catalytic Classification"
        profile = {
            "organization_pattern": "taxonomy followed by comparison",
            "heading_conventions": {},
        }
        prompt = self.module.build_transfer_prompt(
            "Target topic",
            [{"paper_id": "P001", "title": "Target Matrix Paper"}],
            profile,
        )
        self.assertNotIn(source_phrase, prompt)
        self.assertIn("reference review text and headings are intentionally absent", prompt)
        self.assertIn("every title and every future subsection label", prompt)

    def test_style_profile_with_copied_source_heading_is_sanitized(self) -> None:
        profile = {
            name: "generic"
            for name in self.module.STYLE_SCHEMA["required"]
        }
        profile["forbidden_content_check"] = {
            "contains_source_topic_content": False,
            "notes": "Source Specific Catalytic Classification was reused.",
        }
        sanitized = self.module.validate_style_profile(
            profile,
            [{"level": 1, "title": "Source Specific Catalytic Classification"}],
        )
        serialized = json.dumps(sanitized, ensure_ascii=False)
        self.assertNotIn("Source Specific Catalytic Classification", serialized)
        self.assertIn("source wording is excluded", sanitized["forbidden_content_check"]["notes"])

    def test_style_extraction_retries_a_reported_content_leak(self) -> None:
        leaked = {
            name: "generic"
            for name in self.module.STYLE_SCHEMA["required"]
        }
        leaked["forbidden_content_check"] = {
            "contains_source_topic_content": True,
            "notes": "The profile may contain source content.",
        }
        clean = dict(leaked)
        clean["forbidden_content_check"] = {
            "contains_source_topic_content": False,
            "notes": "Only abstract conventions remain.",
        }
        with patch.object(
            self.module,
            "call_json_model",
            side_effect=[leaked, clean],
        ) as model_call:
            profile, retry_count, replacement_count = self.module.extract_style_profile(
                "style prompt",
                [{"level": 1, "title": "Source Specific Catalytic Classification"}],
                {"model": "test-model"},
            )
        self.assertEqual(model_call.call_count, 2)
        self.assertEqual(retry_count, 1)
        self.assertEqual(replacement_count, 0)
        self.assertFalse(profile["forbidden_content_check"]["contains_source_topic_content"])

    def test_matrix_papers_are_assigned_exactly_once(self) -> None:
        sections = self.module.clean_outline_sections(
            [
                {
                    "title": "Opening",
                    "section_role": "introduction",
                    "purpose": "Frame the review.",
                    "assigned_paper_ids": [],
                },
                {
                    "title": "Target class A",
                    "section_role": "body",
                    "purpose": "Compare target evidence.",
                    "assigned_paper_ids": ["P001", "P002"],
                },
                {
                    "title": "Target class B",
                    "section_role": "body",
                    "purpose": "Compare another target category.",
                    "assigned_paper_ids": ["P002", "UNKNOWN"],
                },
                {
                    "title": "Closing",
                    "section_role": "conclusion",
                    "purpose": "Synthesize comparisons.",
                    "assigned_paper_ids": [],
                },
            ],
            ["P001", "P002", "P003"],
        )
        assigned = [
            paper_id
            for section in sections
            for paper_id in section["assigned_paper_ids"]
        ]
        self.assertEqual(sorted(assigned), ["P001", "P002", "P003"])

    def test_outline_transfer_retries_source_subject_headings(self) -> None:
        leaked = {
            "sections": [
                {"title": "Introduction", "section_role": "introduction", "purpose": "Frame scope.", "assigned_paper_ids": []},
                {"title": "Graph Neural Networks", "section_role": "body", "purpose": "Organize evidence.", "assigned_paper_ids": ["P001"]},
                {"title": "Conclusion", "section_role": "conclusion", "purpose": "Close scope.", "assigned_paper_ids": []},
            ],
            "transfer_notes": "",
        }
        grounded = {
            "sections": [
                {"title": "Introduction", "section_role": "introduction", "purpose": "Frame scope.", "assigned_paper_ids": []},
                {"title": "Propargylic Precursors for Axial-Chiral Allenes", "section_role": "body", "purpose": "Organize evidence.", "assigned_paper_ids": ["P001"]},
                {"title": "Conclusion", "section_role": "conclusion", "purpose": "Close scope.", "assigned_paper_ids": []},
            ],
            "transfer_notes": "",
        }
        papers = [{"paper_id": "P001", "title": "Axial-chiral allene synthesis from propargylic precursors", "keywords": [], "matrix_summary": ""}]
        with patch.object(self.module, "call_json_model", side_effect=[leaked, grounded]) as model_call:
            transfer, retry_count = self.module.extract_outline_transfer(
                "transfer prompt",
                [{"level": 1, "title": "Graph Neural Networks"}],
                "Axial-chiral allene synthesis",
                papers,
                {"model": "test-model"},
            )
        self.assertEqual(model_call.call_count, 2)
        self.assertEqual(retry_count, 1)
        self.assertEqual(transfer["sections"][1]["title"], "Propargylic Precursors for Axial-Chiral Allenes")

    def test_toc_page_leaders_are_removed_from_source_headings(self) -> None:
        self.assertEqual(
            self.module.clean_heading("Graph Neural Networks ........................ 18"),
            "Graph Neural Networks",
        )

    def test_candidate_markdown_declares_matrix_only_content(self) -> None:
        sections = self.module.clean_outline_sections(
            [
                {"title": "Current Matrix category", "section_role": "body", "purpose": "Compare current evidence.", "assigned_paper_ids": ["P001"]},
            ],
            ["P001"],
        )
        markdown = self.module.candidate_markdown(sections, 1)
        self.assertIn("Scientific content source: current literature Matrix only.", markdown)
        self.assertIn("All heading and subsection semantics source: current topic and Matrix only.", markdown)
        self.assertIn("Assigned papers: P001.", markdown)
        self.assertNotIn("reference.pdf", markdown)

    def test_main_runs_two_isolated_ai_passes_and_writes_candidate(self) -> None:
        style_profile = {
            "organization_pattern": "taxonomy followed by comparison",
            "heading_conventions": {
                "numbering": "decimal",
                "depth": 2,
                "capitalization": "sentence case",
                "syntax": "concise noun phrases",
                "average_words": 4,
            },
            "section_role_sequence": [
                {"role": "opening", "level": 1, "heading_pattern": "[Scope]", "purpose_pattern": "frame scope"},
                {"role": "taxonomy", "level": 1, "heading_pattern": "[Current category]", "purpose_pattern": "compare evidence"},
                {"role": "closing", "level": 1, "heading_pattern": "[Outlook]", "purpose_pattern": "synthesize limits"},
            ],
            "paragraph_conventions": {
                "opening_move": "state the comparison axis",
                "development_move": "group evidence",
                "comparison_move": "contrast categories",
                "closing_move": "state a bounded synthesis",
                "typical_length": "medium",
            },
            "transition_conventions": "explicit comparison transitions",
            "evidence_conventions": "evidence follows the organizing statement",
            "conclusion_conventions": "synthesize limitations before outlook",
            "forbidden_content_check": {
                "contains_source_topic_content": False,
                "notes": "Only abstract conventions are present.",
            },
        }
        transfer = {
            "sections": [
                {"title": "Introduction", "section_role": "introduction", "purpose": "Frame the target scope.", "assigned_paper_ids": []},
                {"title": "Target Matrix category", "section_role": "body", "purpose": "Compare target evidence.", "assigned_paper_ids": ["P001"]},
                {"title": "Conclusion and outlook", "section_role": "conclusion", "purpose": "Synthesize target limitations.", "assigned_paper_ids": []},
            ],
            "transfer_notes": "Applied abstract style only.",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "reference.md"
            source.write_text(
                "# Introduction\n\n" + "This paragraph supplies a writing-style sample without reusable findings. " * 20,
                encoding="utf-8",
            )
            matrix = root / "literature_matrix.json"
            matrix.write_text(
                json.dumps({"review_topic": "Target topic", "rows": [{"paper_id": "P001", "title": "Target Matrix Paper"}]}),
                encoding="utf-8",
            )
            output = root / "candidate.json"
            argv = [
                str(SCRIPT), "--input", str(source), "--matrix", str(matrix), "--output", str(output),
                "--project-id", "test", "--candidate-id", "reference-test",
                "--base-url", "https://example.test/v1", "--api-key", "test-key",
                "--model", "test-model", "--wire-api", "chat-completions",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                self.module,
                "call_json_model",
                side_effect=[style_profile, transfer],
            ) as model_call:
                self.assertEqual(self.module.main(), 0)
            self.assertEqual(model_call.call_count, 2)
            candidate = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(candidate["analysis_mode"], "ai_style_only_transfer_v2")
            self.assertEqual(candidate["content_source"], "current_matrix_only")
            self.assertFalse(candidate["reference_content_reused"])
            self.assertEqual(
                candidate["content_firewall"]["all_heading_levels_content_source"],
                "current_matrix_only",
            )
            self.assertNotIn("heading_hierarchy", candidate)


if __name__ == "__main__":
    unittest.main()
