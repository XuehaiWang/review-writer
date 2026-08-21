from __future__ import annotations

import unittest

from review_writer_core.bibliography_audit import audit_bibliography
from review_writer_core.paper_sources.base import PaperSearchRequest, SourceSearchResult
from review_writer_core.publication_voice import publication_voice_issues
from review_writer_api.domain_services.final import _figure_argument_findings


class _Connector:
    def __init__(self, name: str, result: SourceSearchResult):
        self.name = name
        self._result = result

    def search(self, _request: PaperSearchRequest) -> SourceSearchResult:
        return self._result


class QualityOptimizationUnitTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
