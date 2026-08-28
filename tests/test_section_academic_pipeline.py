from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from review_writer_core.writing_contracts import derive_writing_scope_contract


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

    def test_writing_scope_is_stable_and_binds_both_model_stages(self) -> None:
        scope = {
            "schema_version": 1,
            "topic": "Selective synthesis",
            "target_question": "Which strategies remain transferable?",
            "review_objective": "Build a bounded evidence map.",
            "target_readers": ["Researchers", "Graduate readers"],
            "required_reader_outcomes": ["Compare the supported strategies"],
            "time_span": {"from": 2015, "to": 2026, "basis": "user_topic"},
            "core_window": {"from": 2018, "to": 2026, "basis": "confirmed"},
            "coverage_mode": "local_bounded",
            "coverage_basis": {
                "kind": "selected_matrix",
                "selected_paper_count": 12,
                "global_literature_coverage_claimed": False,
            },
            "inclusion_criteria": ["Directly addresses the review question"],
            "exclusion_criteria": ["Outside the confirmed topic"],
            "evidence_availability_policy": "Do not exceed the available source.",
            "primary_navigation_axis": "reaction_strategy",
            "secondary_axes": ["catalyst_or_method"],
        }
        contract = derive_writing_scope_contract(scope)
        reordered = derive_writing_scope_contract(dict(reversed(list(scope.items()))))

        self.assertEqual(contract["fingerprint"], reordered["fingerprint"])
        self.assertEqual("active", contract["status"])
        planning = PIPELINE.writing_scope_prompt_block(contract, stage="planning")
        drafting = PIPELINE.writing_scope_prompt_block(contract, stage="drafting")
        for prompt in (planning, drafting):
            self.assertIn("Which strategies remain transferable?", prompt)
            self.assertIn("reaction_strategy", prompt)
            self.assertIn("local_bounded", prompt)
            self.assertIn("Directly addresses the review question", prompt)
        self.assertIn("distinct responsibility", planning)
        self.assertIn("Do not broaden the time window", drafting)

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

    def test_matrix_comparison_table_is_always_an_object(self) -> None:
        rows = {
            "P001": {
                "scientific_facts": [
                    {
                        "field_id": "yield",
                        "value": "81%",
                        "evidence_refs": [{"evidence_key": "sha256:a"}],
                    }
                ]
            },
            "P002": {"scientific_facts": None},
        }

        table = PIPELINE.build_matrix_comparison_table(
            "S02", ["P001", "P002"], rows
        )

        self.assertIsInstance(table, dict)
        self.assertEqual("S02", table["section_id"])
        self.assertEqual(["yield"], table["single_source_fields"])
        self.assertEqual(
            [{"paper_id": "P002", "field_id": "yield", "status": "unresolved"}],
            table["missing_cells"],
        )

    def test_matrix_comparison_table_marks_shared_fields_comparable(self) -> None:
        rows = {
            paper_id: {
                "scientific_facts": [
                    {
                        "field_id": "yield",
                        "value": value,
                        "evidence_refs": [{"evidence_key": evidence_key}],
                    }
                ]
            }
            for paper_id, value, evidence_key in (
                ("P001", "81%", "sha256:a"),
                ("P002", "76%", "sha256:b"),
            )
        }

        table = PIPELINE.build_matrix_comparison_table(
            "S02", ["P001", "P002"], rows
        )

        self.assertEqual(["yield"], table["comparable_fields"])
        self.assertEqual([], table["missing_cells"])
        self.assertEqual(2, len(table["cells"]))

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

    def test_realized_scientific_anchors_must_exist_in_the_claim_chunks(self) -> None:
        evidence = [
            {
                "evidence_id": "EV-A",
                "evidence_key": "sha256:a",
                "paper_id": "P001",
                "chunk_id": "C001",
                "content": "CuBr2 afforded the product in 81% yield.",
            }
        ]
        proposed = {
            "overview_intent": "Summarize the source.",
            "synthesis_summary": "The source reports a bounded result.",
            "components": [],
            "paragraphs": [
                {
                    "theme": "Reported outcome",
                    "argument_role": "example",
                    "objective": "Report the outcome.",
                    "reader_takeaway": "A result was reported.",
                    "positive_synthesis": "The source supports a bounded result.",
                    "paper_ids": ["P001"],
                    "claims": [
                        {
                            "claim": "CuBr2 afforded the product in 81% yield.",
                            "claim_kind": "reported_finding",
                            "epistemic_status": "direct_source_report",
                            "support_status": "supported",
                            "citation_group": ["P001"],
                            "evidence_keys": ["sha256:a"],
                            "evidence_ceiling": "Do not change the reported value.",
                        }
                    ],
                }
            ],
        }
        _synthesis, writing = PIPELINE.normalize_section_plan(
            section_id="S02",
            role="body",
            primary=["P001"],
            supporting=[],
            allowed=["P001"],
            evidence=evidence,
            retrieval_mode="lexical",
            generated=proposed,
            synthesis_requirements=[],
        )
        claim_id = writing["claims"][0]["claim_id"]

        with self.assertRaisesRegex(
            RuntimeError,
            "unsupported evidence anchors.*97%",
        ):
            PIPELINE.validate_and_realize_section(
                section_id="S02",
                generated={
                    "overview": "The source reports a bounded result.",
                    "paragraphs": [
                        {
                            "paragraph_id": "S02-p1",
                            "claim_realizations": [
                                {
                                    "claim_id": claim_id,
                                    "text": "CuBr2 afforded the product in 97% yield.",
                                }
                            ],
                        }
                    ],
                },
                writing_section=writing,
                evidence=evidence,
                citation_map={"P001": 1},
            )

    def test_safe_evidence_fallback_omits_an_unsupported_planned_value(self) -> None:
        evidence = [
            {
                "evidence_id": "EV-A",
                "evidence_key": "sha256:a",
                "paper_id": "P001",
                "chunk_id": "C001",
                "content": "The cited experiment afforded the product in 81% yield.",
            }
        ]
        writing = {
            "overview_intent": "Summarize the cited experiment.",
            "paragraphs": [
                {
                    "paragraph_id": "S02-p1",
                    "claim_ids": ["S02-p1-C01"],
                    "positive_synthesis": "A bounded result was reported.",
                }
            ],
            "claims": [
                {
                    "claim_id": "S02-p1-C01",
                    "paragraph_id": "S02-p1",
                    "claim": "The product was obtained in 97% yield.",
                    "allowed_assertion": "The product was obtained in 97% yield.",
                    "claim_kind": "reported_finding",
                    "citation_group": ["P001"],
                    "evidence_refs": [
                        {
                            "evidence_id": "EV-A",
                            "evidence_key": "sha256:a",
                            "relationship": "supports",
                        }
                    ],
                    "support_status": "supported",
                }
            ],
        }

        fallback = PIPELINE.build_safe_evidence_fallback(
            writing_section=writing,
            evidence=evidence,
        )

        self.assertNotIn("97%", json.dumps(fallback))
        overview, paragraphs, _validations, _reviews = PIPELINE.validate_and_realize_section(
            section_id="S02",
            generated=fallback,
            writing_section=writing,
            evidence=evidence,
            citation_map={"P001": 1},
        )
        self.assertTrue(overview)
        self.assertIn("[1]", paragraphs[0]["text"])

    def test_legacy_prefix_fallback_requires_explicit_authorization(self) -> None:
        self.assertEqual(
            "insufficient_evidence",
            PIPELINE.effective_retrieval_mode(
                {"retrieval_mode": "fixed_prefix_fallback"}
            ),
        )
        self.assertEqual(
            "fixed_prefix_fallback",
            PIPELINE.effective_retrieval_mode(
                {
                    "retrieval_mode": "fixed_prefix_fallback",
                    "legacy_fallback_authorized": True,
                }
            ),
        )

    def test_planning_evidence_is_compacted_under_a_request_budget(self) -> None:
        evidence = [
            {
                "evidence_key": f"sha256:{index}",
                "paper_id": f"P{index:03d}",
                "chunk_id": f"C{index:03d}",
                "content": "source content " * 2_000,
                "claim_eligible": True,
            }
            for index in range(50)
        ]

        compact, report = PIPELINE.bounded_evidence_payload(
            evidence,
            char_budget=20_000,
        )

        self.assertLessEqual(report["content_characters"], 20_000)
        self.assertEqual(50, len(compact))
        self.assertTrue(all(row["evidence_key"] for row in compact))

    def test_direct_provider_json_parser_accepts_extra_top_level_data(self) -> None:
        parsed = PIPELINE.parse_json_object(
            '{"paragraphs": [], "overview": "complete"}\n'
            '{"relay_diagnostic": "ignored"}',
            required_list="paragraphs",
        )

        self.assertEqual("complete", parsed["overview"])
        self.assertNotIn("relay_diagnostic", parsed)

    def test_conclusion_receives_validated_body_synthesis_and_evidence_keys(self) -> None:
        context, keys = PIPELINE.prior_body_synthesis_context(
            {
                "S02": {
                    "section_id": "S02",
                    "title": "Defined precursor class",
                    "section_role": "body",
                    "section_thesis": "Compare the bounded body evidence.",
                },
                "S03": {"section_id": "S03", "section_role": "conclusion"},
            },
            [{"section_id": "S02", "summary": "A bounded body conclusion."}],
            [
                {
                    "section_id": "S02",
                    "section_role": "body",
                    "claims": [
                        {
                            "claim": "The studies support a bounded difference.",
                            "claim_kind": "cross_study_comparison",
                            "support_status": "supported",
                            "citation_group": ["P001", "P002"],
                            "evidence_refs": [
                                {"evidence_key": "sha256:a", "relationship": "supports"},
                                {"evidence_key": "sha256:b", "relationship": "supports"},
                            ],
                        }
                    ],
                }
            ],
        )

        self.assertEqual(["S02"], [item["section_id"] for item in context])
        self.assertEqual({"sha256:a", "sha256:b"}, keys)
        self.assertEqual(
            "A bounded body conclusion.", context[0]["validated_synthesis_summary"]
        )


if __name__ == "__main__":
    unittest.main()
