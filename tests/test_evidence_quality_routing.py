from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace

from review_writer_api.domain_services.drafts import DraftsService


class EvidenceQualityRoutingTests(unittest.TestCase):
    def test_source_failure_returns_to_affected_section(self) -> None:
        issues, routing = DraftsService._quality_routing(
            {
                "issues": [{"issue_id": "I-1", "paragraph_id": "S02-p1"}],
                "paragraph_scores": [
                    {
                        "paragraph_id": "S02-p1",
                        "source_check_status": "unsupported",
                        "failed_dimensions": ["factual_support"],
                    }
                ],
            },
            {
                "section_index": {
                    "sections": [
                        {
                            "section_id": "S02",
                            "paragraphs": [{"paragraph_id": "S02-p1"}],
                        }
                    ]
                },
                "section_evidence": {
                    "sections": [
                        {
                            "section_id": "S02",
                            "status": "partial",
                            "unresolved_primary_papers": ["P002"],
                        }
                    ]
                },
            },
        )

        # Section-evidence repair is now orchestrated from the Draft page so
        # users do not have to jump backward and repair packages manually.
        self.assertEqual("draft", routing["recommended_return_stage"])
        self.assertEqual("S02", issues[0]["section_id"])
        self.assertEqual(["P002"], issues[0]["unresolved_primary_papers"])

    def test_literature_coverage_failure_returns_to_discovery(self) -> None:
        issues, routing = DraftsService._quality_routing(
            {
                "issues": [{"issue_id": "I-1", "paragraph_id": "S01-p1"}],
                "paragraph_scores": [
                    {
                        "paragraph_id": "S01-p1",
                        "failed_dimensions": ["literature_coverage"],
                    }
                ],
            },
            {"section_index": {}, "section_evidence": {}},
        )

        self.assertEqual("discovery", routing["recommended_return_stage"])
        self.assertEqual("discovery", issues[0]["recommended_return_stage"])

    def test_manual_claim_requires_exact_source_verification(self) -> None:
        current = SimpleNamespace(
            metadata={"unverified_manual_paragraph_ids": ["S01-p1", "S01-p2"]}
        )
        review = DraftsService._manual_claim_review(
            current,
            {
                "paragraph_scores": [
                    {
                        "paragraph_id": "S01-p1",
                        "source_check_status": "verified",
                        "source_evidence_refs": ["P001:chunk-1"],
                    },
                    {
                        "paragraph_id": "S01-p2",
                        "source_check_status": "partially_supported",
                        "source_evidence_refs": ["P002:chunk-2"],
                    },
                ]
            },
        )

        self.assertEqual(["S01-p1"], review["verified_manual_paragraph_ids"])
        self.assertEqual(["S01-p2"], review["unverified_manual_paragraph_ids"])
        self.assertTrue(review["warning_required"])

    def test_automatic_synthesis_removes_only_unverified_manual_prose(self) -> None:
        service = object.__new__(DraftsService)
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {
                "source_draft_artifact_id": "draft-1",
                "unverified_manual_paragraph_ids": ["S01-p2"],
            },
            SimpleNamespace(id="quality-1"),
        )
        text = (
            "# Review\n\nSupported paragraph.\n\n"
            "<!-- paragraph_id: S01-p1 -->\n\n"
            "Manual unsupported paragraph.\n\n"
            "<!-- paragraph_id: S01-p2 -->\n"
        )

        payload = service.automatic_synthesis_source(
            None, "project-1", text=text, draft=SimpleNamespace(id="draft-1")
        )

        self.assertIn("Supported paragraph.", payload["draft_text"])
        self.assertNotIn("Manual unsupported paragraph.", payload["draft_text"])
        self.assertEqual(["S01-p2"], payload["excluded_manual_paragraph_ids"])

    def test_inserted_figure_metadata_links_paragraph_claims_and_evidence(self) -> None:
        markdown = DraftsService._assemble_markdown(
            "Review",
            {
                "sections": [
                    {
                        "section_id": "S01",
                        "paragraphs": [
                            {
                                "paragraph_id": "S01-p1",
                                "paper_id": "P001",
                                "text": "Supported text.",
                                "claim_realizations": [
                                    {
                                        "claim_id": "S01-p1-C01",
                                        "evidence_refs": [
                                            {
                                                "evidence_id": "EV-001",
                                                "evidence_key": "sha256:one",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
            {
                "figures": [
                    {
                        "figure_id": "P001-F01",
                        "paper_id": "P001",
                        "target_paragraph_id": "S01-p1",
                        "output_artifact_id": "artifact-1",
                        "status": "redrawn",
                    }
                ]
            },
            {"rows": [{"paper_id": "P001", "title": "Paper"}]},
            {
                "evidence_registry": [
                    {
                        "evidence_id": "EV-001",
                        "evidence_key": "sha256:one",
                        "paper_id": "P001",
                        "chunk_id": "chunk-1",
                    }
                ]
            },
        )
        match = re.search(r"<!-- inserted_figure: (\{.*?\}) -->", markdown)
        self.assertIsNotNone(match)
        metadata = json.loads(match.group(1))
        self.assertEqual(["S01-p1-C01"], metadata["claim_ids"])
        self.assertEqual(["EV-001"], metadata["evidence_ids"])
        self.assertEqual(["EV-001"], metadata["figure_evidence_ids"])


if __name__ == "__main__":
    unittest.main()
