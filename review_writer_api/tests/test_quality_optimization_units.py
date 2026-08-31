from __future__ import annotations

import unittest
from unittest.mock import patch

from review_writer_core.bibliography_audit import (
    BibliographyResolutionError,
    apply_bibliography_updates,
    audit_bibliography,
    bibliography_field_readiness,
    resolve_bibliography,
)
from review_writer_core.evidence_queries import build_question_query_plans
from review_writer_core.review_fact_readiness import (
    fact_readiness_report,
    negative_claim_eligibility,
    required_fact_roles,
)
from review_writer_core.paper_sources.base import PaperSearchRequest, SourceSearchResult
from review_writer_core.publication_metadata import extract_front_matter_doi
from review_writer_core.publication_voice import publication_voice_issues
from review_writer_core.publication_caption import figure_rights_fields
from review_writer_api.domain_services.final import (
    _figure_argument_findings,
    _normalize_publication_markup,
)
from review_writer_api.domain_services.discovery import discovery_coverage_diagnostics
from review_writer_api.domain_services.sections import SectionsService


class _Connector:
    def __init__(self, name: str, result: SourceSearchResult):
        self.name = name
        self._result = result
        self.requests: list[PaperSearchRequest] = []

    def search(self, request: PaperSearchRequest) -> SourceSearchResult:
        self.requests.append(request)
        return self._result


