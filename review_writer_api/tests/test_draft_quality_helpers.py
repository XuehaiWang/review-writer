from __future__ import annotations

import unittest
from types import SimpleNamespace
import threading

from review_writer_api.domain_services.drafts import (
    DRAFT_DOCUMENT,
    DRAFT_QUALITY,
    DraftsService,
)
from review_writer_core.draft_quality import full_draft_quality_provenance, QUALITY_INPUT_ARTIFACTS


class DraftQualityHelperTests(unittest.TestCase):
    @staticmethod
    def _drafts_service() -> DraftsService:
        service = object.__new__(DraftsService)
        service._write_lock = threading.RLock()
        # These tests isolate proposal transformations. Actual database snapshot
        # checks are covered by test_workflow_input_guards and test_drafts_v1.
        service.validate_task_inputs = lambda _principal, _project, payload: {
            logical_name: str(payload[field] or "")
            for field, logical_name in {
                **QUALITY_INPUT_ARTIFACTS,
                "source_draft_artifact_id": DRAFT_DOCUMENT,
                "source_quality_artifact_id": DRAFT_QUALITY,
            }.items() if field in payload
        }
        return service

    def test_evaluation_payload_reuses_only_exact_full_quality(self) -> None:
        service = self._drafts_service()
        draft_text = (
            "# Review\n\nStable paragraph [1].\n\n"
            "<!-- paragraph_id: S01-p1 -->\n"
        )
        paragraphs = [{"paragraph_id": "S01-p1"}]
        compatibility = {"source_matrix_artifact_id": "matrix-v1"}
        quality = {
            "current": True,
            "source_draft_artifact_id": "draft-v1",
            "score": 90,
            **full_draft_quality_provenance(
                draft_text,
                paragraphs,
                input_artifact_ids=compatibility,
            ),
        }
        payload = {
            "draft_artifact_id": "draft-v1",
            "quality_artifact_id": "quality-v1",
            "revision": 4,
            "first_draft_md": draft_text,
            "paragraphs": paragraphs,
            "freshness": {"upstream_stale": False},
            "quality": quality,
            "optimization_proposals": [],
        }
        service.get = lambda *_args, **_kwargs: payload  # type: ignore[method-assign]
        service.compatibility_payload = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: compatibility
        )

        exact = service.evaluation_payload(
            SimpleNamespace(), "project-1", goal=90
        )
        self.assertTrue(exact["quality_reused"])
        self.assertEqual("steady_state", exact["run_mode"])
        self.assertEqual(quality, exact["baseline_quality"])

        quality["quality_scope"] = "batch_selected_paragraphs"
        migration = service.evaluation_payload(
            SimpleNamespace(), "project-1", goal=90
        )
        self.assertFalse(migration["quality_reused"])
        self.assertEqual("migration", migration["run_mode"])
        self.assertEqual({}, migration["baseline_quality"])

    def test_quality_roots_group_repeated_reference_failures(self) -> None:
        issues = [
            {
                "issue_id": "I-1",
                "paragraph_id": "S01-p1",
                "section_id": "S01",
                "repair_route": "deterministic_reference_rebuild",
                "issue_type": "citation_reference_mapping",
                "auto_repairable": True,
            },
            {
                "issue_id": "I-2",
                "paragraph_id": "S04-p3",
                "section_id": "S04",
                "repair_route": "deterministic_reference_rebuild",
                "issue_type": "citation_reference_mapping",
                "auto_repairable": True,
            },
        ]
        roots, tasks = DraftsService._quality_root_causes(issues)

        self.assertEqual(1, len(roots))
        self.assertEqual(["I-1", "I-2"], roots[0]["issue_ids"])
        self.assertEqual(roots[0]["root_cause_id"], issues[0]["root_cause_id"])
        self.assertEqual(1, len(tasks))
        self.assertEqual("queued", tasks[0]["status"])

    def test_different_missing_claims_are_not_hidden_in_one_root(self):
        issues = [{"issue_id": f"I{index}", "paragraph_id": "S1-p1", "repair_route": "targeted_evidence_then_paragraph_rewrite",
                   "claim_ids": [f"claim{index}"], "paper_ids": ["P1"]} for index in range(2)]
        roots, _tasks = DraftsService._quality_root_causes(issues)
        self.assertEqual(2, len(roots))
        self.assertEqual("unchanged", DraftsService._repair_summary({"root_causes": roots}, roots)["repair_status"])

    def test_quality_roots_keep_coverage_expansion_as_user_decision(self) -> None:
        issues = [
            {
                "issue_id": "C-1",
                "repair_route": "manual_online_retrieval_decision",
                "issue_type": "literature_coverage_gap",
                "auto_repairable": False,
            }
        ]
        roots, tasks = DraftsService._quality_root_causes(issues)

        self.assertTrue(roots[0]["requires_user_decision"])
        self.assertEqual("requires_user_input", tasks[0]["status"])

    def test_evidence_roots_are_tracked_per_paragraph_not_hidden_by_section(self) -> None:
        issues = [
            {
                "issue_id": "E-1",
                "paragraph_id": "S02-p1",
                "section_id": "S02",
                "repair_route": "targeted_evidence_then_paragraph_rewrite",
                "issue_type": "claim_evidence_gap",
                "auto_repairable": True,
            },
            {
                "issue_id": "E-2",
                "paragraph_id": "S02-p3",
                "section_id": "S02",
                "repair_route": "targeted_evidence_then_paragraph_rewrite",
                "issue_type": "claim_evidence_gap",
                "auto_repairable": True,
            },
        ]

        roots, tasks = DraftsService._quality_root_causes(issues)

        self.assertEqual(2, len(roots))
        self.assertEqual({"S02-p1", "S02-p3"}, {root["scope"] for root in roots})
        self.assertEqual(2, len(tasks))













    def test_targeted_source_passage_repairs_section_evidence_summary(self) -> None:
        repaired, summary, dispositions = DraftsService._repair_evidence_package(
            {
                "matrix": {"rows": [{"paper_id": "P001"}]},
                "section_index": {
                    "sections": [
                        {
                            "section_id": "S01",
                            "paragraphs": [
                                {
                                    "paragraph_id": "S01-p1",
                                    "cited_paper_ids": ["P001"],
                                    "claim_realizations": [
                                        {"claim_id": "C01", "question_id": "scope"}
                                    ],
                                }
                            ],
                        }
                    ]
                },
                "section_evidence": {
                    "schema_version": 2,
                    "evidence_registry": [],
                    "sections": [
                        {
                            "section_id": "S01",
                            "status": "insufficient_evidence",
                            "hits": [],
                            "primary_paper_states": [
                                {
                                    "paper_id": "P001",
                                    "status": "unresolved",
                                    "diagnostic": "query_miss",
                                }
                            ],
                            "query_plans": [
                                {
                                    "question_id": "scope",
                                    "status": "insufficient",
                                    "coverage_policy": "evidence_bearing",
                                    "matched_papers": [],
                                    "matched_primary_papers": [],
                                }
                            ],
                            "corpus_gap_questions": ["scope"],
                        }
                    ],
                },
            },
            {
                "paragraph_scores": [
                    {
                        "paragraph_id": "S01-p1",
                        "source_check_status": "verified",
                        "source_evidence_refs": ["P001:p2:b3"],
                    }
                ],
                "source_check": {
                    "entries": [
                        {
                            "paragraph_id": "S01-p1",
                            "papers": [
                                {
                                    "paper_id": "P001",
                                    "passages": [
                                        {
                                            "ref": "P001:p2:b3",
                                            "page": 2,
                                            "text": "Direct source passage.",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        )

        section = repaired["sections"][0]
        hit = section["hits"][0]
        self.assertEqual(1, summary["added_evidence_count"])
        self.assertEqual({}, dispositions)
        self.assertEqual("P001:p2:b3", hit["chunk_id"])
        self.assertTrue(hit["claim_eligible"])
        self.assertEqual("ready", section["status"])
        self.assertEqual([], section["unresolved_primary_papers"])
        self.assertEqual("sufficient", section["query_plans"][0]["status"])
        self.assertEqual(
            ["P001"], section["query_plans"][0]["expected_primary_papers"]
        )
        self.assertEqual([], section["corpus_gap_questions"])

    def test_validated_targeted_binding_is_promoted_to_same_paper_matrix(self) -> None:
        job_payload = {
            "matrix": {
                "rows": [
                    {
                        "paper_id": "P001",
                        "title": "Study",
                        "scientific_facts": [],
                        "topic_classifications": [{"axis_id": "substrate"}],
                    }
                ]
            },
            "section_index": {
                "sections": [
                    {
                        "section_id": "S01",
                        "paragraphs": [
                            {
                                "paragraph_id": "S01-p1",
                                "cited_paper_ids": ["P001"],
                                "claim_realizations": [
                                    {"claim_id": "C01", "question_id": "scope"}
                                ],
                            }
                        ],
                    }
                ]
            },
            "section_evidence": {
                "schema_version": 2,
                "evidence_registry": [],
                "sections": [
                    {"section_id": "S01", "hits": [], "query_plans": []}
                ],
            },
        }
        built = {
            "paragraph_scores": [
                {
                    "paragraph_id": "S01-p1",
                    "source_check_status": "verified",
                    "source_evidence_refs": ["P001:p2:b3"],
                    "claim_fact_bindings": [
                        {
                            "paper_id": "P001",
                            "source_ref": "P001:p2:b3",
                            "support_excerpt": "The reaction afforded 3aa in 82% yield.",
                            "fact_type": "outcome",
                            "subject": "the reaction",
                            "predicate": "afforded",
                            "value": "3aa in 82% yield",
                            "normalized_value": "82%",
                            "qualifiers": {},
                            "confidence": 0.96,
                        }
                    ],
                }
            ],
            "source_check": {
                "entries": [
                    {
                        "paragraph_id": "S01-p1",
                        "papers": [
                            {
                                "paper_id": "P001",
                                "passages": [
                                    {
                                        "ref": "P001:p2:b3",
                                        "page": 2,
                                        "text": "The reaction afforded 3aa in 82% yield.",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        }

        _evidence, repair, _dispositions = DraftsService._repair_evidence_package(
            job_payload, built
        )
        candidate, applied = DraftsService._matrix_with_promoted_facts(
            job_payload["matrix"], repair
        )

        self.assertEqual(1, repair["promoted_fact_count"])
        self.assertEqual(1, len(applied))
        self.assertEqual("P001", applied[0]["paper_id"])
        facts = candidate["rows"][0]["scientific_facts"]
        self.assertEqual("P001", facts[0]["paper_id"])
        self.assertEqual("claim_targeted_fact", facts[0]["field_id"])
        self.assertEqual(
            [{"axis_id": "substrate"}],
            candidate["rows"][0]["topic_classifications"],
        )
        self.assertTrue(applied[0]["fact_id"].startswith("MF-"))


if __name__ == "__main__":
    unittest.main()
