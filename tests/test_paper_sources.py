from __future__ import annotations

import unittest

from review_writer_core.paper_sources import PaperSearchRequest, SourceSearchResult, search_paper_sources
from review_writer_core.paper_sources.deduplicate import deduplicate_candidates


def candidate(
    source: str,
    provider_id: str,
    *,
    title: str,
    doi: str = "",
    authors: list[str] | None = None,
    year: int = 2024,
    rank: int = 1,
) -> dict:
    return {
        "identifiers": {
            "doi": doi,
            "arxiv_id": "",
            "openalex_id": provider_id if source == "openalex" else "",
            "semantic_scholar_id": provider_id if source == "semantic_scholar" else "",
        },
        "title": title,
        "abstract": f"Evidence about {title}",
        "authors": authors or ["Ada Lovelace"],
        "year": year,
        "publication_date": f"{year}-01-01",
        "journal": "Journal",
        "document_type": "journal-article",
        "citation_count": 10,
        "landing_url": f"https://example.test/{provider_id}",
        "pdf_url": "",
        "open_access": {"is_oa": False, "license": "", "source": source},
        "sources": [
            {
                "name": source,
                "provider_id": provider_id,
                "provider_rank": rank,
                "provider_score": None,
            }
        ],
        "abstract_decision": {"status": "not_run", "reason": "", "model_tier_snapshot": ""},
        "selected_for_download": False,
    }


class FakeConnector:
    def __init__(self, name: str, candidates: list[dict] | None = None, error: str = ""):
        self.name = name
        self.candidates = candidates or []
        self.error = error

    def search(self, _request: PaperSearchRequest) -> SourceSearchResult:
        if self.error:
            return SourceSearchResult(source=self.name, status="failed", error=self.error)
        return SourceSearchResult(source=self.name, status="completed", candidates=self.candidates)


class MultiSourceSearchTests(unittest.TestCase):
    def test_partial_source_failure_preserves_results_and_deduplicates_strong_ids(self) -> None:
        connectors = [
            FakeConnector(
                "crossref",
                [candidate("crossref", "10.1/example", title="Catalyst Design", doi="10.1/example", rank=1)],
            ),
            FakeConnector(
                "openalex",
                [candidate("openalex", "W1", title="Catalyst Design", doi="https://doi.org/10.1/EXAMPLE", rank=2)],
            ),
            FakeConnector(
                "arxiv",
                [candidate("arxiv", "2401.00001", title="General Catalyst Screening", rank=1)],
            ),
            FakeConnector("semantic_scholar", error="rate limited"),
        ]
        events = []
        result = search_paper_sources(
            PaperSearchRequest(query="catalyst design", topic="catalyst design"),
            connectors=connectors,
            status_callback=lambda *event: events.append(event),
        )

        self.assertEqual("partial", result["completion_state"])
        self.assertTrue(result["degraded"])
        self.assertEqual("rate limited", result["source_errors"]["semantic_scholar"])
        self.assertEqual(2, len(result["candidates"]))
        merged = next(item for item in result["candidates"] if item["doi"] == "10.1/example")
        self.assertEqual({"crossref", "openalex"}, {item["name"] for item in merged["sources"]})
        self.assertIsInstance(merged["score"], float)
        self.assertIn("source_rank_normalized", merged["score_components"])
        self.assertTrue(any(event[1] == "failed" for event in events))

    def test_all_sources_failed_is_explicit(self) -> None:
        result = search_paper_sources(
            PaperSearchRequest(query="topic"),
            connectors=[FakeConnector("crossref", error="offline"), FakeConnector("arxiv", error="timeout")],
        )
        self.assertEqual("failed", result["completion_state"])
        self.assertEqual([], result["candidates"])
        self.assertEqual({"crossref", "arxiv"}, set(result["source_errors"]))

    def test_title_only_similarity_does_not_merge_without_author_evidence(self) -> None:
        rows = [
            candidate("crossref", "one", title="Same Title", authors=["First Author"]),
            candidate("openalex", "two", title="Same Title", authors=["Different Author"]),
        ]
        self.assertEqual(2, len(deduplicate_candidates(rows)))


if __name__ == "__main__":
    unittest.main()