class QualityOptimizationUnitTests(unittest.TestCase):
    def test_manual_bibliography_resolution_requires_traceable_evidence(self) -> None:
        metadata = {"paper_id": "P001", "title": {"value": "A paper"}}
        with self.assertRaises(BibliographyResolutionError) as raised:
            resolve_bibliography(
                metadata,
                {},
                {
                    "action": "save_manual",
                    "document_type": "journal_article",
                    "fields": {
                        "title": "A paper",
                        "authors": ["A. Author"],
                        "journal": "Journal",
                        "year": 2024,
                    },
                },
            )
        self.assertIn("manual_evidence.location", raised.exception.fields)

    def test_manual_bibliography_resolution_marks_fields_human_checked(self) -> None:
        metadata, audit, outcome = resolve_bibliography(
            {"paper_id": "P001", "title": {"value": "Old title"}},
            {"status": "conflict", "manual_review_status": "not_reviewed"},
            {
                "action": "save_manual",
                "document_type": "journal_article",
                "fields": {
                    "title": "Canonical title",
                    "authors": ["A. Author"],
                    "journal": "Journal",
                    "year": 2024,
                    "doi": "https://doi.org/10.1000/example",
                },
                "manual_evidence": {
                    "evidence_type": "first_page",
                    "location": "PDF page 1",
                    "note": "Publisher header",
                },
            },
        )
        self.assertEqual("resolved", audit["manual_review_status"])
        self.assertEqual("human", audit["resolved_by"])
        self.assertTrue(metadata["title"]["human_checked"])
        self.assertEqual("10.1000/example", metadata["doi"]["value"])
        self.assertIn("title", outcome["changed_fields"])

    def test_uncitable_supporting_source_is_context_only(self) -> None:
        _metadata, audit, _outcome = resolve_bibliography(
            {"paper_id": "P001", "title": {"value": "Procedure"}},
            {"status": "not_found"},
            {
                "action": "supporting_only",
                "document_type": "other",
                "fields": {},
                "manual_evidence": {
                    "evidence_type": "pdf_page",
                    "location": "PDF page 3",
                    "note": "Experimental procedure",
                },
            },
        )
        self.assertEqual("supporting_only", audit["manual_review_status"])
        self.assertTrue(audit["context_only"])
        self.assertFalse(audit["direct_claim_eligible"])

    def test_first_page_doi_prefers_explicit_article_identifier(self) -> None:
        text = (
            "Angewandte Chemie International Edition\n"
            "DOI: 10.1002/anie.201204796.\n"
            "Published online 14 December 2012"
        )
        self.assertEqual(
            "10.1002/anie.201204796", extract_front_matter_doi(text)["value"]
        )

    def test_reliable_local_date_skips_external_provider(self) -> None:
        metadata = {
            "title": {"value": "Local evidence paper", "confidence": 1.0},
            "year": {"value": 2024, "confidence": 0.97},
        }
        connector = _Connector(
            "crossref",
            SourceSearchResult(source="crossref", status="completed", candidates=[]),
        )
        local = {
            "basic_info": {
                "publication_year": 2024,
                "publication_date": "2024-06",
            },
            "publication_evidence": {
                "source_text": "Published online 18 June 2024",
                "source_location": "pdf_page_1",
                "date_type": "published_online",
                "confidence": 0.98,
            },
            "status": "reliable",
            "network_required": False,
        }
        with patch(
            "review_writer_core.bibliography_audit._pdf_first_page",
            return_value={"status": "available", "doi": "", "text": ""},
        ):
            audit = audit_bibliography(
                metadata,
                connectors=[connector],
                local_extraction=local,
                network_mode="fallback",
            )

        self.assertEqual([], connector.requests)
        self.assertEqual("verified", audit["status"])
        self.assertFalse(audit["network_lookup"]["used"])

    def test_pdf_doi_selects_the_matching_publisher_version(self) -> None:
        metadata = {
            "title": {"value": "Enantioselective Decarboxylative Amination", "confidence": 1.0},
            "authors": {"value": ["A. Author"], "confidence": 1.0},
            "year": {"value": 2026, "confidence": 1.0},
            "doi": {"value": "10.1002/ange.201204796", "confidence": 1.0},
        }
        crossref = _Connector(
            "crossref",
            SourceSearchResult(
                source="crossref",
                status="completed",
                candidates=[
                    {
                        "title": "Enantioselective Decarboxylative Amination",
                        "authors": ["A. Author"],
                        "year": 2013,
                        "identifiers": {"doi": "10.1002/anie.201204796"},
                    }
                ],
            ),
        )
        with patch(
            "review_writer_core.bibliography_audit._pdf_first_page",
            return_value={
                "status": "verified",
                "title_present": True,
                "doi": "10.1002/anie.201204796",
                "text_sha256": "test",
            },
        ):
            audit = audit_bibliography(metadata, connectors=[crossref])
        updated, changed = apply_bibliography_updates(metadata, audit)

        self.assertEqual("10.1002/anie.201204796", crossref.requests[0].query)
        self.assertEqual("10.1002/anie.201204796", updated["doi"]["value"])
        self.assertEqual(2013, updated["year"]["value"])
        self.assertIn("doi", changed)

    def test_coverage_diagnosis_uses_explicit_topic_range_when_filters_are_missing(self) -> None:
        report = discovery_coverage_diagnostics(
            {
                "topic": "Selected advances in catalysis, 2019–2022",
                "results": [
                    {
                        "keyword": "catalysis",
                        "local_results": [
                            {"paper_id": "P001", "first_publication_date": "2020-05-01"}
                        ],
                    }
                ],
            }
        )
        self.assertEqual(2019, report["declared_year_from"])
        self.assertEqual(2022, report["declared_year_to"])
        self.assertEqual([2019, 2021, 2022], report["missing_years"])
        self.assertTrue(report["online_search_suggested"])

    def test_final_cleanup_removes_template_unicode_and_reference_web_residue(self) -> None:
        cleaned = _normalize_publication_markup(
            "# Review\n\nReview Writer | modern-survey/6\n\nText\ufffd.\n\n"
            "## References\n\n[1] Cite This Read Online A. Author. Title. Supporting Information\n"
        )
        self.assertNotIn("Review Writer", cleaned)
        self.assertNotIn("modern-survey", cleaned)
        self.assertNotIn("\ufffd", cleaned)
        self.assertNotIn("Cite This", cleaned)
        self.assertNotIn("Supporting Information", cleaned)
        self.assertIn("A. Author. Title.", cleaned)

    def test_figure_attribution_does_not_claim_reuse_permission(self) -> None:
        fields = figure_rights_fields(
            {"paper_id": "P001", "source_label": "Figure 2"}
        )
        self.assertEqual("source_attributed", fields["rights_status"])
        self.assertEqual("unknown", fields["permission_status"])
        self.assertEqual("verified", fields["source_identity_status"])

        verified = figure_rights_fields(
            {
                "paper_id": "P001",
                "license_verified": True,
                "permission_record_id": "permission-123",
            }
        )
        self.assertEqual("license_verified", verified["rights_status"])
        self.assertEqual("verified", verified["permission_status"])
        self.assertEqual("unresolved", verified["source_identity_status"])

    def test_fact_readiness_is_independent_from_worker_completion(self) -> None:
        roles = required_fact_roles(
            "Compare catalyst roles, mechanism evidence, yield, and scope"
        )
        report = fact_readiness_report(
            facts=[
                {
                    "field_id": "quantitative_results",
                    "value": "The reported yield was 82%.",
                    "support_level": "direct",
                    "evidence_refs": [{"evidence_key": "sha256:result"}],
                }
            ],
            required_roles=roles,
            extraction_status="complete",
            failed_fields=["intervention_role", "mechanism"],
        )
        self.assertEqual("partial", report["review_readiness"])
        self.assertEqual(
            "retrieval_not_found", report["field_states"]["mechanism"]
        )

    def test_question_plan_uses_only_blueprint_required_fact_roles(self) -> None:
        plans = build_question_query_plans(
            review_topic="A review of intervention systems",
            heading="Comparative systems",
            required_fact_roles=["intervention_role", "limitations"],
        )
        ids = {row["question_id"] for row in plans}
        self.assertIn("section_focus", ids)
        self.assertIn("intervention_role", ids)
        self.assertIn("limitations", ids)
        self.assertNotIn("mechanism", ids)

    def test_negative_source_claim_needs_verified_absence_and_checked_source(self) -> None:
        self.assertFalse(
            negative_claim_eligibility("retrieval_not_found", ["main_article"])
        )
        self.assertFalse(
            negative_claim_eligibility("source_verified_not_reported", [])
        )
        self.assertTrue(
            negative_claim_eligibility(
                "source_verified_not_reported", ["main_article", "table"]
            )
        )

    def test_bibliography_field_readiness_rejects_residue_and_missing_locator(self) -> None:
        report = bibliography_field_readiness(
            {
                "document_type": "journal_article",
                "title": "A canonical paper",
                "authors": ["A. Author Received 2 May"],
                "journal": "Journal",
                "year": 2024,
            },
            {"status": "verified"},
        )
        self.assertFalse(report["ready"])
        self.assertIn("authors", report["polluted_fields"])
        self.assertIn("pages_or_article_number", report["missing_fields"])

    def test_bibliography_field_readiness_rejects_markup_only_author_items(self) -> None:
        report = bibliography_field_readiness(
            {
                "document_type": "journal_article",
                "title": "A canonical paper",
                "authors": ["A. Author", "<sup></sup>", "Vol., No. –"],
                "journal": "Journal",
                "year": 2024,
                "pages": "1-9",
            },
            {"status": "verified"},
        )
        self.assertFalse(report["ready"])
        self.assertIn("authors", report["polluted_fields"])
        self.assertIn(
            "authors_contain_rejected_items", report["author_quality_issues"]
        )

    def test_publication_voice_detects_retrieval_boundary_language(self) -> None:
        issues = publication_voice_issues(
            "The locally bounded selected Matrix and available excerpts were used."
        )
        self.assertIn(
            "retrieval_boundary_leak", {row["code"] for row in issues}
        )

    def test_missing_direct_evidence_downgrades_primary_without_forcing_citation(self) -> None:
        tasks = [
            {
                "section_id": "S02",
                "section_role": "body",
                "primary_papers": ["P001", "P002", "P003"],
                "supporting_papers": ["P004"],
                "context_papers": [],
                "allowed_papers": ["P001", "P002", "P003", "P004"],
                "writing_mode": "primary_evidence_synthesis",
            }
        ]
        evidence = {
            "S02": {
                "writeable_primary_papers": ["P001"],
                "context_only_primary_papers": ["P002"],
                "unresolved_primary_papers": ["P003"],
                "primary_paper_states": [
                    {"paper_id": "P002", "diagnostic": "query_miss"},
                    {"paper_id": "P003", "diagnostic": "index_incomplete"},
                ],
            }
        }

        [task] = SectionsService._apply_primary_evidence_roles(tasks, evidence)

        self.assertEqual(["P001"], task["primary_papers"])
        self.assertEqual(["P002", "P003"], task["context_papers"])
        self.assertEqual(2, len(task["evidence_role_changes"]))
        self.assertEqual(["P001", "P004", "P002", "P003"], task["allowed_papers"])

    def test_visible_figure_callout_is_not_satisfied_by_hidden_metadata_or_caption(self) -> None:
        metadata = (
            '<!-- inserted_figure: {"figure_id":"P001-F01","paper_id":"P001",'
            '"output_artifact_id":"11111111-1111-1111-1111-111111111111",'
            '"published_label":"Figure 1","interpretation_basis":"source_caption"} -->'
        )
        image = "![Scheme](/api/v1/artifacts/11111111-1111-1111-1111-111111111111/content)"
        caption = "*Figure 1. Scheme*"
        missing = _figure_argument_findings("\n\n".join((metadata, image, caption)))
        self.assertEqual(["visible_callout_or_interpretation_missing"], missing[0]["issues"])
        complete = _figure_argument_findings(
            "Figure 1 presents the reported transformation as visual support.\n\n"
            + "\n\n".join((metadata, image, caption))
        )
        self.assertEqual([], complete)

    def test_publication_voice_detects_prose_but_ignores_machine_metadata(self) -> None:
        markdown = """# Review

The supplied evidence package establishes the result.

<!-- the workflow must preserve this machine marker -->

## References

[1] Supplied evidence package, 2024.
"""
        issues = publication_voice_issues(markdown)
        self.assertEqual(
            ["evidence_package", "workflow_artifact"],
            [row["code"] for row in issues],
        )

    def test_bibliography_retry_merges_previous_successful_source(self) -> None:
        metadata = {
            "title": {"value": "Catalytic Allenation"},
            "authors": {"value": ["A. Author"]},
            "doi": {"value": "10.1000/example"},
        }
        previous = {
            "sources": {
                "crossref": {
                    "status": "verified",
                    "candidate": {"title": "Catalytic Allenation"},
                },
                "openalex": {"status": "unavailable", "error": "timeout"},
            }
        }
        openalex = _Connector(
            "openalex",
            SourceSearchResult(
                source="openalex",
                status="completed",
                candidates=[
                    {
                        "title": "Catalytic Allenation",
                        "authors": ["A. Author"],
                        "identifiers": {"doi": "10.1000/example"},
                    }
                ],
            ),
        )
        result = audit_bibliography(
            metadata,
            connectors=[openalex],
            previous_audit=previous,
        )
        self.assertEqual("verified", result["status"])
        self.assertEqual("verified", result["sources"]["crossref"]["status"])
        self.assertEqual("verified", result["sources"]["openalex"]["status"])
        self.assertFalse(result["canonical_metadata_changed"])

    def test_verified_title_and_author_correct_low_confidence_year(self) -> None:
        metadata = {
            "title": {"value": "Chemoenzymatic Dynamic Kinetic Resolution of Axially Chiral Allenes", "confidence": 0.88},
            "authors": {"value": ["A. Author"], "confidence": 0.8},
            "year": {"value": 2026, "source": "download_date", "confidence": 0.68},
            "doi": {"value": None, "confidence": 0.0},
        }
        crossref = _Connector(
            "crossref",
            SourceSearchResult(
                source="crossref",
                status="completed",
                candidates=[
                    {
                        "title": "Chemoenzymatic Dynamic Kinetic Resolution of Axially Chiral Allenes",
                        "authors": ["A. Author"],
                        "year": 2010,
                        "bibliographic_year": 2010,
                        "first_publication_date": "2010-07-15",
                        "publication_status": "issue_assigned",
                        "journal": "Chemistry - A European Journal",
                        "identifiers": {"doi": "10.1002/chem.201000301"},
                    }
                ],
            ),
        )

        audit = audit_bibliography(metadata, connectors=[crossref])
        updated, changed = apply_bibliography_updates(metadata, audit)

        self.assertEqual("verified", audit["status"])
        self.assertIn("year", changed)
        self.assertEqual(2010, updated["year"]["value"])
        self.assertEqual(2010, updated["bibliographic_year"]["value"])
        self.assertEqual(
            "2010-07-15", updated["first_publication_date"]["value"]
        )
        self.assertEqual("issue_assigned", updated["publication_status"]["value"])
        self.assertEqual("10.1002/chem.201000301", updated["doi"]["value"])

    def test_human_checked_year_is_not_automatically_overwritten(self) -> None:
        metadata = {
            "title": {"value": "A sufficiently distinctive article title", "confidence": 0.9},
            "authors": {"value": ["A. Author"], "confidence": 0.8},
            "year": {"value": 2026, "confidence": 1.0, "human_checked": True},
        }
        source = _Connector(
            "crossref",
            SourceSearchResult(
                source="crossref",
                status="completed",
                candidates=[
                    {
                        "title": "A sufficiently distinctive article title",
                        "authors": ["A. Author"],
                        "year": 2010,
                    }
                ],
            ),
        )

        audit = audit_bibliography(metadata, connectors=[source])
        updated, changed = apply_bibliography_updates(metadata, audit)

        self.assertNotIn("year", changed)
        self.assertEqual(2026, updated["year"]["value"])

    def test_verified_publisher_record_replaces_equal_confidence_parser_guess(self) -> None:
        metadata = {
            "title": {
                "value": "A Room-Temperature Catalytic Asymmetric Synthesis of Allenes with ECNU-Phos",
                "confidence": 1.0,
            },
            "authors": {
                "value": [
                    "Yuli Wang",
                    "<sup></sup> Wanli Zhang",
                    "<sup></sup> and Shengming Ma<sup>",
                    "</sup>",
                ],
                "confidence": 1.0,
            },
            "year": {
                "value": 2026,
                "source": "filename_or_front_matter",
                "confidence": 1.0,
                "human_checked": False,
            },
            "journal": {
                "value": "Green Chemistry",
                "confidence": 1.0,
                "human_checked": False,
            },
        }
        crossref = _Connector(
            "crossref",
            SourceSearchResult(
                source="crossref",
                status="completed",
                candidates=[
                    {
                        "title": "A Room-Temperature Catalytic Asymmetric Synthesis of Allenes with ECNU-Phos",
                        "authors": ["Yuli Wang", "Wanli Zhang", "Shengming Ma"],
                        "year": 2013,
                        "bibliographic_year": 2013,
                        "first_publication_date": "2013-08-14",
                        "publication_status": "issue_assigned",
                        "journal": "Journal of the American Chemical Society",
                        "identifiers": {"doi": "10.1021/ja406135t"},
                    }
                ],
            ),
        )

        audit = audit_bibliography(metadata, connectors=[crossref])
        updated, changed = apply_bibliography_updates(metadata, audit)

        self.assertEqual("verified", audit["sources"]["crossref"]["status"])
        self.assertEqual("verified", audit["status"])
        self.assertEqual(2013, updated["year"]["value"])
        self.assertEqual(2013, updated["bibliographic_year"]["value"])
        self.assertEqual("10.1021/ja406135t", updated["doi"]["value"])
        self.assertIn("journal", changed)


if __name__ == "__main__":
    unittest.main()
