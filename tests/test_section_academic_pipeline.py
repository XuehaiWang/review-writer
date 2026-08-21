from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


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

    def test_direct_provider_json_parser_accepts_extra_top_level_data(self) -> None:
        parsed = PIPELINE.parse_json_object(
            '{"paragraphs": [], "overview": "complete"}\n'
            '{"relay_diagnostic": "ignored"}',
            required_list="paragraphs",
        )

        self.assertEqual("complete", parsed["overview"])
        self.assertNotIn("relay_diagnostic", parsed)


if __name__ == "__main__":
    unittest.main()
